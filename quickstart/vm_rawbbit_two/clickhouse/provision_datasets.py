#!/usr/bin/env python3
"""Safely provision optional dataset databases and read-only MCP identities.

This entry point intentionally uses only the Python standard library and only
accepts protected file paths. It is run as root on the analytics VM, but sends
ClickHouse admin credentials only to the loopback HTTP endpoint.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import stat
import tempfile
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any


IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
DATASET_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
CLICKHOUSE_CODE_RE = re.compile(r"Code:\s*(\d+)")
JOURNAL_FIELDS = {
    "database", "table", "role", "user", "privileges", "schema_fingerprint",
    "object_ids", "password_revision", "state", "pending_action",
}
JOURNAL_OBJECT_ID_FIELDS = {"database", "table", "role", "user"}
JOURNAL_PENDING_ACTIONS = {
    "create_database", "create_table", "create_role", "create_user",
    "alter_user_settings", "rotate_user_password", "revoke_managed_role_assignment",
    "grant_dataset_role_to_user", "set_default_dataset_role",
    "revoke_obsolete_managed_grant", "grant_dataset_select",
}
CANONICAL_TABLE_RE = re.compile(
    r"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+analytics\.events\s*"
    r"\((.*?)\)\s*ENGINE\s*=\s*([A-Za-z0-9_]+)\s*"
    r"PARTITION\s+BY\s*(.*?)\s*ORDER\s+BY\s*(.*?);",
    re.IGNORECASE | re.DOTALL,
)


class ProvisionError(RuntimeError):
    """A safe-to-report configuration, ownership, or ClickHouse error."""


def _identifier(value: Any, name: str) -> str:
    if not isinstance(value, str) or not IDENTIFIER_RE.fullmatch(value):
        raise ProvisionError(f"{name} must be a simple ClickHouse identifier")
    return value


def _dataset_id(value: Any) -> str:
    if not isinstance(value, str) or not DATASET_ID_RE.fullmatch(value):
        raise ProvisionError("dataset id must be a lowercase slug using letters, digits, and underscores")
    if value == "default":
        raise ProvisionError("dataset id 'default' is reserved for the original main dataset")
    return value


def _required_string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProvisionError(f"{name} is required")
    return value


def _reject_unknown_fields(value: dict[str, Any], allowed: set[str], name: str) -> None:
    if set(value) - allowed:
        raise ProvisionError(f"{name} contains unsupported fields")


def _quote_identifier(value: str) -> str:
    return f"`{_identifier(value, 'ClickHouse identifier')}`"


def _quote_string(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _secure_read_json(path: Path, *, require_root: bool) -> dict[str, Any]:
    try:
        if require_root:
            _validate_root_owned_parent(path.parent)
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise ProvisionError("configuration and journal paths must be regular files, not symlinks")
        if info.st_mode & 0o077:
            raise ProvisionError("configuration and journal files must have mode 0600 or stricter")
        if require_root and info.st_uid != 0:
            raise ProvisionError("privileged configuration and journal files must be root-owned")
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except ProvisionError:
        raise
    except (OSError, json.JSONDecodeError) as exc:
        raise ProvisionError("protected JSON file is missing or invalid") from exc
    if not isinstance(parsed, dict):
        raise ProvisionError("protected JSON file must contain an object")
    return parsed


def _validate_root_owned_parent(directory: Path) -> None:
    """Reject writable or replaceable parents for privileged protected files."""
    for current in (directory, *directory.parents):
        try:
            info = current.lstat()
        except OSError as exc:
            raise ProvisionError("protected file parent directory is missing or inaccessible") from exc
        if (
            not stat.S_ISDIR(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or info.st_uid != 0
            or info.st_mode & 0o022
        ):
            raise ProvisionError("protected file parent directories must be root-owned and not group/world writable")


def _split_top_level(value: str) -> list[str]:
    parts: list[str] = []
    start = 0
    depth = 0
    quote: str | None = None
    escaped = False
    for index, character in enumerate(value):
        if quote:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == quote:
                quote = None
            continue
        if character in "'\"`":
            quote = character
        elif character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
            if depth < 0:
                raise ProvisionError("canonical events schema has invalid parentheses")
        elif character == "," and depth == 0:
            parts.append(value[start:index].strip())
            start = index + 1
    if depth != 0 or quote:
        raise ProvisionError("canonical events schema has invalid SQL structure")
    tail = value[start:].strip()
    if tail:
        parts.append(tail)
    return parts


def _normalize_expression(value: str) -> str:
    normalized = re.sub(r"\s+", "", value).lower().rstrip(";")
    if normalized.startswith("(") and normalized.endswith(")"):
        depth = 0
        wraps_entire_expression = True
        for index, character in enumerate(normalized):
            if character == "(":
                depth += 1
            elif character == ")":
                depth -= 1
                if depth == 0 and index != len(normalized) - 1:
                    wraps_entire_expression = False
                    break
        if wraps_entire_expression and depth == 0:
            normalized = normalized[1:-1]
    return normalized


@dataclass(frozen=True)
class CanonicalSchema:
    create_table_sql: str
    columns: tuple[tuple[str, str], ...]
    engine: str
    partition_key: str
    sorting_key: str
    fingerprint: str


def parse_canonical_schema(path: Path) -> CanonicalSchema:
    try:
        source = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ProvisionError("canonical events schema file is missing or unreadable") from exc
    match = CANONICAL_TABLE_RE.search(source)
    if not match:
        raise ProvisionError("canonical events schema does not contain the expected analytics.events MergeTree DDL")
    column_block, engine, partition_key, sorting_key = match.groups()
    columns: list[tuple[str, str]] = []
    for definition in _split_top_level(column_block):
        column_match = re.match(r"^\s*`?([A-Za-z_][A-Za-z0-9_]*)`?\s+(.+?)\s*$", definition, re.DOTALL)
        if not column_match:
            raise ProvisionError("canonical events schema contains an unsupported column declaration")
        columns.append((column_match.group(1), re.sub(r"\s+", " ", column_match.group(2).strip())))
    if not columns or len({name for name, _ in columns}) != len(columns):
        raise ProvisionError("canonical events schema must contain unique columns")
    if engine.lower() != "mergetree":
        raise ProvisionError("canonical events schema must use MergeTree")
    normalized_partition = _normalize_expression(partition_key)
    normalized_sorting = _normalize_expression(sorting_key)
    fingerprint_payload = {
        "columns": columns,
        "engine": engine.lower(),
        "partition_key": normalized_partition,
        "sorting_key": normalized_sorting,
    }
    fingerprint = hashlib.sha256(
        json.dumps(fingerprint_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    create_table_sql = match.group(0).strip().rstrip(";")
    return CanonicalSchema(
        create_table_sql=create_table_sql,
        columns=tuple(columns),
        engine=engine,
        partition_key=normalized_partition,
        sorting_key=normalized_sorting,
        fingerprint=fingerprint,
    )


@dataclass(frozen=True)
class AdminConnection:
    host: str
    port: int
    username: str
    password: str
    timeout_seconds: int

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}/"


class ClickHouseHTTP:
    def __init__(self, connection: AdminConnection) -> None:
        self.connection = connection
        credentials = f"{connection.username}:{connection.password}".encode("utf-8")
        self.authorization = "Basic " + base64.b64encode(credentials).decode("ascii")

    def request(self, sql: str) -> str:
        request = urllib.request.Request(
            self.connection.url,
            data=sql.encode("utf-8"),
            headers={
                "Authorization": self.authorization,
                "Content-Type": "text/plain; charset=utf-8",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.connection.timeout_seconds) as response:
                return response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            # Do not return ClickHouse's response body: it can include SQL text.
            body = exc.read(4096).decode("utf-8", errors="ignore")
            code_match = CLICKHOUSE_CODE_RE.search(body)
            code = code_match.group(1) if code_match else "unknown"
            raise ProvisionError(f"ClickHouse rejected a request (HTTP {exc.code}, error code {code})") from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ProvisionError("ClickHouse request failed or timed out") from None

    def query(self, sql: str) -> list[dict[str, Any]]:
        body = self.request(sql.rstrip().rstrip(";") + " FORMAT JSONEachRow")
        if not body.strip():
            return []
        try:
            rows = [json.loads(line) for line in body.splitlines() if line.strip()]
        except json.JSONDecodeError as exc:
            raise ProvisionError("ClickHouse returned an invalid metadata response") from exc
        if not all(isinstance(row, dict) for row in rows):
            raise ProvisionError("ClickHouse returned an invalid metadata response")
        return rows

    def execute(self, sql: str) -> None:
        self.request(sql)


def _validate_registry(config: dict[str, Any]) -> tuple[AdminConnection, list[dict[str, Any]]]:
    _reject_unknown_fields(
        config,
        {"version", "admin", "main_database", "main_user", "datasets"},
        "provisioning registry",
    )
    if type(config.get("version")) is not int or config["version"] != 1:
        raise ProvisionError("unsupported provisioning registry version")
    main_database = _identifier(config.get("main_database"), "main database")
    main_user = _identifier(config.get("main_user"), "main MCP user")
    admin = config.get("admin")
    raw_datasets = config.get("datasets")
    if not isinstance(admin, dict) or not isinstance(raw_datasets, list):
        raise ProvisionError("provisioning registry requires admin and datasets fields")
    _reject_unknown_fields(admin, {"host", "port", "username", "password", "timeout_seconds"}, "admin config")
    host = _required_string(admin.get("host"), "admin host")
    if host not in {"127.0.0.1", "localhost"}:
        raise ProvisionError("admin host must be localhost so credentials remain on the host")
    username = _identifier(admin.get("username"), "admin username")
    password = _required_string(admin.get("password"), "admin password")
    if main_user == username:
        raise ProvisionError("main MCP user must differ from the privileged admin user")
    port = admin.get("port", 8123)
    timeout = admin.get("timeout_seconds", 15)
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise ProvisionError("admin port must be an integer from 1 to 65535")
    if not isinstance(timeout, int) or isinstance(timeout, bool) or not 1 <= timeout <= 120:
        raise ProvisionError("admin timeout_seconds must be an integer from 1 to 120")
    connection = AdminConnection(host, port, username, password, timeout)

    datasets: list[dict[str, Any]] = []
    ids: set[str] = set()
    names: set[tuple[str, str]] = set()
    databases: set[str] = set()
    roles: set[str] = set()
    users: set[str] = set()
    for raw in raw_datasets:
        if not isinstance(raw, dict):
            raise ProvisionError("each dataset entry must be an object")
        _reject_unknown_fields(
            raw,
            {
                "id", "enabled", "mode", "endpoint_path", "database", "table", "create_table",
                "main_exposed", "mcp_user", "mcp_role", "password_revision", "mcp_password",
                "limits", "adopt",
            },
            "dataset config",
        )
        dataset_id = _dataset_id(raw.get("id"))
        if dataset_id in ids:
            raise ProvisionError("dataset ids must be unique")
        ids.add(dataset_id)
        enabled = raw.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ProvisionError("dataset enabled must be a boolean")
        mode = raw.get("mode")
        if not isinstance(mode, str) or mode not in {"managed", "reference"}:
            raise ProvisionError("dataset mode must be managed or reference")
        database = _identifier(raw.get("database"), "database")
        if database == main_database:
            raise ProvisionError("dataset database must differ from the main ClickHouse database")
        table = _identifier(raw.get("table", "events"), "table")
        if database in databases:
            raise ProvisionError("each managed dataset must use a distinct database")
        databases.add(database)
        if (database, table) in names:
            raise ProvisionError("each dataset must target a unique database and table")
        names.add((database, table))
        endpoint_path = _required_string(raw.get("endpoint_path"), "endpoint_path")
        if (
            endpoint_path != endpoint_path.strip()
            or not endpoint_path.startswith(f"/datasets/{dataset_id}/")
            or endpoint_path.endswith("/")
            or "//" in endpoint_path
            or any(part in {".", ".."} for part in endpoint_path.split("/"))
            or not re.fullmatch(r"/datasets/[a-z][a-z0-9_]{0,39}/[A-Za-z0-9_/-]+", endpoint_path)
        ):
            raise ProvisionError("dataset endpoint_path must use /datasets/<id>/<MCP_PATH>")
        create_table = raw.get("create_table", False)
        if not isinstance(create_table, bool):
            raise ProvisionError("create_table must be a boolean")
        if mode == "reference" and create_table:
            raise ProvisionError("reference datasets cannot request table creation")
        mcp_user = _identifier(raw.get("mcp_user"), "MCP user")
        mcp_role = _identifier(raw.get("mcp_role"), "MCP role")
        if mcp_user == username:
            raise ProvisionError("dataset MCP users must be distinct from the privileged admin user")
        if mcp_user == main_user:
            raise ProvisionError("dataset MCP users must be distinct from the main MCP user")
        if mcp_user in users or mcp_role in roles:
            raise ProvisionError("MCP user and role names must be unique")
        users.add(mcp_user)
        roles.add(mcp_role)
        password_revision = raw.get("password_revision", 1)
        if not isinstance(password_revision, int) or isinstance(password_revision, bool) or password_revision < 1:
            raise ProvisionError("password_revision must be a positive integer")
        main_exposed = raw.get("main_exposed", False)
        if not isinstance(main_exposed, bool):
            raise ProvisionError("main_exposed must be a boolean")
        limits = raw.get("limits", {})
        if not isinstance(limits, dict):
            raise ProvisionError("dataset limits must be an object")
        _reject_unknown_fields(
            limits,
            {"max_query_rows", "max_sample_rows", "max_execution_seconds"},
            "dataset limits",
        )
        checked_limits: dict[str, int] = {}
        for name in ("max_query_rows", "max_sample_rows", "max_execution_seconds"):
            value = limits.get(name)
            if value is not None:
                if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                    raise ProvisionError(f"{name} must be a positive integer")
                checked_limits[name] = value
        adoption = raw.get("adopt", {})
        if not isinstance(adoption, dict):
            raise ProvisionError("adopt must be an object")
        _reject_unknown_fields(adoption, {"database_uuid", "table_uuid", "schema_fingerprint"}, "adoption config")
        for name in ("database_uuid", "table_uuid", "schema_fingerprint"):
            value = adoption.get(name)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ProvisionError(f"adopt.{name} must be a non-empty string")
        if mode == "reference" and any(adoption.values()):
            raise ProvisionError("reference datasets do not use managed-object adoption")
        if enabled and mode == "managed":
            _required_string(raw.get("mcp_password"), "managed MCP password")
        datasets.append(
            {
                **raw,
                "id": dataset_id,
                "enabled": enabled,
                "mode": mode,
                "database": database,
                "table": table,
                "endpoint_path": endpoint_path,
                "create_table": create_table,
                "mcp_user": mcp_user,
                "mcp_role": mcp_role,
                "password_revision": password_revision,
                "main_exposed": main_exposed,
                "limits": checked_limits,
                "adopt": adoption,
            }
        )
    return connection, datasets


def _validate_runtime(
    runtime: dict[str, Any], *, main_database: str = "analytics"
) -> dict[str, dict[str, Any]]:
    _reject_unknown_fields(runtime, {"version", "datasets"}, "runtime registry")
    if type(runtime.get("version")) is not int or runtime["version"] != 1 or not isinstance(runtime.get("datasets"), list):
        raise ProvisionError("runtime registry version or datasets field is invalid")
    main_database = _identifier(main_database, "main database")
    by_id: dict[str, dict[str, Any]] = {}
    tokens: set[str] = set()
    databases: set[str] = set()
    users: set[str] = set()
    for dataset in runtime["datasets"]:
        if not isinstance(dataset, dict):
            raise ProvisionError("runtime dataset entry must be an object")
        _reject_unknown_fields(
            dataset,
            {
                "id", "enabled", "endpoint_path", "database", "table", "main_exposed",
                "clickhouse_user", "clickhouse_password", "bearer_tokens", "max_query_rows",
                "max_sample_rows", "max_execution_seconds",
            },
            "runtime dataset config",
        )
        dataset_id = _dataset_id(dataset.get("id"))
        if dataset_id in by_id or dataset.get("enabled") is not True:
            raise ProvisionError("runtime registry must contain unique enabled datasets only")
        database = _identifier(dataset.get("database"), "runtime database")
        if database == main_database:
            raise ProvisionError("runtime dataset database must differ from the main ClickHouse database")
        if database in databases:
            raise ProvisionError("each runtime dataset must use a distinct ClickHouse database")
        databases.add(database)
        _identifier(dataset.get("table", "events"), "runtime table")
        clickhouse_user = _identifier(dataset.get("clickhouse_user"), "runtime ClickHouse user")
        if clickhouse_user in users:
            raise ProvisionError("runtime datasets must use distinct ClickHouse users")
        users.add(clickhouse_user)
        endpoint_path = _required_string(dataset.get("endpoint_path"), "runtime endpoint_path")
        if (
            endpoint_path != endpoint_path.strip()
            or not endpoint_path.startswith(f"/datasets/{dataset_id}/")
            or endpoint_path.endswith("/")
            or "//" in endpoint_path
            or any(part in {".", ".."} for part in endpoint_path.split("/"))
            or not re.fullmatch(r"/datasets/[a-z][a-z0-9_]{0,39}/[A-Za-z0-9_/-]+", endpoint_path)
        ):
            raise ProvisionError("runtime endpoint_path must use /datasets/<id>/<MCP_PATH>")
        if not isinstance(dataset.get("main_exposed"), bool):
            raise ProvisionError("runtime main_exposed must be a boolean")
        if not isinstance(dataset.get("clickhouse_password"), str) or not dataset["clickhouse_password"].strip():
            raise ProvisionError("runtime dataset ClickHouse password is required")
        bearer_tokens = dataset.get("bearer_tokens")
        if not isinstance(bearer_tokens, dict) or not bearer_tokens:
            raise ProvisionError("runtime dataset requires scoped bearer tokens")
        for label, token in bearer_tokens.items():
            if (
                not isinstance(label, str)
                or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", label)
                or not isinstance(token, str)
                or not token.strip()
                or token != token.strip()
            ):
                raise ProvisionError("runtime scoped bearer token labels and values must be non-empty")
            if token in tokens:
                raise ProvisionError("runtime scoped bearer token values must be unique")
            tokens.add(token)
        for name in ("max_query_rows", "max_sample_rows", "max_execution_seconds"):
            limit = dataset.get(name)
            if limit is not None and (not isinstance(limit, int) or isinstance(limit, bool) or limit < 1):
                raise ProvisionError(f"runtime {name} must be a positive integer")
        by_id[dataset_id] = dataset
    return by_id


def _read_journal(path: Path, *, require_root: bool) -> dict[str, Any]:
    if not path.exists():
        return {"version": 1, "datasets": {}}
    journal = _secure_read_json(path, require_root=require_root)
    if type(journal.get("version")) is not int or journal["version"] != 1 or not isinstance(journal.get("datasets"), dict):
        raise ProvisionError("ownership journal has an unsupported or invalid format")
    _reject_unknown_fields(journal, {"version", "datasets", "main_access_targets"}, "ownership journal")
    managed_names: dict[str, str] = {}
    for dataset_id, record in journal["datasets"].items():
        _dataset_id(dataset_id)
        if not isinstance(record, dict):
            raise ProvisionError("ownership journal contains an invalid dataset record")
        _reject_unknown_fields(record, JOURNAL_FIELDS, "ownership journal dataset")
        for name in ("database", "table", "role", "user"):
            _identifier(record.get(name), f"ownership journal {name}")
            if name == "database":
                if record[name] in managed_names:
                    raise ProvisionError("ownership journal assigns a database to multiple dataset IDs")
                managed_names[record[name]] = dataset_id
        if record.get("privileges") != ["SELECT"]:
            raise ProvisionError("ownership journal has an unsupported managed privilege set")
        fingerprint = record.get("schema_fingerprint")
        if not isinstance(fingerprint, str) or not SHA256_RE.fullmatch(fingerprint):
            raise ProvisionError("ownership journal has an invalid schema fingerprint")
        object_ids = record.get("object_ids")
        if not isinstance(object_ids, dict) or set(object_ids) - JOURNAL_OBJECT_ID_FIELDS:
            raise ProvisionError("ownership journal has an invalid object identity map")
        if any(not isinstance(value, str) or not value.strip() for value in object_ids.values()):
            raise ProvisionError("ownership journal contains an invalid ClickHouse object ID")
        revision = record.get("password_revision")
        if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
            raise ProvisionError("ownership journal has an invalid password revision")
        state = record.get("state")
        if not isinstance(state, str) or state not in {"pending", "applied"}:
            raise ProvisionError("ownership journal has an invalid provisioning state")
        pending_action = record.get("pending_action")
        if pending_action is not None and (
            not isinstance(pending_action, str) or pending_action not in JOURNAL_PENDING_ACTIONS
        ):
            raise ProvisionError("ownership journal has an unsupported pending operation")
        if state == "applied" and pending_action is not None:
            raise ProvisionError("ownership journal has an applied record with a pending operation")
    access_targets = journal.get("main_access_targets", [])
    if not isinstance(access_targets, list):
        raise ProvisionError("ownership journal has invalid main-access target records")
    seen_targets: set[tuple[str, str]] = set()
    for target in access_targets:
        if not isinstance(target, dict):
            raise ProvisionError("ownership journal has invalid main-access target records")
        _reject_unknown_fields(target, {"database", "table"}, "ownership journal main-access target")
        database = _identifier(target.get("database"), "ownership journal main-access database")
        table = _identifier(target.get("table"), "ownership journal main-access table")
        if (database, table) in seen_targets:
            raise ProvisionError("ownership journal has duplicate main-access target records")
        seen_targets.add((database, table))
    return journal


def _atomic_write_json(path: Path, value: dict[str, Any], *, require_root: bool) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path.parent.is_symlink():
            raise ProvisionError("ownership journal directory must not be a symlink")
        if require_root:
            _validate_root_owned_parent(path.parent)
        fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as temporary:
                json.dump(value, temporary, sort_keys=True, separators=(",", ":"))
                temporary.write("\n")
                temporary.flush()
                os.fsync(temporary.fileno())
            if require_root and os.geteuid() != 0:
                raise ProvisionError("privileged ownership journal writes must run as root")
            os.replace(temporary_name, path)
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if os.path.exists(temporary_name):
                os.unlink(temporary_name)
    except ProvisionError:
        raise
    except OSError as exc:
        raise ProvisionError("ownership journal could not be atomically written") from exc


def _single_row(client: ClickHouseHTTP, sql: str) -> dict[str, Any] | None:
    rows = client.query(sql)
    if len(rows) > 1:
        raise ProvisionError("ClickHouse metadata returned duplicate object names")
    return rows[0] if rows else None


def _has_stable_uuid(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = uuid.UUID(value)
    except (AttributeError, TypeError, ValueError):
        return False
    return parsed.int != 0


def _object_metadata(client: ClickHouseHTTP, dataset: dict[str, Any], kind: str) -> dict[str, Any] | None:
    if kind == "database":
        name = dataset["database"]
        return _single_row(
            client,
            "SELECT name, engine, toString(uuid) AS object_id FROM system.databases "
            f"WHERE name = {_quote_string(name)}",
        )
    if kind == "table":
        database = dataset["database"]
        table = dataset["table"]
        return _single_row(
            client,
            "SELECT database, name, toString(uuid) AS object_id, engine, partition_key, sorting_key "
            "FROM system.tables "
            f"WHERE database = {_quote_string(database)} AND name = {_quote_string(table)}",
        )
    if kind == "role":
        name = dataset["mcp_role"]
        return _single_row(
            client,
            "SELECT name, toString(id) AS object_id FROM system.roles "
            f"WHERE name = {_quote_string(name)}",
        )
    if kind == "user":
        name = dataset["mcp_user"]
        return _single_row(
            client,
            "SELECT name, toString(id) AS object_id FROM system.users "
            f"WHERE name = {_quote_string(name)}",
        )
    raise ProvisionError("unsupported managed object type")


def _actual_columns(client: ClickHouseHTTP, dataset: dict[str, Any]) -> tuple[tuple[str, str], ...]:
    rows = client.query(
        "SELECT name, type FROM system.columns "
        f"WHERE database = {_quote_string(dataset['database'])} "
        f"AND table = {_quote_string(dataset['table'])} ORDER BY position"
    )
    return tuple((str(row["name"]), str(row["type"])) for row in rows)


def _schema_matches(client: ClickHouseHTTP, dataset: dict[str, Any], schema: CanonicalSchema) -> bool:
    table = _object_metadata(client, dataset, "table")
    if table is None:
        return False
    columns = _actual_columns(client, dataset)
    return (
        columns == schema.columns
        and str(table["engine"]).lower() == schema.engine.lower()
        and _normalize_expression(str(table.get("partition_key") or "")) == schema.partition_key
        and _normalize_expression(str(table.get("sorting_key") or "")) == schema.sorting_key
    )


def _schema_fingerprint(client: ClickHouseHTTP, dataset: dict[str, Any], schema: CanonicalSchema) -> str:
    table = _object_metadata(client, dataset, "table")
    if table is None or not _schema_matches(client, dataset, schema):
        raise ProvisionError("dataset table is not compatible with the canonical Rawbbit events schema")
    return schema.fingerprint


def _ownership_record_matches(record: dict[str, Any], dataset: dict[str, Any]) -> bool:
    expected = {
        "database": dataset["database"],
        "table": dataset["table"],
        "role": dataset["mcp_role"],
        "user": dataset["mcp_user"],
        "privileges": ["SELECT"],
    }
    return all(record.get(key) == value for key, value in expected.items())


def _assert_recorded_id(
    record: dict[str, Any] | None,
    kind: str,
    metadata: dict[str, Any] | None,
) -> None:
    if record is None:
        return
    if kind in {"database", "table"} and metadata is not None and not _has_stable_uuid(metadata.get("object_id")):
        raise ProvisionError(f"recovery required: {kind} has no stable ClickHouse UUID")
    recorded_id = record.get("object_ids", {}).get(kind)
    if recorded_id is None:
        if metadata is not None:
            raise ProvisionError(
                f"recovery required: {kind} exists without a recorded ClickHouse object ID"
            )
        if record.get("pending_action") in (None, f"create_{kind}"):
            return
        raise ProvisionError(f"ownership journal is inconsistent for the {kind} identity")
    if metadata is None or str(metadata.get("object_id")) != str(recorded_id):
        raise ProvisionError(f"ownership conflict: recorded {kind} identity does not match ClickHouse")


def _verify_exact_managed_access(client: ClickHouseHTTP, dataset: dict[str, Any]) -> None:
    role = dataset["mcp_role"]
    user = dataset["mcp_user"]
    role_grants = client.query(
        "SELECT user_name, role_name, access_type, database, table, column, "
        "is_partial_revoke, grant_option FROM system.grants "
        f"WHERE role_name = {_quote_string(role)} OR user_name = {_quote_string(user)}"
    )
    expected = {
        "user_name": None,
        "role_name": role,
        "access_type": "SELECT",
        "database": dataset["database"],
        "table": dataset["table"],
        "column": None,
        "is_partial_revoke": 0,
        "grant_option": 0,
    }
    if len(role_grants) != 1 or any(role_grants[0].get(key) != value for key, value in expected.items()):
        raise ProvisionError("managed ClickHouse role has unexpected or ineffective grants")
    assignments = client.query(
        "SELECT user_name, role_name, granted_role_name, granted_role_is_default, with_admin_option "
        "FROM system.role_grants "
        f"WHERE user_name = {_quote_string(user)} OR role_name = {_quote_string(role)} "
        f"OR granted_role_name = {_quote_string(role)}"
    )
    expected_assignment = {
        "user_name": user,
        "role_name": None,
        "granted_role_name": role,
        "granted_role_is_default": 1,
        "with_admin_option": 0,
    }
    if len(assignments) != 1 or any(assignments[0].get(key) != value for key, value in expected_assignment.items()):
        raise ProvisionError("managed ClickHouse user has unexpected role assignments")


def _verify_reference_access(client: ClickHouseHTTP, dataset: dict[str, Any]) -> None:
    user = dataset["mcp_user"]
    assignments = client.query(
        "SELECT user_name, role_name, granted_role_name, with_admin_option FROM system.role_grants "
        f"WHERE user_name = {_quote_string(user)} OR role_name = {_quote_string(user)}"
    )
    user_rows = client.query(
        "SELECT user_name, role_name, access_type, database, table, column, "
        "is_partial_revoke, grant_option FROM system.grants "
        f"WHERE user_name = {_quote_string(user)}"
    )
    role_names = {
        str(row["granted_role_name"])
        for row in assignments
        if row.get("user_name") == user
    }
    if any(
        row.get("user_name") == user and row.get("with_admin_option") != 0
        for row in assignments
    ):
        raise ProvisionError("reference user has a role assignment with ADMIN OPTION")
    if any(row.get("role_name") == user for row in assignments):
        raise ProvisionError("reference user is inherited by a role and cannot be safely scoped")
    effective_rows = list(user_rows)
    for role in role_names:
        effective_rows.extend(
            client.query(
                "SELECT user_name, role_name, access_type, database, table, column, "
                "is_partial_revoke, grant_option FROM system.grants "
                f"WHERE role_name = {_quote_string(role)}"
            )
        )
        nested = client.query(
            "SELECT granted_role_name FROM system.role_grants "
            f"WHERE role_name = {_quote_string(role)}"
        )
        if nested:
            raise ProvisionError("reference user uses nested roles; verify direct least-privilege grants manually")
    expected_scope = (dataset["database"], dataset["table"])
    if not effective_rows or any(
        row.get("access_type") != "SELECT"
        or row.get("database") != expected_scope[0]
        or row.get("table") != expected_scope[1]
        or row.get("column") not in (None, "")
        or row.get("is_partial_revoke") != 0
        or row.get("grant_option") != 0
        for row in effective_rows
    ):
        raise ProvisionError("reference ClickHouse credentials have privileges outside the target table")


def _verify_main_user_cannot_read_unexposed_datasets(
    client: ClickHouseHTTP,
    main_user: str,
    datasets: list[dict[str, Any]],
    journal: dict[str, Any] | None = None,
) -> None:
    unexposed_targets = {
        (dataset["database"], dataset["table"])
        for dataset in datasets
        if not dataset["enabled"] or not dataset["main_exposed"]
    }
    exposed_targets = {
        (dataset["database"], dataset["table"])
        for dataset in datasets
        if dataset["enabled"] and dataset["main_exposed"]
    }
    journal = journal or {}
    historical_targets = list(journal.get("main_access_targets", []))
    historical_targets.extend(
        record
        for record in journal.get("datasets", {}).values()
        if isinstance(record, dict)
    )
    for target in historical_targets:
        if not isinstance(target, dict):
            raise ProvisionError("ownership journal has invalid main-access target records")
        database = _identifier(target.get("database"), "ownership journal main-access database")
        table = _identifier(target.get("table"), "ownership journal main-access table")
        pair = (database, table)
        if pair not in exposed_targets:
            unexposed_targets.add(pair)
    targets = sorted(unexposed_targets)
    if not targets:
        return

    if _single_row(
        client,
        "SELECT name FROM system.users "
        f"WHERE name = {_quote_string(main_user)}",
    ) is None:
        raise ProvisionError("main MCP ClickHouse user is missing; cannot verify dataset isolation")

    roles: set[str] = set()
    direct_assignments = client.query(
        "SELECT user_name, role_name, granted_role_name FROM system.role_grants "
        f"WHERE user_name = {_quote_string(main_user)}"
    )
    roles.update(
        str(row["granted_role_name"])
        for row in direct_assignments
        if isinstance(row.get("granted_role_name"), str)
    )

    checked_roles: set[str] = set()
    while roles - checked_roles:
        current = sorted(roles - checked_roles)
        checked_roles.update(current)
        quoted_roles = ", ".join(_quote_string(role) for role in current)
        nested_assignments = client.query(
            "SELECT user_name, role_name, granted_role_name FROM system.role_grants "
            f"WHERE role_name IN ({quoted_roles})"
        )
        roles.update(
            str(row["granted_role_name"])
            for row in nested_assignments
            if row.get("role_name") in current and isinstance(row.get("granted_role_name"), str)
        )

    grants_filter = f"user_name = {_quote_string(main_user)}"
    if roles:
        quoted_roles = ", ".join(_quote_string(role) for role in sorted(roles))
        grants_filter += f" OR role_name IN ({quoted_roles})"
    grants = client.query(
        "SELECT user_name, role_name, access_type, database, table, column, is_partial_revoke "
        f"FROM system.grants WHERE {grants_filter}"
    )

    for grant in grants:
        if grant.get("is_partial_revoke") == 1 or grant.get("access_type") not in {"SELECT", "ALL"}:
            continue
        if grant.get("user_name") not in (None, main_user) and grant.get("role_name") not in roles:
            continue
        database = grant.get("database")
        table = grant.get("table")
        for target_database, target_table in targets:
            database_matches = database in (None, "", "*", target_database)
            table_matches = table in (None, "", "*", target_table)
            if database_matches and table_matches:
                raise ProvisionError(
                    "main MCP ClickHouse user can read an unexposed dataset; "
                    "narrow its existing direct or inherited grants before provisioning"
                )


def _restricted_client(runtime_dataset: dict[str, Any], admin: AdminConnection) -> ClickHouseHTTP:
    return ClickHouseHTTP(
        AdminConnection(
            host=admin.host,
            port=admin.port,
            username=runtime_dataset["clickhouse_user"],
            password=runtime_dataset["clickhouse_password"],
            timeout_seconds=admin.timeout_seconds,
        )
    )


class DatasetProvisioner:
    def __init__(
        self,
        client: ClickHouseHTTP,
        admin: AdminConnection,
        schema: CanonicalSchema,
        journal_path: Path,
        journal: dict[str, Any],
        *,
        require_root: bool,
        main_user: str = "rawbbit_mcp",
    ) -> None:
        self.client = client
        self.admin = admin
        self.schema = schema
        self.journal_path = journal_path
        self.journal = journal
        self.require_root = require_root
        self.main_user = main_user
        self.changed = False

    def save(self) -> None:
        _atomic_write_json(self.journal_path, self.journal, require_root=self.require_root)

    def _recorded_entity(self, kind: str, name: str) -> tuple[dict[str, Any], str] | None:
        key = "role" if kind == "role" else "user"
        object_key = "role" if kind == "role" else "user"
        for record in self.journal["datasets"].values():
            if isinstance(record, dict) and record.get(key) == name:
                object_id = record.get("object_ids", {}).get(object_key)
                if object_id is not None:
                    return record, str(object_id)
        return None

    def _identity_object_id(self, kind: str, name: str) -> str:
        owned = self._recorded_entity(kind, name)
        if owned is None:
            raise ProvisionError("cross-dataset access references an unowned identity")
        metadata = _single_row(
            self.client,
            f"SELECT toString(id) AS object_id FROM system.{kind}s WHERE name = {_quote_string(name)}",
        )
        if metadata is None or str(metadata.get("object_id")) != owned[1]:
            raise ProvisionError("cross-dataset access references a stale managed identity")
        return owned[1]

    def _validate_reconcilable_access(self, dataset: dict[str, Any]) -> None:
        user = dataset["mcp_user"]
        role = dataset["mcp_role"]
        assignments = self.client.query(
            "SELECT user_name, role_name, granted_role_name FROM system.role_grants "
            f"WHERE user_name = {_quote_string(user)} OR role_name = {_quote_string(role)} "
            f"OR granted_role_name = {_quote_string(role)}"
        )
        for assignment in assignments:
            is_expected = (
                assignment.get("user_name") == user
                and assignment.get("role_name") is None
                and assignment.get("granted_role_name") == role
            )
            if is_expected:
                continue
            assigned_role = assignment.get("granted_role_name")
            grantee_user = assignment.get("user_name")
            grantee_role = assignment.get("role_name")
            if assigned_role == role and grantee_user is not None:
                if grantee_user != user and self._recorded_entity("user", str(grantee_user)) is None:
                    raise ProvisionError("managed role is assigned to an unowned ClickHouse user")
                if grantee_user != user:
                    self._identity_object_id("user", str(grantee_user))
            elif assigned_role == role and grantee_role is not None:
                if self._recorded_entity("role", str(grantee_role)) is None:
                    raise ProvisionError("managed role is inherited by an unowned ClickHouse role")
                self._identity_object_id("role", str(grantee_role))
            elif grantee_user == user and assigned_role is not None:
                self._identity_object_id("role", str(assigned_role))
            elif grantee_role == role and assigned_role is not None:
                self._identity_object_id("role", str(assigned_role))
            else:
                raise ProvisionError("ClickHouse role metadata is not safely reconcilable")

        grants = self.client.query(
            "SELECT user_name, role_name, database, table FROM system.grants "
            f"WHERE user_name = {_quote_string(user)} OR role_name = {_quote_string(role)}"
        )
        for grant in grants:
            if grant.get("role_name") == role or grant.get("user_name") == user:
                database = str(grant.get("database") or "*")
                table = str(grant.get("table") or "*")
                if database != "*":
                    _identifier(database, "managed grant database")
                if table != "*":
                    _identifier(table, "managed grant table")
            else:
                raise ProvisionError("ClickHouse grants include an unowned principal")

    def _validate_pending_operation(self, dataset_id: str, record: dict[str, Any]) -> None:
        action = record.get("pending_action")
        if action is None:
            return
        if not isinstance(action, str) or not action.startswith("create_"):
            raise ProvisionError(
                f"recovery required: dataset {dataset_id} has an interrupted managed operation; "
                "preserve ClickHouse and the journal, then complete an operator-reviewed recovery"
            )
        kind = action.removeprefix("create_")
        if kind not in JOURNAL_OBJECT_ID_FIELDS:
            raise ProvisionError(f"recovery required: dataset {dataset_id} has an unsupported pending creation")
        pending_dataset = {
            "database": record["database"],
            "table": record["table"],
            "mcp_role": record["role"],
            "mcp_user": record["user"],
        }
        metadata = _object_metadata(self.client, pending_dataset, kind)
        # A creation is safe to retry only when the object is still absent. If it
        # now exists, continue only when its identity was durably journaled.
        _assert_recorded_id(record, kind, metadata)

    def _mutate(
        self,
        record: dict[str, Any],
        action: str,
        sql: str,
        *,
        updates: dict[str, Any] | None = None,
    ) -> None:
        record["state"] = "pending"
        record["pending_action"] = action
        self.save()
        self.client.execute(sql)
        self.changed = True
        if updates:
            record.update(updates)
        record["pending_action"] = None
        self.save()

    def preflight(self, datasets: list[dict[str, Any]], runtime_by_id: dict[str, dict[str, Any]]) -> None:
        journal_datasets = self.journal["datasets"]
        enabled_ids = {dataset["id"] for dataset in datasets if dataset["enabled"]}
        if enabled_ids != set(runtime_by_id):
            raise ProvisionError("enabled provisioning and runtime dataset IDs must match exactly")
        managed_names: dict[str, str] = {}
        for dataset_id, record in journal_datasets.items():
            if not isinstance(record, dict):
                raise ProvisionError("ownership journal contains an invalid dataset record")
            self._validate_pending_operation(dataset_id, record)
            for key in ("database", "role", "user"):
                name = record.get(key)
                if isinstance(name, str):
                    managed_names[f"{key}:{name}"] = dataset_id
            if isinstance(record.get("database"), str) and isinstance(record.get("table"), str):
                managed_names[f"table:{record['database']}.{record['table']}"] = dataset_id

        for dataset in datasets:
            if not dataset["enabled"]:
                continue
            runtime = runtime_by_id.get(dataset["id"])
            if runtime is None:
                raise ProvisionError("every enabled provisioned dataset must exist in the runtime registry")
            for key, runtime_key in (
                ("database", "database"),
                ("table", "table"),
                ("mcp_user", "clickhouse_user"),
                ("endpoint_path", "endpoint_path"),
            ):
                if dataset[key] != runtime.get(runtime_key, "events" if runtime_key == "table" else None):
                    raise ProvisionError("provisioning and runtime registry metadata do not match")
            if dataset["mode"] == "managed" and dataset.get("mcp_password") != runtime.get("clickhouse_password"):
                raise ProvisionError("managed provisioning and runtime ClickHouse passwords do not match")
            if dataset["main_exposed"] != runtime.get("main_exposed"):
                raise ProvisionError("provisioning and runtime main-exposure settings do not match")
            for name in ("max_query_rows", "max_sample_rows", "max_execution_seconds"):
                if dataset["limits"].get(name) != runtime.get(name):
                    raise ProvisionError("provisioning and runtime query limits do not match")

            record = journal_datasets.get(dataset["id"])
            if record is not None and not _ownership_record_matches(record, dataset):
                raise ProvisionError("ownership journal dataset names or privileges conflict with configuration")
            if record is not None and record.get("schema_fingerprint") != self.schema.fingerprint:
                raise ProvisionError("canonical schema fingerprint changed for an owned dataset; operator review is required")
            for kind in ("database", "table", "role", "user"):
                name = (
                    dataset["database"]
                    if kind == "database"
                    else f"{dataset['database']}.{dataset['table']}"
                    if kind == "table"
                    else dataset["mcp_role"]
                    if kind == "role"
                    else dataset["mcp_user"]
                )
                previous_owner = managed_names.get(f"{kind}:{name}")
                if previous_owner is not None and previous_owner != dataset["id"]:
                    raise ProvisionError("a ClickHouse object name is already recorded under another dataset id")

            db_metadata = _object_metadata(self.client, dataset, "database")
            table_metadata = _object_metadata(self.client, dataset, "table")
            role_metadata = _object_metadata(self.client, dataset, "role")
            user_metadata = _object_metadata(self.client, dataset, "user")
            if dataset["mode"] == "reference":
                if db_metadata is None or table_metadata is None or not _schema_matches(self.client, dataset, self.schema):
                    raise ProvisionError("reference dataset is missing or has an incompatible events table")
                restricted = _restricted_client(runtime, self.admin)
                restricted.query(
                    "SELECT 1 AS ok FROM "
                    f"{_quote_identifier(dataset['database'])}.{_quote_identifier(dataset['table'])} LIMIT 0"
                )
                _verify_reference_access(self.client, dataset)
                continue

            if db_metadata is not None and (
                db_metadata.get("engine") != "Atomic"
                or not _has_stable_uuid(db_metadata.get("object_id"))
            ):
                raise ProvisionError("managed dataset adoption requires an Atomic database with a non-nil UUID")

            if record is None:
                if role_metadata is not None or user_metadata is not None:
                    raise ProvisionError("unrecorded same-named ClickHouse user or role requires manual recovery")
                adoption = dataset["adopt"]
                if db_metadata is not None:
                    if adoption.get("database_uuid") != db_metadata.get("object_id"):
                        raise ProvisionError("pre-existing managed database requires explicit matching adoption UUID")
                elif adoption.get("database_uuid"):
                    raise ProvisionError("adoption database UUID does not exist in ClickHouse")
                if table_metadata is not None:
                    if not _has_stable_uuid(table_metadata.get("object_id")):
                        raise ProvisionError("managed table adoption requires a non-nil ClickHouse UUID")
                    fingerprint = _schema_fingerprint(self.client, dataset, self.schema)
                    if (
                        adoption.get("table_uuid") != table_metadata.get("object_id")
                        or adoption.get("schema_fingerprint") != fingerprint
                    ):
                        raise ProvisionError(
                            "pre-existing managed table requires explicit matching adoption UUID and schema fingerprint"
                        )
                elif adoption.get("table_uuid") or adoption.get("schema_fingerprint"):
                    raise ProvisionError("adoption table identity does not exist in ClickHouse")
                if table_metadata is None and not dataset["create_table"]:
                    raise ProvisionError("managed events table is missing and create_table is disabled")
            else:
                for kind, metadata in (
                    ("database", db_metadata),
                    ("table", table_metadata),
                    ("role", role_metadata),
                    ("user", user_metadata),
                ):
                    _assert_recorded_id(record, kind, metadata)
                if dataset["create_table"] is False and table_metadata is None:
                    raise ProvisionError("owned managed events table is missing")
                if table_metadata is not None:
                    _schema_fingerprint(self.client, dataset, self.schema)
                if role_metadata is None or user_metadata is None:
                    if record.get("state") != "pending":
                        raise ProvisionError("an applied managed dataset is missing an owned ClickHouse access identity")
                else:
                    self._validate_reconcilable_access(dataset)
                if user_metadata is not None and record.get("password_revision") == dataset["password_revision"]:
                    try:
                        _restricted_client(runtime, self.admin).query(
                            "SELECT 1 AS ok FROM "
                            f"{_quote_identifier(dataset['database'])}.{_quote_identifier(dataset['table'])} LIMIT 0"
                        )
                    except ProvisionError as exc:
                        raise ProvisionError(
                            "managed ClickHouse credential does not match its recorded password revision; "
                            "restore the prior secret or increment password_revision for an intentional rotation"
                        ) from exc

        _verify_main_user_cannot_read_unexposed_datasets(
            self.client, self.main_user, datasets, self.journal
        )

    def _record_for(self, dataset: dict[str, Any]) -> dict[str, Any]:
        dataset_id = dataset["id"]
        record = self.journal["datasets"].get(dataset_id)
        if record is None:
            record = {
                "database": dataset["database"],
                "table": dataset["table"],
                "role": dataset["mcp_role"],
                "user": dataset["mcp_user"],
                "privileges": ["SELECT"],
                "schema_fingerprint": self.schema.fingerprint,
                "object_ids": {},
                "password_revision": dataset["password_revision"],
                "state": "pending",
                "pending_action": None,
            }
            self.journal["datasets"][dataset_id] = record
            adoption = dataset["adopt"]
            db_metadata = _object_metadata(self.client, dataset, "database")
            if db_metadata is not None and adoption.get("database_uuid") == db_metadata.get("object_id"):
                record["object_ids"]["database"] = str(db_metadata["object_id"])
            table_metadata = _object_metadata(self.client, dataset, "table")
            if table_metadata is not None and adoption.get("table_uuid") == table_metadata.get("object_id"):
                record["object_ids"]["table"] = str(table_metadata["object_id"])
            self.save()
            self.changed = True
        return record

    def _ensure_database(self, dataset: dict[str, Any], record: dict[str, Any]) -> None:
        metadata = _object_metadata(self.client, dataset, "database")
        _assert_recorded_id(record, "database", metadata)
        if metadata is not None:
            return
        record["pending_action"] = "create_database"
        self.save()
        self.client.execute(f"CREATE DATABASE {_quote_identifier(dataset['database'])} ENGINE = Atomic")
        self.changed = True
        metadata = _object_metadata(self.client, dataset, "database")
        if metadata is None:
            raise ProvisionError("ClickHouse did not confirm database creation")
        record["object_ids"]["database"] = str(metadata["object_id"])
        record["pending_action"] = None
        self.save()

    def _ensure_table(self, dataset: dict[str, Any], record: dict[str, Any]) -> None:
        metadata = _object_metadata(self.client, dataset, "table")
        _assert_recorded_id(record, "table", metadata)
        if metadata is not None:
            if not _schema_matches(self.client, dataset, self.schema):
                raise ProvisionError("owned managed events table no longer matches the canonical schema")
            return
        if not dataset["create_table"]:
            raise ProvisionError("managed events table is missing and create_table is disabled")
        record["pending_action"] = "create_table"
        self.save()
        create_sql = re.sub(
            r"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+analytics\.events",
            f"CREATE TABLE IF NOT EXISTS {_quote_identifier(dataset['database'])}.{_quote_identifier(dataset['table'])}",
            self.schema.create_table_sql,
            count=1,
            flags=re.IGNORECASE,
        )
        if "analytics.events" in create_sql:
            raise ProvisionError("failed to bind canonical schema to the configured dataset")
        self.client.execute(create_sql)
        self.changed = True
        metadata = _object_metadata(self.client, dataset, "table")
        if metadata is None or not _schema_matches(self.client, dataset, self.schema):
            raise ProvisionError("ClickHouse did not confirm canonical events table creation")
        record["object_ids"]["table"] = str(metadata["object_id"])
        record["pending_action"] = None
        self.save()

    def _ensure_access_identity(self, dataset: dict[str, Any], record: dict[str, Any]) -> None:
        role_metadata = _object_metadata(self.client, dataset, "role")
        user_metadata = _object_metadata(self.client, dataset, "user")
        _assert_recorded_id(record, "role", role_metadata)
        _assert_recorded_id(record, "user", user_metadata)
        if role_metadata is None:
            record["pending_action"] = "create_role"
            self.save()
            self.client.execute(f"CREATE ROLE {_quote_identifier(dataset['mcp_role'])}")
            self.changed = True
            role_metadata = _object_metadata(self.client, dataset, "role")
            if role_metadata is None:
                raise ProvisionError("ClickHouse did not confirm role creation")
            record["object_ids"]["role"] = str(role_metadata["object_id"])
            record["pending_action"] = None
            self.save()
        if user_metadata is None:
            record["pending_action"] = "create_user"
            self.save()
            password = _required_string(dataset.get("mcp_password"), "managed MCP password")
            user_settings = self._user_settings(dataset)
            self.client.execute(
                f"CREATE USER {_quote_identifier(dataset['mcp_user'])} "
                f"IDENTIFIED WITH sha256_password BY {_quote_string(password)} {user_settings}"
            )
            self.changed = True
            user_metadata = _object_metadata(self.client, dataset, "user")
            if user_metadata is None:
                raise ProvisionError("ClickHouse did not confirm user creation")
            record["object_ids"]["user"] = str(user_metadata["object_id"])
            record["password_revision"] = dataset["password_revision"]
            record["pending_action"] = None
            self.save()
        else:
            if not self._user_settings_are_applied(dataset):
                self._mutate(
                    record,
                    "alter_user_settings",
                    f"ALTER USER {_quote_identifier(dataset['mcp_user'])} {self._user_settings(dataset)}",
                )
            if record.get("password_revision") != dataset["password_revision"]:
                password = _required_string(dataset.get("mcp_password"), "managed MCP password")
                self._mutate(
                    record,
                    "rotate_user_password",
                    f"ALTER USER {_quote_identifier(dataset['mcp_user'])} "
                    f"IDENTIFIED WITH sha256_password BY {_quote_string(password)}",
                    updates={"password_revision": dataset["password_revision"]},
                )
        self._reconcile_role_and_user(dataset, record)

    @staticmethod
    def _user_settings(dataset: dict[str, Any]) -> str:
        limits = dataset["limits"]
        parts = ["readonly = 1"]
        if "max_execution_seconds" in limits:
            parts.append(f"max_execution_time = {limits['max_execution_seconds']}")
        if "max_query_rows" in limits:
            parts.append(f"max_result_rows = {limits['max_query_rows']}")
            parts.append("result_overflow_mode = 'break'")
        return "SETTINGS " + ", ".join(parts)

    @staticmethod
    def _expected_user_settings(dataset: dict[str, Any]) -> dict[str, str]:
        limits = dataset["limits"]
        expected = {"readonly": "1"}
        if "max_execution_seconds" in limits:
            expected["max_execution_time"] = str(limits["max_execution_seconds"])
        if "max_query_rows" in limits:
            expected["max_result_rows"] = str(limits["max_query_rows"])
            expected["result_overflow_mode"] = "break"
        return expected

    def _user_settings_are_applied(self, dataset: dict[str, Any]) -> bool:
        rows = self.client.query(
            "SELECT setting_name, value FROM system.settings_profile_elements "
            f"WHERE user_name = {_quote_string(dataset['mcp_user'])} AND role_name IS NULL"
        )
        actual = {str(row["setting_name"]): str(row["value"]) for row in rows}
        return all(actual.get(name) == value for name, value in self._expected_user_settings(dataset).items())

    def _reconcile_role_and_user(self, dataset: dict[str, Any], record: dict[str, Any]) -> None:
        role = dataset["mcp_role"]
        user = dataset["mcp_user"]
        database = dataset["database"]
        table = dataset["table"]
        assigned = self.client.query(
            "SELECT user_name, role_name, granted_role_name, granted_role_is_default, with_admin_option "
            "FROM system.role_grants "
            f"WHERE user_name = {_quote_string(user)} OR role_name = {_quote_string(role)} "
            f"OR granted_role_name = {_quote_string(role)}"
        )
        managed_roles = {
            record.get("role")
            for record in self.journal["datasets"].values()
            if isinstance(record, dict) and isinstance(record.get("role"), str)
        }
        for assignment in assigned:
            is_expected = (
                assignment.get("user_name") == user
                and assignment.get("granted_role_name") == role
                and assignment.get("role_name") is None
                and assignment.get("with_admin_option") == 0
            )
            if is_expected:
                continue
            assigned_role = assignment.get("granted_role_name")
            grantee_user = assignment.get("user_name")
            grantee_role = assignment.get("role_name")
            if assigned_role in managed_roles:
                managed_record = next(
                    record
                    for record in self.journal["datasets"].values()
                    if isinstance(record, dict) and record.get("role") == assigned_role
                )
                role_metadata = _single_row(
                    self.client,
                    "SELECT toString(id) AS object_id FROM system.roles "
                    f"WHERE name = {_quote_string(assigned_role)}",
                )
                if (
                    role_metadata is None
                    or str(role_metadata.get("object_id")) != str(managed_record.get("object_ids", {}).get("role"))
                ):
                    raise ProvisionError("unexpected role assignment does not match a recorded managed identity")
                if grantee_user is not None and self._recorded_entity("user", str(grantee_user)) is not None:
                    self._identity_object_id("user", str(grantee_user))
                    grantee = _quote_identifier(str(grantee_user))
                elif grantee_role is not None and self._recorded_entity("role", str(grantee_role)) is not None:
                    self._identity_object_id("role", str(grantee_role))
                    grantee = _quote_identifier(str(grantee_role))
                else:
                    raise ProvisionError("managed role is assigned to an unowned ClickHouse principal")
                self._mutate(
                    record,
                    "revoke_managed_role_assignment",
                    f"REVOKE {_quote_identifier(assigned_role)} FROM {grantee}",
                )
                continue
            raise ProvisionError("managed user or role has an unowned role assignment; resolve manually")
        remaining_assignments = self.client.query(
            "SELECT user_name, role_name, granted_role_name, granted_role_is_default, with_admin_option "
            "FROM system.role_grants "
            f"WHERE user_name = {_quote_string(user)} OR role_name = {_quote_string(role)} "
            f"OR granted_role_name = {_quote_string(role)}"
        )
        if any(
            row.get("user_name") == user
            and row.get("role_name") is None
            and row.get("granted_role_name") == role
            and row.get("with_admin_option") != 0
            for row in remaining_assignments
        ):
            raise ProvisionError("managed role assignment retained ADMIN OPTION after revocation")
        role_assignment = next(
            (
                row
                for row in remaining_assignments
                if row.get("user_name") == user
                and row.get("role_name") is None
                and row.get("granted_role_name") == role
                and row.get("with_admin_option") == 0
            ),
            None,
        )
        if role_assignment is None:
            self._mutate(
                record,
                "grant_dataset_role_to_user",
                f"GRANT {_quote_identifier(role)} TO {_quote_identifier(user)}",
            )
        user_metadata = _single_row(
            self.client,
            "SELECT default_roles_all, default_roles_list, default_roles_except "
            "FROM system.users "
            f"WHERE name = {_quote_string(user)}",
        )
        default_role_is_exact = (
            user_metadata is not None
            and user_metadata.get("default_roles_all") == 0
            and user_metadata.get("default_roles_list") == [role]
            and user_metadata.get("default_roles_except") == []
        )
        if not default_role_is_exact:
            self._mutate(
                record,
                "set_default_dataset_role",
                f"SET DEFAULT ROLE {_quote_identifier(role)} TO {_quote_identifier(user)}",
            )

        grants = self.client.query(
            "SELECT user_name, role_name, access_type, database, table, column, "
            "is_partial_revoke, grant_option FROM system.grants "
            f"WHERE role_name = {_quote_string(role)} OR user_name = {_quote_string(user)}"
        )
        target_scope = (database, table)
        extra_scopes: set[tuple[str, str, str]] = set()
        for grant in grants:
            is_expected = (
                grant.get("user_name") is None
                and grant.get("role_name") == role
                and grant.get("access_type") == "SELECT"
                and (grant.get("database"), grant.get("table")) == target_scope
                and grant.get("column") in (None, "")
                and grant.get("is_partial_revoke") == 0
                and grant.get("grant_option") == 0
            )
            if not is_expected:
                grant_database = str(grant.get("database") or "*")
                grant_table = str(grant.get("table") or "*")
                if grant_database != "*":
                    _identifier(grant_database, "managed grant database")
                if grant_table != "*":
                    _identifier(grant_table, "managed grant table")
                if grant.get("role_name") == role:
                    principal = role
                elif grant.get("user_name") == user:
                    principal = user
                else:
                    raise ProvisionError("managed grant metadata contains an unexpected principal")
                extra_scopes.add((principal, grant_database, grant_table))
        for principal, grant_database, grant_table in sorted(extra_scopes):
            scope = "*.*" if grant_database == "*" else f"{_quote_identifier(grant_database)}.*" if grant_table == "*" else f"{_quote_identifier(grant_database)}.{_quote_identifier(grant_table)}"
            self._mutate(
                record,
                "revoke_obsolete_managed_grant",
                f"REVOKE ALL ON {scope} FROM {_quote_identifier(principal)}",
            )
        remaining = self.client.query(
            "SELECT access_type, database, table, is_partial_revoke FROM system.grants "
            f"WHERE role_name = {_quote_string(role)}"
        )
        # system.grants has additional columns; compare the stable privilege scope.
        stable = [
            (row.get("access_type"), row.get("database"), row.get("table"), row.get("is_partial_revoke"))
            for row in remaining
        ]
        if stable != [("SELECT", database, table, 0)]:
            self._mutate(
                record,
                "grant_dataset_select",
                f"GRANT SELECT ON {_quote_identifier(database)}.{_quote_identifier(table)} "
                f"TO {_quote_identifier(role)}",
            )
        _verify_exact_managed_access(self.client, dataset)

    def provision(self, datasets: list[dict[str, Any]], runtime_by_id: dict[str, dict[str, Any]]) -> None:
        for dataset in datasets:
            if not dataset["enabled"] or dataset["mode"] == "reference":
                continue
            record = self._record_for(dataset)
            self._ensure_database(dataset, record)
            self._ensure_table(dataset, record)
            self._ensure_access_identity(dataset, record)
            _schema_fingerprint(self.client, dataset, self.schema)
            _verify_exact_managed_access(self.client, dataset)
            restricted = _restricted_client(runtime_by_id[dataset["id"]], self.admin)
            restricted.query(
                "SELECT 1 AS ok FROM "
                f"{_quote_identifier(dataset['database'])}.{_quote_identifier(dataset['table'])} LIMIT 0"
            )
            if (
                record.get("state") != "applied"
                or record.get("pending_action") is not None
                or record.get("schema_fingerprint") != self.schema.fingerprint
            ):
                record["state"] = "applied"
                record["pending_action"] = None
                record["schema_fingerprint"] = self.schema.fingerprint
                self.save()
                self.changed = True

        existing_targets = {
            (target["database"], target["table"])
            for target in self.journal.get("main_access_targets", [])
        }
        existing_targets.update(
            (record["database"], record["table"])
            for record in self.journal["datasets"].values()
            if isinstance(record, dict)
        )
        existing_targets.update((dataset["database"], dataset["table"]) for dataset in datasets)
        serialized_targets = [
            {"database": database, "table": table}
            for database, table in sorted(existing_targets)
        ]
        if serialized_targets != self.journal.get("main_access_targets", []):
            self.journal["main_access_targets"] = serialized_targets
            self.save()
            self.changed = True


def run(
    config_path: Path,
    runtime_path: Path,
    schema_path: Path,
    journal_path: Path,
    *,
    preflight_only: bool = False,
    require_root: bool = True,
) -> bool:
    if require_root and os.geteuid() != 0:
        raise ProvisionError("dataset provisioning must run as root")
    config = _secure_read_json(config_path, require_root=require_root)
    runtime = _secure_read_json(runtime_path, require_root=False)
    admin, datasets = _validate_registry(config)
    runtime_by_id = _validate_runtime(runtime, main_database=config["main_database"])
    schema = parse_canonical_schema(schema_path)
    journal = _read_journal(journal_path, require_root=require_root)
    client = ClickHouseHTTP(admin)
    provisioner = DatasetProvisioner(
        client,
        admin,
        schema,
        journal_path,
        journal,
        require_root=require_root,
        main_user=config["main_user"],
    )
    provisioner.preflight(datasets, runtime_by_id)
    if preflight_only:
        return False
    provisioner.provision(datasets, runtime_by_id)
    return provisioner.changed


def main() -> int:
    parser = argparse.ArgumentParser(description="Provision optional Rawbbit ClickHouse datasets safely")
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--runtime-config", required=True, type=Path)
    parser.add_argument("--schema", required=True, type=Path)
    parser.add_argument("--journal", required=True, type=Path)
    parser.add_argument("--preflight-only", action="store_true")
    args = parser.parse_args()
    try:
        changed = run(args.config, args.runtime_config, args.schema, args.journal, preflight_only=args.preflight_only)
    except ProvisionError as exc:
        print(f"dataset provisioner: {exc}", file=__import__("sys").stderr)
        return 1
    if args.preflight_only:
        print("dataset provisioner: preflight passed")
    else:
        print("dataset provisioner: configuration reconciled" if changed else "dataset provisioner: configuration unchanged")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
