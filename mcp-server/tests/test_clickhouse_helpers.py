from __future__ import annotations

import pytest

from rawbbit_mcp.clickhouse import UnsafeQueryError, build_funnel_sql, checked_limit, quote_string, validate_readonly_sql
from rawbbit_mcp.settings import Settings


def test_quote_string_escapes_single_quotes() -> None:
    assert quote_string("bob's app") == "'bob\\'s app'"


def test_checked_limit_bounds_values() -> None:
    assert checked_limit(0, 100) == 1
    assert checked_limit(50, 100) == 50
    assert checked_limit(500, 100) == 100


def test_validate_readonly_sql_allows_select() -> None:
    assert validate_readonly_sql("SELECT 1;") == "SELECT 1"


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO analytics.events SELECT 1",
        "SELECT 1; DROP TABLE analytics.events",
        "ALTER TABLE analytics.events DELETE WHERE 1",
    ],
)
def test_validate_readonly_sql_rejects_mutation(sql: str) -> None:
    with pytest.raises(UnsafeQueryError):
        validate_readonly_sql(sql)


def test_build_funnel_sql_uses_ordered_window_funnel() -> None:
    settings = Settings(MCP_API_KEYS_JSON='{"test":"token"}')

    sql = build_funnel_sql(
        settings=settings,
        steps=["tutorial_started", "tutorial_completed", "purchase_completed"],
        start_date="2026-06-01",
        end_date="2026-06-07",
        app_id="com.example.game",
        environment="prod",
        window_hours=24,
        exclude_bots=True,
    )

    assert "windowFunnel(86400)" in sql
    assert "minIf(" not in sql
    assert "event_name = 'tutorial_started'" in sql
    assert "event_name = 'tutorial_completed'" in sql
    assert "event_name = 'purchase_completed'" in sql
    assert "countIf(funnel_step >= 3) AS step_3_users" in sql
