from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from quickstart.vm_rawbbit_two.clickhouse.provision_datasets import (
    AdminConnection,
    ClickHouseHTTP,
    ProvisionError,
    _assert_recorded_id,
    _atomic_write_json,
    _secure_read_json,
    _read_journal,
    _validate_registry,
    _validate_runtime,
    _verify_exact_managed_access,
    _verify_main_user_cannot_read_unexposed_datasets,
    _verify_reference_access,
    DatasetProvisioner,
    parse_canonical_schema,
)


ROOT = Path(__file__).resolve().parents[4]
SCHEMA = ROOT / "quickstart/vm_rawbbit_two/clickhouse/schema_analytics_events.sql"
REFERENCE_SCHEMA = ROOT / "clickhouse/schema__analytics_events.sql"


def base_dataset(**updates):
    value = {
        "id": "team_a",
        "enabled": True,
        "mode": "managed",
        "endpoint_path": "/datasets/team_a/mcp",
        "database": "team_a",
        "table": "events",
        "create_table": True,
        "main_exposed": True,
        "mcp_user": "rawbbit_mcp_team_a",
        "mcp_role": "rawbbit_role_team_a",
        "mcp_password": "synthetic-clickhouse-password",
        "password_revision": 1,
        "limits": {"max_query_rows": 500, "max_sample_rows": 50, "max_execution_seconds": 30},
    }
    value.update(updates)
    return value


def base_runtime(**updates):
    value = {
        "id": "team_a",
        "enabled": True,
        "endpoint_path": "/datasets/team_a/mcp",
        "database": "team_a",
        "table": "events",
        "main_exposed": True,
        "clickhouse_user": "rawbbit_mcp_team_a",
        "clickhouse_password": "synthetic-clickhouse-password",
        "bearer_tokens": {"agent": "synthetic-bearer-token"},
        "max_query_rows": 500,
        "max_sample_rows": 50,
        "max_execution_seconds": 30,
    }
    value.update(updates)
    return value


def base_registry(**dataset_updates):
    return {
        "version": 1,
        "main_database": "analytics",
        "main_user": "rawbbit_mcp",
        "admin": {
            "host": "127.0.0.1",
            "port": 8123,
            "username": "admin",
            "password": "synthetic-admin-password",
            "timeout_seconds": 15,
        },
        "datasets": [base_dataset(**dataset_updates)],
    }


def _normalized_table_ddl(path: Path) -> str:
    import re

    sql = path.read_text(encoding="utf-8")
    match = re.search(
        r"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+analytics\.events\s*.*?"
        r"ORDER\s+BY\s*\(.*?\)(?:\s*;|\s*\Z)",
        sql,
        re.IGNORECASE | re.DOTALL,
    )
    assert match, f"canonical analytics.events DDL is missing from {path}"
    return re.sub(r"\s+", "", match.group(0)).rstrip(";").lower()


def test_canonical_managed_schema_and_reference_copy_stay_in_sync() -> None:
    parsed = parse_canonical_schema(SCHEMA)
    assert len(parsed.columns) == 26
    assert parsed.engine == "MergeTree"
    assert parsed.fingerprint
    assert _normalized_table_ddl(SCHEMA) == _normalized_table_ddl(REFERENCE_SCHEMA)


@pytest.mark.parametrize(
    "dataset_updates",
    [
        {"id": "Team-A"},
        {"database": "team_a; DROP TABLE events"},
        {"endpoint_path": "/datasets/team_a/../mcp"},
        {"endpoint_path": "/datasets/team_a/mcp?admin=1"},
        {"mcp_user": "mcp.team_a"},
        {"mode": "managed", "create_table": True, "mcp_password": ""},
        {"mode": "reference", "create_table": True},
    ],
)
def test_registry_rejects_unsafe_or_inconsistent_config(dataset_updates) -> None:
    config = base_registry(**dataset_updates)
    if dataset_updates.get("mode") == "reference":
        config["datasets"][0].pop("mcp_password", None)
    with pytest.raises(ProvisionError):
        _validate_registry(config)


