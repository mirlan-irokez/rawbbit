from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from fastmcp import FastMCP

from rawbbit_mcp.clickhouse import (
    JSON_COLUMNS,
    ClickHouseGateway,
    bot_filter_sql,
    build_funnel_sql,
    checked_limit,
    optional_filters_sql,
    quote_string,
)
from rawbbit_mcp.datasets import DatasetRegistry, RuntimeDataset
from rawbbit_mcp.settings import Settings

GatewayFactory = Callable[[Settings], ClickHouseGateway]


@dataclass(frozen=True)
class QueryTarget:
    dataset_id: str
    settings: Settings
    gateway: ClickHouseGateway
    endpoint_path: str


def _dataset_settings(settings: Settings, dataset: RuntimeDataset) -> Settings:
    limits: dict[str, int] = {}
    for name in ("max_query_rows", "max_sample_rows", "max_execution_seconds"):
        override = getattr(dataset, name)
        if override is not None:
            limits[name] = min(getattr(settings, name), override)
    return settings.model_copy(
        update={
            "clickhouse_user": dataset.clickhouse_user,
            "clickhouse_password": dataset.clickhouse_password.get_secret_value(),
            "clickhouse_database": dataset.database,
            "clickhouse_table": dataset.table,
            **limits,
        }
    )


def _target(
    dataset_id: str,
    settings: Settings,
    endpoint_path: str,
    gateway_factory: GatewayFactory,
) -> QueryTarget:
    return QueryTarget(dataset_id, settings, gateway_factory(settings), endpoint_path)


