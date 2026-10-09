from __future__ import annotations

import json
import os

import pytest
from pydantic import ValidationError

from rawbbit_mcp.datasets import DatasetConfigurationError, DatasetRegistry, load_dataset_registry
from rawbbit_mcp.settings import Settings


def dataset_entry(**updates):
    value = {
        "id": "team_a",
        "endpoint_path": "/datasets/team_a/mcp",
        "database": "team_a",
        "table": "events",
        "main_exposed": True,
        "clickhouse_user": "rawbbit_mcp_team_a",
        "clickhouse_password": "dataset-db-password",
        "bearer_tokens": {"agent": "team-a-token"},
    }
    value.update(updates)
    return value


def base_settings(**overrides):
    return Settings(MCP_API_KEYS_JSON='{"operator":"main-token"}', **overrides)


def test_registry_rejects_duplicate_ids_paths_and_tokens() -> None:
    entry = dataset_entry()
    with pytest.raises(ValidationError):
        DatasetRegistry.model_validate({"version": 1, "datasets": [entry, entry]})

    second = dataset_entry(id="team_b", endpoint_path="/datasets/team_a/mcp")
    with pytest.raises(ValidationError):
        DatasetRegistry.model_validate({"version": 1, "datasets": [entry, second]})

    second = dataset_entry(id="team_b", endpoint_path="/datasets/team_b/mcp")
    with pytest.raises(ValidationError):
        DatasetRegistry.model_validate({"version": 1, "datasets": [entry, second]})

    shared_user = dataset_entry(
        id="team_b",
        endpoint_path="/datasets/team_b/mcp",
        database="team_b",
        bearer_tokens={"agent": "team-b-token"},
    )
    with pytest.raises(ValidationError, match="distinct ClickHouse user"):
        DatasetRegistry.model_validate({"version": 1, "datasets": [entry, shared_user]})


@pytest.mark.parametrize(
    "update",
    [
        {"id": "Team-A"},
        {"id": "default"},
        {"id": " team_a"},
        {"database": "team_a; DROP TABLE events"},
        {"clickhouse_user": "admin.user"},
        {"endpoint_path": "/datasets/team_a/../mcp"},
        {"endpoint_path": " /datasets/team_a/mcp"},
        {"bearer_tokens": {}},
        {"bearer_tokens": {"team A": "token"}},
        {"bearer_tokens": {"agent": " token "}},
        {"clickhouse_password": ""},
        {"enabled": False},
    ],
)
def test_runtime_dataset_rejects_invalid_or_disabled_config(update) -> None:
    with pytest.raises(ValidationError):
        DatasetRegistry.model_validate({"version": 1, "datasets": [dataset_entry(**update)]})


def test_runtime_registry_rejects_duplicate_physical_targets_and_non_strict_limits() -> None:
    first = dataset_entry()
    second = dataset_entry(
        id="team_b",
        endpoint_path="/datasets/team_b/mcp",
        clickhouse_user="rawbbit_mcp_team_b",
        bearer_tokens={"agent": "team-b-token"},
    )
    with pytest.raises(ValidationError, match="distinct ClickHouse table"):
        DatasetRegistry.model_validate({"version": 1, "datasets": [first, second]})

    second["table"] = "other_events"
    with pytest.raises(ValidationError, match="distinct ClickHouse database"):
        DatasetRegistry.model_validate({"version": 1, "datasets": [first, second]})

    with pytest.raises(ValidationError):
        DatasetRegistry.model_validate(
            {"version": 1, "datasets": [dataset_entry(max_query_rows=True)]}
        )


def test_registry_rejects_main_path_collision_and_duplicate_main_token() -> None:
    registry = DatasetRegistry.model_validate({"version": 1, "datasets": [dataset_entry()]})
    registry.validate_for_settings(base_settings())

    with pytest.raises(DatasetConfigurationError, match="distinct"):
        duplicate = DatasetRegistry.model_validate(
            {"version": 1, "datasets": [dataset_entry(bearer_tokens={"agent": "main-token"})]}
        )
        duplicate.validate_for_settings(base_settings())

    with pytest.raises(DatasetConfigurationError, match="match its dataset id"):
        malformed = DatasetRegistry.model_validate(
            {"version": 1, "datasets": [dataset_entry(endpoint_path="/datasets/other/mcp")]}
        )
        malformed.validate_for_settings(base_settings())

    with pytest.raises(DatasetConfigurationError, match="distinct ClickHouse users"):
        registry.validate_for_settings(base_settings(CLICKHOUSE_USER="rawbbit_mcp_team_a"))

    with pytest.raises(DatasetConfigurationError, match="distinct ClickHouse databases"):
        registry.validate_for_settings(base_settings(CLICKHOUSE_DATABASE="team_a"))


def test_runtime_file_requires_restricted_permissions_and_does_not_echo_secrets(tmp_path) -> None:
    config = tmp_path / "datasets.json"
    config.write_text(json.dumps({"version": 1, "datasets": [dataset_entry()]}), encoding="utf-8")
    os.chmod(config, 0o640)
    with pytest.raises(DatasetConfigurationError, match="group or other"):
        load_dataset_registry(str(config), base_settings())

    os.chmod(config, 0o600)
    loaded = load_dataset_registry(str(config), base_settings())
    assert loaded.datasets[0].id == "team_a"
    assert "dataset-db-password" not in repr(loaded)
    assert "team-a-token" not in repr(loaded)


def test_missing_runtime_file_fails_closed_without_revealing_path(tmp_path) -> None:
    missing = tmp_path / "private-datasets.json"
    with pytest.raises(DatasetConfigurationError, match="invalid registry") as exc:
        load_dataset_registry(str(missing), base_settings())
    assert str(missing) not in str(exc.value)