def test_registry_refuses_non_loopback_admin_and_unknown_fields() -> None:
    config = base_registry()
    config["admin"]["host"] = "clickhouse.example.com"
    with pytest.raises(ProvisionError, match="localhost"):
        _validate_registry(config)

    config = base_registry()
    config["datasets"][0]["admin_password"] = "must-not-be-accepted"
    with pytest.raises(ProvisionError, match="unsupported fields"):
        _validate_registry(config)

    for invalid in (
        {**base_registry(), "unexpected": True},
        {**base_registry(), "version": True},
        base_registry(limits={"max_rowz": 100}),
        base_registry(adopt={"admin_password": "synthetic-secret"}),
        base_registry(mode={"managed": True}),
    ):
        with pytest.raises(ProvisionError):
            _validate_registry(invalid)


def test_registry_rejects_dataset_database_shared_with_main_endpoint() -> None:
    with pytest.raises(ProvisionError, match="differ from the main ClickHouse database"):
        _validate_registry(base_registry(database="analytics"))


def test_runtime_rejects_duplicate_tokens_and_mismatched_fields() -> None:
    with pytest.raises(ProvisionError, match="unique"):
        _validate_runtime(
            {
                "version": 1,
                "datasets": [
                    base_runtime(id="team_a"),
                    base_runtime(
                        id="team_b",
                        endpoint_path="/datasets/team_b/mcp",
                        database="team_b",
                        clickhouse_user="rawbbit_mcp_team_b",
                        bearer_tokens={"agent": "synthetic-bearer-token"},
                    ),
                ],
            }
        )

    malformed = base_runtime(dataset_secret="not-allowed")
    with pytest.raises(ProvisionError, match="unsupported fields"):
        _validate_runtime({"version": 1, "datasets": [malformed]})

    with pytest.raises(ProvisionError, match="version"):
        _validate_runtime({"version": True, "datasets": []})

    duplicate_database = base_runtime(
        id="team_b",
        endpoint_path="/datasets/team_b/mcp",
        clickhouse_user="rawbbit_mcp_team_b",
        bearer_tokens={"agent": "team-b-token"},
    )
    with pytest.raises(ProvisionError, match="distinct ClickHouse database"):
        _validate_runtime({"version": 1, "datasets": [base_runtime(), duplicate_database]})

    with pytest.raises(ProvisionError, match="differ from the main ClickHouse database"):
        _validate_runtime({"version": 1, "datasets": [base_runtime(database="analytics")]})


def test_protected_json_reader_requires_restricted_mode_and_hides_file_contents(tmp_path) -> None:
    path = tmp_path / "provision.json"
    path.write_text('{"password":"synthetic-secret"}', encoding="utf-8")
    path.chmod(0o640)
    with pytest.raises(ProvisionError, match="mode 0600") as exc:
        _secure_read_json(path, require_root=False)
    assert "synthetic-secret" not in str(exc.value)

    path.chmod(0o600)
    assert _secure_read_json(path, require_root=False)["password"] == "synthetic-secret"


