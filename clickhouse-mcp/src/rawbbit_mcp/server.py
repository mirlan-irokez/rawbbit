from __future__ import annotations

import hmac
import logging
from typing import Any

import uvicorn
from fastmcp import FastMCP
from fastmcp.server.auth import JWTVerifier
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from rawbbit_mcp.clickhouse import (
    JSON_COLUMNS,
    ClickHouseGateway,
    bot_filter_sql,
    checked_limit,
    optional_filters_sql,
    quote_string,
    validate_readonly_sql,
)
from rawbbit_mcp.settings import Settings, get_settings

settings = get_settings()

logging.basicConfig(
    level=settings.log_level.upper(),
    format="%(asctime)s %(levelname)s service=rawbbit-mcp event=%(message)s",
)
logger = logging.getLogger("rawbbit_mcp")

gateway = ClickHouseGateway(settings)


def _build_auth(cfg: Settings):
    if cfg.auth_mode == "static_tokens":
        return None

    if cfg.jwt_jwks_uri:
        kwargs: dict[str, str] = {"jwks_uri": cfg.jwt_jwks_uri}
        if cfg.jwt_issuer:
            kwargs["issuer"] = cfg.jwt_issuer
        if cfg.jwt_audience:
            kwargs["audience"] = cfg.jwt_audience
        return JWTVerifier(**kwargs)

    if cfg.jwt_public_key:
        kwargs = {"public_key": cfg.jwt_public_key}
        if cfg.jwt_issuer:
            kwargs["issuer"] = cfg.jwt_issuer
        if cfg.jwt_audience:
            kwargs["audience"] = cfg.jwt_audience
        return JWTVerifier(**kwargs)

    logger.warning("auth_disabled reason=no_jwt_verifier_configured")
    return None


def _extract_bearer_token(authorization_header: str | None) -> str | None:
    if not authorization_header:
        return None
    scheme, _, token = authorization_header.partition(" ")
    if scheme.lower() != "bearer":
        return None
    normalized = token.strip()
    return normalized or None


def _resolve_static_token_label(cfg: Settings, token: str | None) -> str | None:
    if not token:
        return None
    for label, candidate in cfg.api_keys_by_user.items():
        if hmac.compare_digest(candidate, token):
            return label
    return None


class StaticBearerAuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, cfg: Settings) -> None:
        super().__init__(app)
        self.cfg = cfg

    async def dispatch(self, request: Request, call_next) -> Response:
        if request.method == "OPTIONS":
            return await call_next(request)

        token = _extract_bearer_token(request.headers.get("authorization"))
        label = _resolve_static_token_label(self.cfg, token)
        if label is None:
            return JSONResponse(
                {"error": "Unauthorized"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )

        request.state.authenticated_api_key_label = label
        return await call_next(request)


def _build_http_middleware(cfg: Settings) -> list[Middleware]:
    if cfg.auth_mode != "static_tokens":
        return []
    return [Middleware(StaticBearerAuthMiddleware, cfg=cfg)]


mcp = FastMCP(settings.mcp_name, auth=_build_auth(settings))
app = mcp.http_app(path=settings.mcp_path, middleware=_build_http_middleware(settings))


@mcp.tool
def healthcheck() -> dict[str, Any]:
    """Check that the MCP server can reach ClickHouse."""
    rows = gateway.query_rows("SELECT 1 AS ok")
    return {
        "status": "ok" if rows and rows[0].get("ok") == 1 else "unknown",
        "clickhouse_table": settings.table_ref,
    }


@mcp.tool
def table_overview(exclude_bots: bool = True) -> list[dict[str, Any]]:
    """Summarize the configured Rawbbit ClickHouse events table."""
    sql = f"""
    SELECT
      count() AS events,
      uniqExact(app_id) AS apps,
      uniqExact(event_name) AS event_names,
      uniqExact(coalesce(nullIf(user_id, ''), user_pseudo_id)) AS actors,
      min(event_time) AS first_event_time,
      max(event_time) AS last_event_time
    FROM {settings.table_ref}
    WHERE event_time IS NOT NULL
    {bot_filter_sql(settings, exclude_bots)}
    """
    return gateway.query_rows(sql)


@mcp.tool
def list_event_names(
    app_id: str | None = None,
    environment: str | None = "prod",
    limit: int = 100,
    exclude_bots: bool = True,
) -> list[dict[str, Any]]:
    """List event names with counts and observed time ranges."""
    row_limit = checked_limit(limit, settings.max_query_rows)
    sql = f"""
    SELECT
      event_name,
      count() AS events,
      uniqExact(coalesce(nullIf(user_id, ''), user_pseudo_id)) AS actors,
      min(event_time) AS first_event_time,
      max(event_time) AS last_event_time
    FROM {settings.table_ref}
    WHERE event_time IS NOT NULL
    {optional_filters_sql(app_id=app_id, environment=environment)}
    {bot_filter_sql(settings, exclude_bots)}
    GROUP BY event_name
    ORDER BY events DESC
    LIMIT {row_limit}
    """
    return gateway.query_rows(sql)


@mcp.tool
def discover_json_keys(
    json_column: str = "event_params_json",
    event_name: str | None = None,
    app_id: str | None = None,
    environment: str | None = "prod",
    limit: int = 100,
    exclude_bots: bool = True,
) -> list[dict[str, Any]]:
    """Discover top-level JSON keys in one of the Rawbbit JSON string columns."""
    if json_column not in JSON_COLUMNS:
        return [{"error": f"json_column must be one of: {', '.join(sorted(JSON_COLUMNS))}"}]

    row_limit = checked_limit(limit, settings.max_query_rows)
    sql = f"""
    SELECT
      key,
      count() AS rows_with_key
    FROM
    (
      SELECT arrayJoin(JSONExtractKeys(if(empty(ifNull({json_column}, '')), '{{}}', {json_column}))) AS key
      FROM {settings.table_ref}
      WHERE event_time IS NOT NULL
      {optional_filters_sql(app_id=app_id, environment=environment, event_name=event_name)}
      {bot_filter_sql(settings, exclude_bots)}
    )
    GROUP BY key
    ORDER BY rows_with_key DESC, key
    LIMIT {row_limit}
    """
    return gateway.query_rows(sql)


@mcp.tool
def sample_events(
    event_name: str | None = None,
    app_id: str | None = None,
    environment: str | None = "prod",
    limit: int = 20,
    exclude_bots: bool = True,
) -> list[dict[str, Any]]:
    """Return recent raw event rows from the configured ClickHouse table."""
    row_limit = checked_limit(limit, settings.max_sample_rows)
    sql = f"""
    SELECT
      event_id,
      app_id,
      environment,
      event_name,
      event_time,
      coalesce(nullIf(user_id, ''), user_pseudo_id) AS actor_id,
      session_id,
      platform,
      event_params_json,
      geo_json,
      ingest_user_agent
    FROM {settings.table_ref}
    WHERE event_time IS NOT NULL
    {optional_filters_sql(app_id=app_id, environment=environment, event_name=event_name)}
    {bot_filter_sql(settings, exclude_bots)}
    ORDER BY event_time DESC
    LIMIT {row_limit}
    """
    return gateway.query_rows(sql)


@mcp.tool
def run_readonly_sql(sql: str, limit: int = 100) -> list[dict[str, Any]]:
    """Run a guarded read-only ClickHouse query against Rawbbit analytics data."""
    checked = validate_readonly_sql(sql)
    if checked.lower().startswith(("select", "with")) and " limit " not in f" {checked.lower()} ":
        checked = f"SELECT * FROM ({checked}) LIMIT {checked_limit(limit, settings.max_query_rows)}"
    return gateway.query_rows(checked)


@mcp.tool
def calculate_dau(
    start_date: str,
    end_date: str,
    app_id: str | None = None,
    environment: str | None = "prod",
    active_event_name: str | None = None,
    exclude_bots: bool = True,
) -> list[dict[str, Any]]:
    """Calculate daily active users by actor_id for the configured events table."""
    sql = f"""
    SELECT
      event_date,
      uniqExact(coalesce(nullIf(user_id, ''), user_pseudo_id)) AS dau
    FROM {settings.table_ref}
    WHERE event_date BETWEEN toDate({quote_string(start_date)}) AND toDate({quote_string(end_date)})
      AND event_time IS NOT NULL
    {optional_filters_sql(app_id=app_id, environment=environment, event_name=active_event_name)}
    {bot_filter_sql(settings, exclude_bots)}
    GROUP BY event_date
    ORDER BY event_date
    """
    return gateway.query_rows(sql)


@mcp.tool
def calculate_funnel(
    steps: list[str],
    start_date: str,
    end_date: str,
    app_id: str | None = None,
    environment: str | None = "prod",
    window_hours: int = 24,
    exclude_bots: bool = True,
) -> list[dict[str, Any]]:
    """Calculate ordered user counts for an event-name funnel."""
    clean_steps = [step.strip() for step in steps if step.strip()]
    if not 2 <= len(clean_steps) <= 10:
        return [{"error": "steps must contain between 2 and 10 event names"}]

    step_set = ", ".join(quote_string(step) for step in clean_steps)
    min_columns = ",\n      ".join(
        f"minIf(event_time, event_name = {quote_string(step)}) AS step_{idx}"
        for idx, step in enumerate(clean_steps, start=1)
    )
    counts = []
    for idx, step in enumerate(clean_steps, start=1):
        if idx == 1:
            condition = "step_1 IS NOT NULL"
        else:
            previous = " AND ".join(f"step_{i} IS NOT NULL" for i in range(1, idx + 1))
            ordered = " AND ".join(f"step_{i} >= step_{i - 1}" for i in range(2, idx + 1))
            within_window = f"step_{idx} <= step_1 + INTERVAL {max(1, window_hours)} HOUR"
            condition = f"{previous} AND {ordered} AND {within_window}"
        counts.append(f"countIf({condition}) AS step_{idx}_users")

    sql = f"""
    WITH per_actor AS
    (
      SELECT
        coalesce(nullIf(user_id, ''), user_pseudo_id) AS actor_id,
        {min_columns}
      FROM {settings.table_ref}
      WHERE event_date BETWEEN toDate({quote_string(start_date)}) AND toDate({quote_string(end_date)})
        AND event_time IS NOT NULL
        AND event_name IN ({step_set})
      {optional_filters_sql(app_id=app_id, environment=environment)}
      {bot_filter_sql(settings, exclude_bots)}
      GROUP BY actor_id
    )
    SELECT
      {", ".join(counts)}
    FROM per_actor
    """
    return gateway.query_rows(sql)


if __name__ == "__main__":
    logger.info(
        "startup env=%s table=%s host=%s port=%s path=%s auth_mode=%s auth_enabled=%s",
        settings.env,
        settings.table_ref,
        settings.mcp_host,
        settings.mcp_port,
        settings.mcp_path,
        settings.auth_mode,
        settings.auth_mode != "none",
    )
    uvicorn.run(
        app,
        host=settings.mcp_host,
        port=settings.mcp_port,
    )