def register_tools(
    mcp: FastMCP,
    settings: Settings,
    registry: DatasetRegistry,
    *,
    gateway_factory: GatewayFactory = ClickHouseGateway,
    scoped_dataset: RuntimeDataset | None = None,
) -> None:
    """Register shared analytics tools with a fixed or main-endpoint dataset scope."""
    default_target = _target("default", settings, settings.mcp_path, gateway_factory)
    by_id: dict[str, QueryTarget] = {}
    for dataset in registry.datasets:
        dataset_settings = _dataset_settings(settings, dataset)
        by_id[dataset.id] = _target(
            dataset.id,
            dataset_settings,
            dataset.endpoint_path,
            gateway_factory,
        )

    fixed_target = None
    if scoped_dataset is not None:
        fixed_target = by_id.get(scoped_dataset.id)
        if fixed_target is None:
            dataset_settings = _dataset_settings(settings, scoped_dataset)
            fixed_target = _target(
                scoped_dataset.id,
                dataset_settings,
                scoped_dataset.endpoint_path,
                gateway_factory,
            )

    exposed_targets = {key: value for key, value in by_id.items() if registry_dataset_exposed(registry, key)}

    def resolve(dataset_id: str | None = None) -> QueryTarget:
        if fixed_target is not None:
            return fixed_target
        if dataset_id is None or dataset_id == "default":
            return default_target
        if dataset_id not in exposed_targets:
            raise ValueError("dataset_id is unknown, disabled, or not exposed on the main MCP endpoint")
        return exposed_targets[dataset_id]

    def table_overview_for(target: QueryTarget, exclude_bots: bool) -> list[dict[str, Any]]:
        cfg = target.settings
        sql = f"""
        SELECT
          count() AS events,
          uniqExact(app_id) AS apps,
          uniqExact(event_name) AS event_names,
          uniqExact(coalesce(nullIf(user_id, ''), user_pseudo_id)) AS actors,
          min(event_time) AS first_event_time,
          max(event_time) AS last_event_time
        FROM {cfg.table_ref}
        WHERE event_time IS NOT NULL
        {bot_filter_sql(cfg, exclude_bots)}
        """
        return target.gateway.query_rows(sql)

    def list_event_names_for(
        target: QueryTarget,
        app_id: str | None,
        environment: str | None,
        limit: int,
        exclude_bots: bool,
    ) -> list[dict[str, Any]]:
        cfg = target.settings
        row_limit = checked_limit(limit, cfg.max_query_rows)
        sql = f"""
        SELECT
          event_name,
          count() AS events,
          uniqExact(coalesce(nullIf(user_id, ''), user_pseudo_id)) AS actors,
          min(event_time) AS first_event_time,
          max(event_time) AS last_event_time
        FROM {cfg.table_ref}
        WHERE event_time IS NOT NULL
        {optional_filters_sql(app_id=app_id, environment=environment)}
        {bot_filter_sql(cfg, exclude_bots)}
        GROUP BY event_name
        ORDER BY events DESC
        LIMIT {row_limit}
        """
        return target.gateway.query_rows(sql)

    def discover_json_keys_for(
        target: QueryTarget,
        json_column: str,
        event_name: str | None,
        app_id: str | None,
        environment: str | None,
        limit: int,
        exclude_bots: bool,
    ) -> list[dict[str, Any]]:
        if json_column not in JSON_COLUMNS:
            return [{"error": f"json_column must be one of: {', '.join(sorted(JSON_COLUMNS))}"}]
        cfg = target.settings
        row_limit = checked_limit(limit, cfg.max_query_rows)
        sql = f"""
        SELECT
          key,
          count() AS rows_with_key
        FROM
        (
          SELECT arrayJoin(JSONExtractKeys(if(empty(ifNull({json_column}, '')), '{{}}', {json_column}))) AS key
          FROM {cfg.table_ref}
          WHERE event_time IS NOT NULL
          {optional_filters_sql(app_id=app_id, environment=environment, event_name=event_name)}
          {bot_filter_sql(cfg, exclude_bots)}
        )
        GROUP BY key
        ORDER BY rows_with_key DESC, key
        LIMIT {row_limit}
        """
        return target.gateway.query_rows(sql)

    def sample_events_for(
        target: QueryTarget,
        event_name: str | None,
        app_id: str | None,
        environment: str | None,
        limit: int,
        exclude_bots: bool,
    ) -> list[dict[str, Any]]:
        cfg = target.settings
        row_limit = checked_limit(limit, cfg.max_sample_rows)
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
        FROM {cfg.table_ref}
        WHERE event_time IS NOT NULL
        {optional_filters_sql(app_id=app_id, environment=environment, event_name=event_name)}
        {bot_filter_sql(cfg, exclude_bots)}
        ORDER BY event_time DESC
        LIMIT {row_limit}
        """
        return target.gateway.query_rows(sql)

    def run_readonly_sql_for(target: QueryTarget, sql: str, limit: int) -> list[dict[str, Any]]:
        from rawbbit_mcp.clickhouse import validate_readonly_sql

        checked = validate_readonly_sql(sql)
        if checked.lower().startswith(("select", "with")) and " limit " not in f" {checked.lower()} ":
            checked = f"SELECT * FROM ({checked}) LIMIT {checked_limit(limit, target.settings.max_query_rows)}"
        return target.gateway.query_rows(checked)

    def calculate_dau_for(
        target: QueryTarget,
        start_date: str,
        end_date: str,
        app_id: str | None,
        environment: str | None,
        active_event_name: str | None,
        exclude_bots: bool,
    ) -> list[dict[str, Any]]:
        cfg = target.settings
        sql = f"""
        SELECT
          event_date,
          uniqExact(coalesce(nullIf(user_id, ''), user_pseudo_id)) AS dau
        FROM {cfg.table_ref}
        WHERE event_date BETWEEN toDate({quote_string(start_date)}) AND toDate({quote_string(end_date)})
          AND event_time IS NOT NULL
        {optional_filters_sql(app_id=app_id, environment=environment, event_name=active_event_name)}
        {bot_filter_sql(cfg, exclude_bots)}
        GROUP BY event_date
        ORDER BY event_date
        """
        return target.gateway.query_rows(sql)

    def calculate_funnel_for(
        target: QueryTarget,
        steps: list[str],
        start_date: str,
        end_date: str,
        app_id: str | None,
        environment: str | None,
        window_hours: int,
        exclude_bots: bool,
    ) -> list[dict[str, Any]]:
        clean_steps = [step.strip() for step in steps if step.strip()]
        if not 2 <= len(clean_steps) <= 10:
            return [{"error": "steps must contain between 2 and 10 event names"}]
        return target.gateway.query_rows(
            build_funnel_sql(
                settings=target.settings,
                steps=clean_steps,
                start_date=start_date,
                end_date=end_date,
                app_id=app_id,
                environment=environment,
                window_hours=window_hours,
                exclude_bots=exclude_bots,
            )
        )

    @mcp.tool
    def healthcheck() -> dict[str, Any]:
        """Check that this endpoint can reach its configured ClickHouse table."""
        target = fixed_target or default_target
        rows = target.gateway.query_rows("SELECT 1 AS ok")
        return {
            "status": "ok" if rows and rows[0].get("ok") == 1 else "unknown",
            "clickhouse_table": target.settings.table_ref,
        }

    if fixed_target is None and registry.datasets:
        @mcp.tool
        def list_datasets() -> list[dict[str, Any]]:
            """List the default dataset and datasets explicitly exposed on this main endpoint."""
            values = [
                {
                    "dataset_id": "default",
                    "endpoint_path": settings.mcp_path,
                    "database": settings.clickhouse_database,
                    "table": settings.clickhouse_table,
                    "is_default": True,
                }
            ]
            values.extend(
                {
                    "dataset_id": dataset.id,
                    "endpoint_path": dataset.endpoint_path,
                    "database": dataset.database,
                    "table": dataset.table,
                    "is_default": False,
                }
                for dataset in registry.datasets
                if dataset.main_exposed
            )
            return values

    if fixed_target is not None:
        @mcp.tool
        def table_overview(exclude_bots: bool = True) -> list[dict[str, Any]]:
            """Summarize the dataset bound to this scoped MCP endpoint."""
            return table_overview_for(fixed_target, exclude_bots)

        @mcp.tool
        def list_event_names(
            app_id: str | None = None,
            environment: str | None = "prod",
            limit: int = 100,
            exclude_bots: bool = True,
        ) -> list[dict[str, Any]]:
            """List event names with counts and observed time ranges."""
            return list_event_names_for(fixed_target, app_id, environment, limit, exclude_bots)

        @mcp.tool
        def discover_json_keys(
            json_column: str = "event_params_json",
            event_name: str | None = None,
            app_id: str | None = None,
            environment: str | None = "prod",
            limit: int = 100,
            exclude_bots: bool = True,
        ) -> list[dict[str, Any]]:
            """Discover top-level JSON keys in one of the dataset's JSON string columns."""
            return discover_json_keys_for(
                fixed_target, json_column, event_name, app_id, environment, limit, exclude_bots
            )

        @mcp.tool
        def sample_events(
            event_name: str | None = None,
            app_id: str | None = None,
            environment: str | None = "prod",
            limit: int = 20,
            exclude_bots: bool = True,
        ) -> list[dict[str, Any]]:
            """Return recent event rows from the dataset bound to this endpoint."""
            return sample_events_for(fixed_target, event_name, app_id, environment, limit, exclude_bots)

        @mcp.tool
        def run_readonly_sql(sql: str, limit: int = 100) -> list[dict[str, Any]]:
            """Run a guarded read-only query using this endpoint's restricted ClickHouse identity."""
            return run_readonly_sql_for(fixed_target, sql, limit)

        @mcp.tool
        def calculate_dau(
            start_date: str,
            end_date: str,
            app_id: str | None = None,
            environment: str | None = "prod",
            active_event_name: str | None = None,
            exclude_bots: bool = True,
        ) -> list[dict[str, Any]]:
            """Calculate daily active users for the dataset bound to this endpoint."""
            return calculate_dau_for(
                fixed_target, start_date, end_date, app_id, environment, active_event_name, exclude_bots
            )

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
            """Calculate ordered user counts for an event-name funnel in this dataset."""
            return calculate_funnel_for(
                fixed_target, steps, start_date, end_date, app_id, environment, window_hours, exclude_bots
            )
        return

    if not registry.datasets:
        @mcp.tool
        def table_overview(exclude_bots: bool = True) -> list[dict[str, Any]]:
            """Summarize the configured Rawbbit ClickHouse events table."""
            return table_overview_for(default_target, exclude_bots)

        @mcp.tool
        def list_event_names(
            app_id: str | None = None,
            environment: str | None = "prod",
            limit: int = 100,
            exclude_bots: bool = True,
        ) -> list[dict[str, Any]]:
            """List event names with counts and observed time ranges."""
            return list_event_names_for(default_target, app_id, environment, limit, exclude_bots)

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
            return discover_json_keys_for(
                default_target, json_column, event_name, app_id, environment, limit, exclude_bots
            )

        @mcp.tool
        def sample_events(
            event_name: str | None = None,
            app_id: str | None = None,
            environment: str | None = "prod",
            limit: int = 20,
            exclude_bots: bool = True,
        ) -> list[dict[str, Any]]:
            """Return recent raw event rows from the configured ClickHouse table."""
            return sample_events_for(default_target, event_name, app_id, environment, limit, exclude_bots)

        @mcp.tool
        def run_readonly_sql(sql: str, limit: int = 100) -> list[dict[str, Any]]:
            """Run a guarded read-only ClickHouse query against Rawbbit analytics data."""
            return run_readonly_sql_for(default_target, sql, limit)

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
            return calculate_dau_for(
                default_target, start_date, end_date, app_id, environment, active_event_name, exclude_bots
            )

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
            return calculate_funnel_for(
                default_target, steps, start_date, end_date, app_id, environment, window_hours, exclude_bots
            )
        return

    @mcp.tool
    def table_overview(exclude_bots: bool = True, dataset_id: str | None = None) -> list[dict[str, Any]]:
        """Summarize the selected Rawbbit ClickHouse events table."""
        return table_overview_for(resolve(dataset_id), exclude_bots)

    @mcp.tool
    def list_event_names(
        app_id: str | None = None,
        environment: str | None = "prod",
        limit: int = 100,
        exclude_bots: bool = True,
        dataset_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """List event names with counts and observed time ranges."""
        return list_event_names_for(resolve(dataset_id), app_id, environment, limit, exclude_bots)

    @mcp.tool
    def discover_json_keys(
        json_column: str = "event_params_json",
        event_name: str | None = None,
        app_id: str | None = None,
        environment: str | None = "prod",
        limit: int = 100,
        exclude_bots: bool = True,
        dataset_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Discover top-level JSON keys in a Rawbbit JSON string column."""
        return discover_json_keys_for(
            resolve(dataset_id), json_column, event_name, app_id, environment, limit, exclude_bots
        )

    @mcp.tool
    def sample_events(
        event_name: str | None = None,
        app_id: str | None = None,
        environment: str | None = "prod",
        limit: int = 20,
        exclude_bots: bool = True,
        dataset_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return recent event rows from the selected dataset."""
        return sample_events_for(resolve(dataset_id), event_name, app_id, environment, limit, exclude_bots)

    @mcp.tool
    def run_readonly_sql(sql: str, limit: int = 100, dataset_id: str | None = None) -> list[dict[str, Any]]:
        """Run a guarded read-only query against the selected authorized dataset."""
        return run_readonly_sql_for(resolve(dataset_id), sql, limit)

    @mcp.tool
    def calculate_dau(
        start_date: str,
        end_date: str,
        app_id: str | None = None,
        environment: str | None = "prod",
        active_event_name: str | None = None,
        exclude_bots: bool = True,
        dataset_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Calculate daily active users for the selected dataset."""
        return calculate_dau_for(
            resolve(dataset_id), start_date, end_date, app_id, environment, active_event_name, exclude_bots
        )

    @mcp.tool
    def calculate_funnel(
        steps: list[str],
        start_date: str,
        end_date: str,
        app_id: str | None = None,
        environment: str | None = "prod",
        window_hours: int = 24,
        exclude_bots: bool = True,
        dataset_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Calculate ordered user counts for an event-name funnel in the selected dataset."""
        return calculate_funnel_for(
            resolve(dataset_id), steps, start_date, end_date, app_id, environment, window_hours, exclude_bots
        )


def registry_dataset_exposed(registry: DatasetRegistry, dataset_id: str) -> bool:
    return any(dataset.id == dataset_id and dataset.main_exposed and dataset.enabled for dataset in registry.datasets)