def test_atomic_journal_is_private_and_recovery_conflicts_fail_closed(tmp_path) -> None:
    path = tmp_path / "state" / "ownership.json"
    _atomic_write_json(path, {"version": 1, "datasets": {}}, require_root=False)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text(encoding="utf-8")) == {"version": 1, "datasets": {}}

    malformed = {"version": 1, "datasets": {"team_a": {"object_ids": []}}}
    _atomic_write_json(path, malformed, require_root=False)
    with pytest.raises(ProvisionError, match="ownership journal"):
        _read_journal(path, require_root=False)

    for invalid in (
        {"version": True, "datasets": {}},
        {
            "version": 1,
            "datasets": {
                "team_a": {
                    "database": "team_a",
                    "table": "events",
                    "role": "rawbbit_role_team_a",
                    "user": "rawbbit_mcp_team_a",
                    "privileges": ["SELECT"],
                    "schema_fingerprint": "0" * 64,
                    "object_ids": {},
                    "password_revision": 1,
                    "state": [],
                    "pending_action": None,
                }
            },
        },
    ):
        _atomic_write_json(path, invalid, require_root=False)
        with pytest.raises(ProvisionError, match="ownership journal"):
            _read_journal(path, require_root=False)

    with pytest.raises(ProvisionError, match="recovery required"):
        _assert_recorded_id(
            {"object_ids": {}, "pending_action": "create_table"},
            "table",
            {"object_id": "00000000-0000-0000-0000-000000000001"},
        )
    with pytest.raises(ProvisionError, match="identity"):
        _assert_recorded_id(
            {"object_ids": {}, "pending_action": "create_table"},
            "database",
            None,
        )


def test_journal_validates_retained_main_access_targets(tmp_path) -> None:
    path = tmp_path / "ownership.json"
    valid = {
        "version": 1,
        "datasets": {},
        "main_access_targets": [{"database": "team_a", "table": "events"}],
    }
    _atomic_write_json(path, valid, require_root=False)
    assert _read_journal(path, require_root=False) == valid

    for invalid_targets in (
        "not-a-list",
        [{"database": "team_a"}],
        [{"database": "team_a", "table": "events; DROP TABLE x"}],
        [
            {"database": "team_a", "table": "events"},
            {"database": "team_a", "table": "events"},
        ],
    ):
        _atomic_write_json(
            path,
            {"version": 1, "datasets": {}, "main_access_targets": invalid_targets},
            require_root=False,
        )
        with pytest.raises(ProvisionError, match="ownership journal"):
            _read_journal(path, require_root=False)


def test_preflight_stops_on_any_interrupted_managed_mutation(tmp_path) -> None:
    config = base_registry()
    _, datasets = _validate_registry(config)
    runtime = {"version": 1, "datasets": [base_runtime()]}
    runtime_by_id = _validate_runtime(runtime)
    journal = {
        "version": 1,
        "datasets": {
            "team_a": {
                "database": "team_a",
                "table": "events",
                "role": "rawbbit_role_team_a",
                "user": "rawbbit_mcp_team_a",
                "privileges": ["SELECT"],
                "schema_fingerprint": "synthetic-schema-fingerprint",
                "object_ids": {},
                "state": "applied",
                "pending_action": "rotate_user_password",
            }
        },
    }
    provisioner = DatasetProvisioner(
        object(),
        AdminConnection("127.0.0.1", 8123, "admin", "synthetic-password", 10),
        parse_canonical_schema(SCHEMA),
        tmp_path / "ownership.json",
        journal,
        require_root=False,
    )
    with pytest.raises(ProvisionError, match="interrupted managed operation"):
        provisioner.preflight(datasets, runtime_by_id)


@pytest.mark.parametrize(
    "metadata,object_ids,raises",
    [
        ([], {}, False),
        ([{"name": "team_a", "object_id": "00000000-0000-0000-0000-000000000001"}], {"database": "00000000-0000-0000-0000-000000000001"}, False),
        ([{"name": "team_a", "object_id": "00000000-0000-0000-0000-000000000001"}], {}, True),
        ([], {"database": "00000000-0000-0000-0000-000000000001"}, True),
    ],
)
def test_pending_creation_recovery_requires_absence_or_matching_recorded_id(
    tmp_path, metadata, object_ids, raises
) -> None:
    class MetadataClient:
        def query(self, sql):
            if "FROM system.users" in sql:
                return [{"name": "rawbbit_mcp"}]
            if "FROM system.role_grants" in sql or "FROM system.grants" in sql:
                return []
            assert "system.databases" in sql
            return metadata

    record = {
        "database": "team_a",
        "table": "events",
        "role": "rawbbit_role_team_a",
        "user": "rawbbit_mcp_team_a",
        "privileges": ["SELECT"],
        "schema_fingerprint": "0" * 64,
        "object_ids": object_ids,
        "password_revision": 1,
        "state": "pending",
        "pending_action": "create_database",
    }
    provisioner = DatasetProvisioner(
        MetadataClient(),
        AdminConnection("127.0.0.1", 8123, "admin", "synthetic-password", 10),
        parse_canonical_schema(SCHEMA),
        tmp_path / "ownership.json",
        {"version": 1, "datasets": {"team_a": record}},
        require_root=False,
    )
    if raises:
        with pytest.raises(ProvisionError, match="recovery required|ownership conflict"):
            provisioner.preflight([], {})
    else:
        provisioner.preflight([], {})


