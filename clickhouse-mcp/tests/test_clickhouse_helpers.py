from __future__ import annotations

import pytest

from rawbbit_mcp.clickhouse import UnsafeQueryError, checked_limit, quote_string, validate_readonly_sql


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
