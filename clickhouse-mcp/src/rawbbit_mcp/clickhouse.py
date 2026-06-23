from __future__ import annotations

import re
from typing import Any

from rawbbit_mcp.settings import Settings

JSON_COLUMNS = {
    "event_params_json",
    "user_properties_json",
    "traffic_source_json",
    "geo_json",
    "consent_json",
}

READONLY_PREFIXES = ("select", "with", "show", "describe", "desc", "explain")
FORBIDDEN_SQL = re.compile(
    r"\b(insert|alter|create|drop|truncate|delete|update|optimize|system|grant|revoke|attach|detach|rename|kill)\b",
    re.IGNORECASE,
)


class UnsafeQueryError(ValueError):
    pass


def quote_string(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def checked_limit(limit: int, maximum: int) -> int:
    if limit < 1:
        return 1
    return min(limit, maximum)


def validate_readonly_sql(sql: str) -> str:
    cleaned = sql.strip()
    if not cleaned:
        raise UnsafeQueryError("SQL cannot be empty")
    if ";" in cleaned.rstrip(";"):
        raise UnsafeQueryError("Only one statement is allowed")
    cleaned = cleaned.rstrip(";").strip()
    lowered = cleaned.lower()
    if not lowered.startswith(READONLY_PREFIXES):
        raise UnsafeQueryError("Only read-only SELECT/WITH/SHOW/DESCRIBE/EXPLAIN queries are allowed")
    if FORBIDDEN_SQL.search(cleaned):
        raise UnsafeQueryError("Query contains forbidden mutating or administrative SQL")
    return cleaned


def bot_filter_sql(settings: Settings, exclude_bots: bool) -> str:
    if not exclude_bots:
        return ""
    return (
        " AND NOT match(lower(ifNull(ingest_user_agent, '')), "
        f"{quote_string(settings.bot_user_agent_regex.lower())})"
    )


def optional_filters_sql(
    *,
    app_id: str | None = None,
    environment: str | None = "prod",
    event_name: str | None = None,
) -> str:
    clauses: list[str] = []
    if app_id:
        clauses.append(f"app_id = {quote_string(app_id)}")
    if environment:
        clauses.append(f"environment = {quote_string(environment)}")
    if event_name:
        clauses.append(f"event_name = {quote_string(event_name)}")
    return (" AND " + " AND ".join(clauses)) if clauses else ""


class ClickHouseGateway:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def client(self):
        import clickhouse_connect

        return clickhouse_connect.get_client(
            host=self.settings.clickhouse_host,
            port=self.settings.clickhouse_port,
            username=self.settings.clickhouse_user,
            password=self.settings.clickhouse_password,
            database=self.settings.clickhouse_database,
            secure=self.settings.clickhouse_secure,
            verify=self.settings.clickhouse_verify,
            settings=self.settings.query_settings,
        )

    def query_rows(self, sql: str) -> list[dict[str, Any]]:
        checked_sql = validate_readonly_sql(sql)
        client = self.client()
        try:
            result = client.query(checked_sql, settings=self.settings.query_settings)
            return [dict(zip(result.column_names, row, strict=False)) for row in result.result_rows]
        finally:
            client.close()