def test_pending_non_creation_mutation_requires_operator_review(tmp_path) -> None:
    record = {
        "database": "team_a",
        "table": "events",
        "role": "rawbbit_role_team_a",
        "user": "rawbbit_mcp_team_a",
        "privileges": ["SELECT"],
        "schema_fingerprint": "0" * 64,
        "object_ids": {},
        "password_revision": 1,
        "state": "pending",
        "pending_action": "rotate_user_password",
    }
    provisioner = DatasetProvisioner(
        object(),
        AdminConnection("127.0.0.1", 8123, "admin", "synthetic-password", 10),
        parse_canonical_schema(SCHEMA),
        tmp_path / "ownership.json",
        {"version": 1, "datasets": {"team_a": record}},
        require_root=False,
    )
    with pytest.raises(ProvisionError, match="operator-reviewed recovery"):
        provisioner.preflight([], {})


@pytest.mark.parametrize(
    "engine,object_id",
    [
        ("Ordinary", "00000000-0000-0000-0000-000000000001"),
        ("Atomic", "00000000-0000-0000-0000-000000000000"),
    ],
)
def test_managed_adoption_rejects_non_atomic_or_unidentified_database(tmp_path, engine, object_id) -> None:
    config = base_registry(adopt={"database_uuid": object_id})
    _, datasets = _validate_registry(config)
    runtime_by_id = _validate_runtime({"version": 1, "datasets": [base_runtime()]})

    class MetadataClient:
        def query(self, sql):
            if "FROM system.databases" in sql:
                return [{"name": "team_a", "engine": engine, "object_id": object_id}]
            return []

    provisioner = DatasetProvisioner(
        MetadataClient(),
        AdminConnection("127.0.0.1", 8123, "admin", "synthetic-password", 10),
        parse_canonical_schema(SCHEMA),
        tmp_path / "ownership.json",
        {"version": 1, "datasets": {}},
        require_root=False,
    )
    with pytest.raises(ProvisionError, match="Atomic database with a non-nil UUID"):
        provisioner.preflight(datasets, runtime_by_id)


def test_managed_adoption_rejects_table_without_stable_uuid(tmp_path) -> None:
    database_uuid = "00000000-0000-0000-0000-000000000001"
    nil_uuid = "00000000-0000-0000-0000-000000000000"
    config = base_registry(
        adopt={
            "database_uuid": database_uuid,
            "table_uuid": nil_uuid,
            "schema_fingerprint": "a" * 64,
        }
    )
    _, datasets = _validate_registry(config)
    runtime_by_id = _validate_runtime({"version": 1, "datasets": [base_runtime()]})

    class MetadataClient:
        def query(self, sql):
            if "FROM system.databases" in sql:
                return [{"name": "team_a", "engine": "Atomic", "object_id": database_uuid}]
            if "FROM system.tables" in sql:
                return [{
                    "database": "team_a",
                    "name": "events",
                    "object_id": nil_uuid,
                    "engine": "MergeTree",
                    "partition_key": "",
                    "sorting_key": "event_time",
                }]
            return []

    provisioner = DatasetProvisioner(
        MetadataClient(),
        AdminConnection("127.0.0.1", 8123, "admin", "synthetic-password", 10),
        parse_canonical_schema(SCHEMA),
        tmp_path / "ownership.json",
        {"version": 1, "datasets": {}},
        require_root=False,
    )
    with pytest.raises(ProvisionError, match="table adoption requires a non-nil ClickHouse UUID"):
        provisioner.preflight(datasets, runtime_by_id)


