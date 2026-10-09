from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor

# server.py exposes a module-level production ASGI app for `python -m`. Supply
# temporary test-only auth while importing it, then restore the test process env
# so the settings unit tests remain independent of import order.
_original_main_keys = os.environ.get("MCP_API_KEYS_JSON")
_original_allow_unauthenticated = os.environ.get("MCP_ALLOW_UNAUTHENTICATED")
os.environ["MCP_API_KEYS_JSON"] = '{"operator":"module-import-test-token"}'
os.environ.pop("MCP_ALLOW_UNAUTHENTICATED", None)

from fastmcp import FastMCP
from starlette.testclient import TestClient

from rawbbit_mcp.datasets import DatasetRegistry
from rawbbit_mcp.server import create_app
from rawbbit_mcp.settings import Settings

if _original_main_keys is None:
    os.environ.pop("MCP_API_KEYS_JSON", None)
else:
    os.environ["MCP_API_KEYS_JSON"] = _original_main_keys
if _original_allow_unauthenticated is None:
    os.environ.pop("MCP_ALLOW_UNAUTHENTICATED", None)
else:
    os.environ["MCP_ALLOW_UNAUTHENTICATED"] = _original_allow_unauthenticated


class RecordingGateway:
    calls: list[tuple[str, str]] = []

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def query_rows(self, sql: str) -> list[dict]:
        self.calls.append((self.settings.clickhouse_database, sql))
        return [{"ok": 1, "database": self.settings.clickhouse_database}]


def settings(**updates) -> Settings:
    values = {
        "MCP_API_KEYS_JSON": '{"operator":"main-token","analyst":"analyst-token"}',
        "CLICKHOUSE_DATABASE": "analytics",
        "CLICKHOUSE_TABLE": "events",
        "MCP_ALLOW_UNAUTHENTICATED": False,
    }
    values.update(updates)
    return Settings(**values)


def registry() -> DatasetRegistry:
    return DatasetRegistry.model_validate(
        {
            "version": 1,
            "datasets": [
                {
                    "id": "team_a",
                    "endpoint_path": "/datasets/team_a/mcp",
                    "database": "team_a",
                    "table": "events",
                    "main_exposed": True,
                    "clickhouse_user": "rawbbit_mcp_team_a",
                    "clickhouse_password": "team-a-db-secret",
                    "bearer_tokens": {"agent": "team-a-token"},
                },
                {
                    "id": "team_b",
                    "endpoint_path": "/datasets/team_b/mcp",
                    "database": "team_b",
                    "table": "events",
                    "main_exposed": False,
                    "clickhouse_user": "rawbbit_mcp_team_b",
                    "clickhouse_password": "team-b-db-secret",
                    "bearer_tokens": {"agent": "team-b-token"},
                },
            ],
        }
    )


def headers(token: str, session_id: str | None = None) -> dict[str, str]:
    result = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    if session_id:
        result["mcp-session-id"] = session_id
    return result


def initialize(client: TestClient, path: str, token: str) -> tuple[dict[str, str], str]:
    response = client.post(
        path,
        headers=headers(token),
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "dataset-test", "version": "1"},
            },
        },
    )
    assert response.status_code == 200, response.text
    session_id = response.headers.get("mcp-session-id")
    assert session_id
    session_headers = headers(token, session_id)
    initialized = client.post(
        path,
        headers=session_headers,
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
    )
    assert initialized.status_code in (200, 202), initialized.text
    return session_headers, session_id


