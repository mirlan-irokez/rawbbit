from __future__ import annotations

import json
import re
import stat
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, field_validator, model_validator

from rawbbit_mcp.settings import Settings

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_DATASET_ID = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
_DATASET_PATH = re.compile(r"^/datasets/[a-z][a-z0-9_]{0,39}(?:/[A-Za-z0-9_-]+)+$")


class DatasetConfigurationError(ValueError):
    """A safe-to-report runtime dataset configuration error."""


class RuntimeDataset(BaseModel):
    """One enabled dataset's MCP-only runtime configuration."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    enabled: bool = Field(default=True, strict=True)
    endpoint_path: str
    database: str
    table: str = "events"
    main_exposed: bool = Field(default=False, strict=True)
    clickhouse_user: str
    clickhouse_password: SecretStr
    bearer_tokens: dict[str, SecretStr]
    max_query_rows: int | None = Field(default=None, ge=1, strict=True)
    max_sample_rows: int | None = Field(default=None, ge=1, strict=True)
    max_execution_seconds: int | None = Field(default=None, ge=1, strict=True)

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("dataset id must not contain leading or trailing whitespace")
        if not _DATASET_ID.fullmatch(value):
            raise ValueError("dataset id must be a lowercase slug using letters, digits, and underscores")
        if value == "default":
            raise ValueError("dataset id 'default' is reserved for the existing main dataset")
        return value

    @field_validator("database", "table", "clickhouse_user")
    @classmethod
    def validate_clickhouse_identifier(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("ClickHouse identifiers must not contain leading or trailing whitespace")
        if not _IDENTIFIER.fullmatch(value):
            raise ValueError("database, table, and user names must be simple ClickHouse identifiers")
        return value

    @field_validator("endpoint_path")
    @classmethod
    def validate_endpoint_path(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("endpoint_path must not contain leading or trailing whitespace")
        if not value.startswith("/") or value == "/" or value.endswith("/"):
            raise ValueError("endpoint_path must be an absolute path without a trailing slash")
        if not _DATASET_PATH.fullmatch(value):
            raise ValueError("endpoint_path must be normalized")
        return value

    @field_validator("clickhouse_password")
    @classmethod
    def validate_clickhouse_password(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip():
            raise ValueError("clickhouse_password is required")
        return value

    @field_validator("bearer_tokens")
    @classmethod
    def validate_bearer_tokens(cls, value: dict[str, SecretStr]) -> dict[str, SecretStr]:
        if not value:
            raise ValueError("at least one scoped bearer token is required")
        seen: set[str] = set()
        for label, token in value.items():
            if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", label):
                raise ValueError("scoped token labels must use letters, digits, dots, underscores, or hyphens")
            token_value = token.get_secret_value()
            if not token_value.strip() or token_value != token_value.strip():
                raise ValueError("scoped token labels and values must be non-empty")
            if token_value in seen:
                raise ValueError("scoped bearer token values must be unique")
            seen.add(token_value)
        return value

    @model_validator(mode="after")
    def validate_enabled(self) -> RuntimeDataset:
        if not self.enabled:
            raise ValueError("disabled datasets must not be present in the MCP runtime registry")
        return self

    @property
    def token_values(self) -> dict[str, str]:
        return {label: token.get_secret_value() for label, token in self.bearer_tokens.items()}


class DatasetRegistry(BaseModel):
    """Versioned JSON contract mounted into the MCP container."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: int = Field(strict=True)
    datasets: tuple[RuntimeDataset, ...] = ()

    @model_validator(mode="after")
    def validate_registry(self) -> DatasetRegistry:
        if self.version != 1:
            raise ValueError("unsupported dataset registry version")
        ids = [dataset.id for dataset in self.datasets]
        paths = [dataset.endpoint_path for dataset in self.datasets]
        clickhouse_users = [dataset.clickhouse_user for dataset in self.datasets]
        databases = [dataset.database for dataset in self.datasets]
        targets = [(dataset.database, dataset.table) for dataset in self.datasets]
        if len(ids) != len(set(ids)):
            raise ValueError("dataset ids must be unique")
        if len(paths) != len(set(paths)):
            raise ValueError("dataset endpoint paths must be unique")
        if len(clickhouse_users) != len(set(clickhouse_users)):
            raise ValueError("each dataset must use a distinct ClickHouse user")
        if len(targets) != len(set(targets)):
            raise ValueError("each dataset must target a distinct ClickHouse table")
        if len(databases) != len(set(databases)):
            raise ValueError("each dataset must use a distinct ClickHouse database")
        all_tokens = [token.get_secret_value() for dataset in self.datasets for token in dataset.bearer_tokens.values()]
        if len(all_tokens) != len(set(all_tokens)):
            raise ValueError("scoped bearer token values must be unique across datasets")
        return self

    def validate_for_settings(self, settings: Settings) -> None:
        main_path = settings.mcp_path.rstrip("/") or "/mcp"
        main_tokens = set(settings.api_keys_by_user.values())
        seen_prefixes: set[str] = set()
        for dataset in self.datasets:
            if dataset.clickhouse_user == settings.clickhouse_user:
                raise DatasetConfigurationError("dataset and main endpoints must use distinct ClickHouse users")
            if dataset.database == settings.clickhouse_database:
                raise DatasetConfigurationError("dataset and main endpoints must use distinct ClickHouse databases")
            if dataset.endpoint_path == main_path:
                raise DatasetConfigurationError("dataset endpoint path collides with the main MCP path")
            if not dataset.endpoint_path.endswith(main_path):
                raise DatasetConfigurationError("dataset endpoint path must end with the configured MCP_PATH")
            prefix = dataset.endpoint_path[: -len(main_path)].rstrip("/")
            if not prefix.startswith("/datasets/") or prefix.count("/") != 2:
                raise DatasetConfigurationError("dataset endpoint path must use /datasets/<id><MCP_PATH>")
            if prefix.rsplit("/", 1)[-1] != dataset.id:
                raise DatasetConfigurationError("dataset endpoint path must match its dataset id")
            if prefix in seen_prefixes:
                raise DatasetConfigurationError("dataset endpoint path prefixes must be unique")
            seen_prefixes.add(prefix)
            if main_tokens.intersection(dataset.token_values.values()):
                raise DatasetConfigurationError("main and scoped bearer token values must be distinct")


def load_dataset_registry(path_value: str | None, settings: Settings) -> DatasetRegistry:
    """Load a protected runtime registry; report no secrets or JSON values on failure."""
    if not path_value or not path_value.strip():
        return DatasetRegistry(version=1, datasets=())

    path = Path(path_value)
    try:
        if path.is_symlink():
            raise DatasetConfigurationError("MCP_DATASETS_FILE must not be a symlink")
        info = path.stat()
        if not stat.S_ISREG(info.st_mode):
            raise DatasetConfigurationError("MCP_DATASETS_FILE must be a regular file")
        if info.st_mode & 0o077:
            raise DatasetConfigurationError("MCP_DATASETS_FILE must not be accessible by group or other users")
        raw: Any = json.loads(path.read_text(encoding="utf-8"))
        registry = DatasetRegistry.model_validate(raw)
        registry.validate_for_settings(settings)
    except DatasetConfigurationError:
        raise
    except (OSError, json.JSONDecodeError, ValidationError) as exc:
        raise DatasetConfigurationError("MCP_DATASETS_FILE is missing or has an invalid registry") from exc
    return registry