def test_pending_managed_creation_with_incomplete_owned_objects_can_resume(tmp_path) -> None:
    _, datasets = _validate_registry(base_registry())
    runtime_by_id = _validate_runtime({"version": 1, "datasets": [base_runtime()]})
    record = {
        "database": "team_a",
        "table": "events",
        "role": "rawbbit_role_team_a",
        "user": "rawbbit_mcp_team_a",
        "privileges": ["SELECT"],
        "schema_fingerprint": parse_canonical_schema(SCHEMA).fingerprint,
        "object_ids": {},
        "password_revision": 1,
        "state": "pending",
        "pending_action": None,
    }

    class EmptyMetadataClient:
        def query(self, _sql):
            return []

    provisioner = DatasetProvisioner(
        EmptyMetadataClient(),
        AdminConnection("127.0.0.1", 8123, "admin", "synthetic-password", 10),
        parse_canonical_schema(SCHEMA),
        tmp_path / "ownership.json",
        {"version": 1, "datasets": {"team_a": record}},
        require_root=False,
    )
    provisioner.preflight(datasets, runtime_by_id)


def test_preflight_requires_main_exposure_and_limit_contracts_to_match(tmp_path) -> None:
    _, datasets = _validate_registry(base_registry())
    runtime_by_id = _validate_runtime(
        {"version": 1, "datasets": [base_runtime(main_exposed=False)]}
    )
    provisioner = DatasetProvisioner(
        object(),
        AdminConnection("127.0.0.1", 8123, "admin", "synthetic-password", 10),
        parse_canonical_schema(SCHEMA),
        tmp_path / "ownership.json",
        {"version": 1, "datasets": {}},
        require_root=False,
    )
    with pytest.raises(ProvisionError, match="main-exposure"):
        provisioner.preflight(datasets, runtime_by_id)


def test_managed_access_rejects_role_assignment_with_admin_option() -> None:
    dataset = base_dataset()

    class AccessClient:
        def query(self, sql):
            if "FROM system.grants" in sql:
                return [{
                    "user_name": None,
                    "role_name": "rawbbit_role_team_a",
                    "access_type": "SELECT",
                    "database": "team_a",
                    "table": "events",
                    "column": None,
                    "is_partial_revoke": 0,
                    "grant_option": 0,
                }]
            if "FROM system.role_grants" in sql:
                return [{
                    "user_name": "rawbbit_mcp_team_a",
                    "role_name": None,
                    "granted_role_name": "rawbbit_role_team_a",
                    "granted_role_is_default": 1,
                    "with_admin_option": 1,
                }]
            raise AssertionError("unexpected metadata query")

    with pytest.raises(ProvisionError, match="unexpected role assignments"):
        _verify_exact_managed_access(AccessClient(), dataset)


def test_reference_access_rejects_role_assignment_with_admin_option() -> None:
    dataset = base_dataset(mode="reference", create_table=False)

    class AccessClient:
        def query(self, sql):
            if "FROM system.role_grants" in sql:
                return [{
                    "user_name": "rawbbit_mcp_team_a",
                    "role_name": None,
                    "granted_role_name": "reference_read_role",
                    "with_admin_option": 1,
                }]
            if "FROM system.grants" in sql:
                return [{
                    "user_name": "rawbbit_mcp_team_a",
                    "role_name": None,
                    "access_type": "SELECT",
                    "database": "team_a",
                    "table": "events",
                    "column": None,
                    "is_partial_revoke": 0,
                    "grant_option": 0,
                }]
            raise AssertionError("unexpected metadata query")

    with pytest.raises(ProvisionError, match="ADMIN OPTION"):
        _verify_reference_access(AccessClient(), dataset)