def call_tool(client: TestClient, path: str, auth_headers: dict[str, str], name: str, arguments: dict):
    response = client.post(
        path,
        headers=auth_headers,
        json={
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
    )
    assert response.status_code == 200, response.text
    return parse_sse_json(response.text)


def tools_list(client: TestClient, path: str, auth_headers: dict[str, str]) -> list[dict]:
    response = client.post(
        path,
        headers=auth_headers,
        json={"jsonrpc": "2.0", "id": 4, "method": "tools/list"},
    )
    assert response.status_code == 200, response.text
    return parse_sse_json(response.text)["result"]["tools"]


def parse_sse_json(body: str) -> dict:
    data_lines = [line.removeprefix("data: ") for line in body.splitlines() if line.startswith("data: ")]
    assert data_lines, body
    import json

    return json.loads(data_lines[-1])


def test_empty_registry_keeps_default_endpoint_tool_schema() -> None:
    cfg = settings()
    server, app = create_app(cfg, DatasetRegistry(version=1), gateway_factory=RecordingGateway)
    assert isinstance(server, FastMCP)

    with TestClient(app) as client:
        auth_headers, _ = initialize(client, "/mcp", "main-token")
        tools = tools_list(client, "/mcp", auth_headers)

    names = {tool["name"] for tool in tools}
    assert names == {
        "healthcheck",
        "table_overview",
        "list_event_names",
        "discover_json_keys",
        "sample_events",
        "run_readonly_sql",
        "calculate_dau",
        "calculate_funnel",
    }
    assert "dataset_id" not in tools[[tool["name"] for tool in tools].index("run_readonly_sql")]["inputSchema"]["properties"]


def test_main_discovery_and_scoped_endpoints_have_distinct_auth_and_tools() -> None:
    RecordingGateway.calls.clear()
    server, app = create_app(settings(), registry(), gateway_factory=RecordingGateway)
    assert isinstance(server, FastMCP)

    with TestClient(app) as client:
        assert client.post("/mcp", headers=headers("team-a-token"), json={}).status_code == 401
        assert client.post("/datasets/team_a/mcp", headers=headers("main-token"), json={}).status_code == 401
        assert client.post("/datasets/team_a/mcp", headers=headers("team-b-token"), json={}).status_code == 401
        assert client.post("/datasets/team_b/mcp", headers=headers("team-a-token"), json={}).status_code == 401

        main_headers, _ = initialize(client, "/mcp", "main-token")
        second_main_headers, _ = initialize(client, "/mcp", "analyst-token")
        main_tools = tools_list(client, "/mcp", main_headers)
        assert "list_datasets" in {tool["name"] for tool in main_tools}
        selector = main_tools[[tool["name"] for tool in main_tools].index("run_readonly_sql")]
        assert "dataset_id" in selector["inputSchema"]["properties"]

        listed = call_tool(client, "/mcp", main_headers, "list_datasets", {})
        listed_text = str(listed)
        assert "default" in listed_text and "team_a" in listed_text
        assert "team_b" not in listed_text
        assert call_tool(client, "/mcp", second_main_headers, "list_datasets", {}) == listed

        scoped_headers, _ = initialize(client, "/datasets/team_a/mcp", "team-a-token")
        scoped_tools = tools_list(client, "/datasets/team_a/mcp", scoped_headers)
        assert "list_datasets" not in {tool["name"] for tool in scoped_tools}
        scoped_sql = scoped_tools[[tool["name"] for tool in scoped_tools].index("run_readonly_sql")]
        assert "dataset_id" not in scoped_sql["inputSchema"]["properties"]

        call_tool(client, "/mcp", main_headers, "run_readonly_sql", {"sql": "SELECT 1", "dataset_id": "team_a"})
        call_tool(client, "/datasets/team_a/mcp", scoped_headers, "run_readonly_sql", {"sql": "SELECT 1"})
        assert [db for db, _ in RecordingGateway.calls[-2:]] == ["team_a", "team_a"]


def test_main_rejects_non_exposed_dataset_without_query_fallback() -> None:
    RecordingGateway.calls.clear()
    _, app = create_app(settings(), registry(), gateway_factory=RecordingGateway)
    with TestClient(app) as client:
        auth_headers, _ = initialize(client, "/mcp", "main-token")
        result = call_tool(client, "/mcp", auth_headers, "run_readonly_sql", {"sql": "SELECT 1", "dataset_id": "team_b"})

    assert result["result"]["isError"] is True
    assert not RecordingGateway.calls


def test_tool_audit_logs_safe_metadata_without_arguments_or_credentials(caplog) -> None:
    caplog.set_level("INFO", logger="rawbbit_mcp")
    RecordingGateway.calls.clear()
    _, app = create_app(settings(), registry(), gateway_factory=RecordingGateway)
    sql_marker = "private-sql-marker"

    with TestClient(app) as client:
        scoped_headers, _ = initialize(client, "/datasets/team_a/mcp", "team-a-token")
        call_tool(
            client,
            "/datasets/team_a/mcp",
            scoped_headers,
            "run_readonly_sql",
            {"sql": f"SELECT '{sql_marker}'"},
        )

    records = [record.getMessage() for record in caplog.records if "tool_call" in record.getMessage()]
    assert any(
        "dataset_id=team_a" in message
        and "endpoint=scoped" in message
        and "tool=run_readonly_sql" in message
        and "actor=agent" in message
        and "result=ok" in message
        and "duration_ms=" in message
        for message in records
    )
    joined = "\n".join(records)
    assert sql_marker not in joined
    assert "team-a-token" not in joined
    assert "team-a-db-secret" not in joined


def test_sessions_and_concurrent_calls_do_not_cross_dataset_context() -> None:
    RecordingGateway.calls.clear()
    _, app = create_app(settings(), registry(), gateway_factory=RecordingGateway)

    with TestClient(app) as client:
        _, team_a_session = initialize(client, "/datasets/team_a/mcp", "team-a-token")
        initialize(client, "/datasets/team_b/mcp", "team-b-token")

        reused_session = client.post(
            "/datasets/team_b/mcp",
            headers=headers("team-b-token", team_a_session),
            json={"jsonrpc": "2.0", "id": 9, "method": "tools/list"},
        )
        assert reused_session.status_code in (400, 404)

        def read_dataset(path: str, token: str) -> str:
            auth_headers, _ = initialize(client, path, token)
            response = call_tool(
                client,
                path,
                auth_headers,
                "run_readonly_sql",
                {"sql": "SELECT 1"},
            )
            return str(response)

        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = [
                executor.submit(
                    read_dataset,
                    "/datasets/team_a/mcp",
                    "team-a-token",
                )
                if index % 2 == 0
                else executor.submit(
                    read_dataset,
                    "/datasets/team_b/mcp",
                    "team-b-token",
                )
                for index in range(16)
            ]
            results = [future.result() for future in futures]

    assert len(results) == 16
    assert all("team_a" in results[index] for index in range(0, 16, 2))
    assert all("team_b" in results[index] for index in range(1, 16, 2))
    assert all("team-a-db-secret" not in result and "team-b-db-secret" not in result for result in results)
    assert {database for database, _ in RecordingGateway.calls} == {"team_a", "team_b"}
    assert all(database in {"team_a", "team_b"} for database, _ in RecordingGateway.calls)