def test_main_user_unexposed_dataset_check_covers_transitive_role_grants() -> None:
    class MainAccessClient:
        def query(self, sql):
            if "FROM system.users" in sql:
                return [{"name": "rawbbit_mcp"}]
            if "FROM system.role_grants" in sql and "WHERE user_name" in sql:
                return [{
                    "user_name": "rawbbit_mcp",
                    "role_name": None,
                    "granted_role_name": "main_reader",
                }]
            if "FROM system.role_grants" in sql and "main_reader" in sql:
                return [{
                    "user_name": None,
                    "role_name": "main_reader",
                    "granted_role_name": "nested_reader",
                }]
            if "FROM system.role_grants" in sql:
                return []
            if "FROM system.grants" in sql:
                assert "nested_reader" in sql
                return [{
                    "user_name": None,
                    "role_name": "nested_reader",
                    "access_type": "SELECT",
                    "database": "team_a",
                    "table": None,
                    "column": None,
                    "is_partial_revoke": 0,
                }]
            raise AssertionError(f"unexpected metadata query: {sql}")

    with pytest.raises(ProvisionError, match="unexposed dataset"):
        _verify_main_user_cannot_read_unexposed_datasets(
            MainAccessClient(), "rawbbit_mcp", [base_dataset(main_exposed=False)]
        )


def test_main_user_unexposed_dataset_check_fails_closed_and_ignores_exposed_targets() -> None:
    class MissingMainUserClient:
        def query(self, sql):
            assert "FROM system.users" in sql
            return []

    with pytest.raises(ProvisionError, match="main MCP ClickHouse user is missing"):
        _verify_main_user_cannot_read_unexposed_datasets(
            MissingMainUserClient(), "rawbbit_mcp", [base_dataset(main_exposed=False)]
        )

    class UnexpectedQueryClient:
        def query(self, sql):
            raise AssertionError(f"no grant check is needed for an exposed dataset: {sql}")

    _verify_main_user_cannot_read_unexposed_datasets(
        UnexpectedQueryClient(), "rawbbit_mcp", [base_dataset(main_exposed=True)]
    )


def test_main_user_check_retains_targets_from_removed_registrations() -> None:
    class MainAccessClient:
        def query(self, sql):
            if "FROM system.users" in sql:
                return [{"name": "rawbbit_mcp"}]
            if "FROM system.role_grants" in sql:
                return []
            if "FROM system.grants" in sql:
                return [{
                    "user_name": "rawbbit_mcp",
                    "role_name": None,
                    "access_type": "SELECT",
                    "database": "team_a",
                    "table": "events",
                    "column": None,
                    "is_partial_revoke": 0,
                }]
            raise AssertionError(f"unexpected metadata query: {sql}")

    old_target_journal = {
        "version": 1,
        "main_access_targets": [{"database": "team_a", "table": "events"}],
        "datasets": {},
    }
    with pytest.raises(ProvisionError, match="unexposed dataset"):
        _verify_main_user_cannot_read_unexposed_datasets(
            MainAccessClient(), "rawbbit_mcp", [], old_target_journal
        )

    # Older journals have managed object records but no exposure-target list.
    legacy_journal = {
        "version": 1,
        "datasets": {"team_a": {"database": "team_a", "table": "events"}},
    }
    with pytest.raises(ProvisionError, match="unexposed dataset"):
        _verify_main_user_cannot_read_unexposed_datasets(
            MainAccessClient(), "rawbbit_mcp", [], legacy_journal
        )


def test_preflight_checks_main_grants_for_removed_journal_targets(tmp_path) -> None:
    class MainAccessClient:
        def query(self, sql):
            if "FROM system.users" in sql:
                return [{"name": "rawbbit_mcp"}]
            if "FROM system.role_grants" in sql:
                return []
            if "FROM system.grants" in sql:
                return [{
                    "user_name": "rawbbit_mcp",
                    "role_name": None,
                    "access_type": "SELECT",
                    "database": "team_a",
                    "table": "events",
                    "column": None,
                    "is_partial_revoke": 0,
                }]
            raise AssertionError(f"unexpected metadata query: {sql}")

    journal = {
        "version": 1,
        "main_access_targets": [{"database": "team_a", "table": "events"}],
        "datasets": {},
    }
    provisioner = DatasetProvisioner(
        MainAccessClient(),
        AdminConnection("127.0.0.1", 8123, "admin", "synthetic-password", 10),
        parse_canonical_schema(SCHEMA),
        tmp_path / "ownership.json",
        journal,
        require_root=False,
    )
    with pytest.raises(ProvisionError, match="unexposed dataset"):
        provisioner.preflight([], {})


def test_preflight_rejects_disabled_dataset_access_through_main_user(tmp_path) -> None:
    _, datasets = _validate_registry(base_registry(enabled=False, main_exposed=True))
    runtime_by_id = _validate_runtime({"version": 1, "datasets": []})

    class MainAccessClient:
        def query(self, sql):
            if "FROM system.users" in sql:
                return [{"name": "rawbbit_mcp"}]
            if "FROM system.role_grants" in sql:
                return []
            if "FROM system.grants" in sql:
                return [{
                    "user_name": "rawbbit_mcp",
                    "role_name": None,
                    "access_type": "ALL",
                    "database": None,
                    "table": None,
                    "column": None,
                    "is_partial_revoke": 0,
                }]
            raise AssertionError(f"unexpected metadata query: {sql}")

    provisioner = DatasetProvisioner(
        MainAccessClient(),
        AdminConnection("127.0.0.1", 8123, "admin", "synthetic-password", 10),
        parse_canonical_schema(SCHEMA),
        tmp_path / "ownership.json",
        {"version": 1, "datasets": {}},
        require_root=False,
        main_user="rawbbit_mcp",
    )
    with pytest.raises(ProvisionError, match="unexposed dataset"):
        provisioner.preflight(datasets, runtime_by_id)


def test_provisioner_retains_registered_targets_after_disable_or_removal(tmp_path) -> None:
    _, disabled = _validate_registry(base_registry(enabled=False))
    journal = {"version": 1, "datasets": {}}
    provisioner = DatasetProvisioner(
        object(),
        AdminConnection("127.0.0.1", 8123, "admin", "synthetic-password", 10),
        parse_canonical_schema(SCHEMA),
        tmp_path / "ownership.json",
        journal,
        require_root=False,
    )

    provisioner.provision(disabled, {})

    assert journal["main_access_targets"] == [{"database": "team_a", "table": "events"}]
    assert json.loads((tmp_path / "ownership.json").read_text())["main_access_targets"] == journal[
        "main_access_targets"
    ]

    # An empty registry never erases previous isolation targets.
    next_run = DatasetProvisioner(
        object(),
        AdminConnection("127.0.0.1", 8123, "admin", "synthetic-password", 10),
        parse_canonical_schema(SCHEMA),
        tmp_path / "ownership.json",
        journal,
        require_root=False,
    )
    next_run.provision([], {})
    assert journal["main_access_targets"] == [{"database": "team_a", "table": "events"}]


def test_main_user_must_be_distinct_from_admin_and_dataset_users() -> None:
    config = base_registry()
    config["main_user"] = "admin"
    with pytest.raises(ProvisionError, match="privileged admin user"):
        _validate_registry(config)

    config = base_registry()
    config["datasets"][0]["mcp_user"] = "rawbbit_mcp"
    with pytest.raises(ProvisionError, match="main MCP user"):
        _validate_registry(config)


def test_managed_grant_reconciliation_removes_partial_revokes(tmp_path) -> None:
    dataset = base_dataset()
    role = dataset["mcp_role"]
    user = dataset["mcp_user"]
    grants = [
        {
            "user_name": None,
            "role_name": role,
            "access_type": "SELECT",
            "database": dataset["database"],
            "table": None,
            "column": None,
            "is_partial_revoke": 0,
            "grant_option": 0,
        },
        {
            "user_name": None,
            "role_name": role,
            "access_type": "SELECT",
            "database": dataset["database"],
            "table": dataset["table"],
            "column": None,
            "is_partial_revoke": 1,
            "grant_option": 0,
        },
    ]
    assignments = [{
        "user_name": user,
        "role_name": None,
        "granted_role_name": role,
        "granted_role_is_default": 1,
        "with_admin_option": 0,
    }]
    executions = []

    class PartialRevokeClient:
        def query(self, sql):
            if "FROM system.role_grants" in sql:
                return assignments
            if "FROM system.settings_profile_elements" in sql:
                return [
                    {"setting_name": "readonly", "value": "1"},
                    {"setting_name": "max_execution_time", "value": "30"},
                    {"setting_name": "max_result_rows", "value": "500"},
                    {"setting_name": "result_overflow_mode", "value": "break"},
                ]
            if "FROM system.users" in sql:
                return [{
                    "default_roles_all": 0,
                    "default_roles_list": [role],
                    "default_roles_except": [],
                }]
            if "FROM system.grants" in sql:
                return list(grants)
            raise AssertionError(f"unexpected metadata query: {sql}")

        def execute(self, sql):
            executions.append(sql)
            if sql.startswith("REVOKE ALL ON"):
                # ClickHouse removes the broad grant and its partial-revoke row
                # when the managed role's whole database scope is reconciled.
                grants.clear()
            elif sql.startswith("GRANT SELECT ON"):
                grants[:] = [{
                    "user_name": None,
                    "role_name": role,
                    "access_type": "SELECT",
                    "database": dataset["database"],
                    "table": dataset["table"],
                    "column": None,
                    "is_partial_revoke": 0,
                    "grant_option": 0,
                }]

    record = {
        "database": dataset["database"],
        "table": dataset["table"],
        "role": role,
        "user": user,
        "privileges": ["SELECT"],
        "schema_fingerprint": "synthetic-schema-fingerprint",
        "object_ids": {},
        "password_revision": 1,
        "state": "applied",
        "pending_action": None,
    }
    provisioner = DatasetProvisioner(
        PartialRevokeClient(),
        AdminConnection("127.0.0.1", 8123, "admin", "synthetic-password", 10),
        parse_canonical_schema(SCHEMA),
        tmp_path / "ownership.json",
        {"version": 1, "datasets": {dataset["id"]: record}},
        require_root=False,
    )

    provisioner._reconcile_role_and_user(dataset, record)

    assert any(sql.startswith("REVOKE ALL ON") for sql in executions)
    assert any(sql.startswith("GRANT SELECT ON") for sql in executions)
    assert grants == [{
        "user_name": None,
        "role_name": role,
        "access_type": "SELECT",
        "database": dataset["database"],
        "table": dataset["table"],
        "column": None,
        "is_partial_revoke": 0,
        "grant_option": 0,
    }]


def test_clickhouse_query_uses_post_request_without_returning_http_error_body(monkeypatch) -> None:
    captured = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return b"1\n"

    def fake_urlopen(request, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    client = ClickHouseHTTP(AdminConnection("127.0.0.1", 8123, "admin", "synthetic-secret", 7))
    assert client.request("SELECT 1") == "1\n"
    assert captured["request"].get_method() == "POST"
    assert captured["request"].get_header("Authorization")
    assert captured["timeout"] == 7
