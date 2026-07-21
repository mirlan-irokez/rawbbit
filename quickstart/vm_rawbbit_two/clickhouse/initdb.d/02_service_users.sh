#!/usr/bin/env bash
set -euo pipefail

: "${CLICKHOUSE_USER:?CLICKHOUSE_USER is required}"
: "${CLICKHOUSE_PASSWORD:?CLICKHOUSE_PASSWORD is required}"
: "${CLICKHOUSE_DATABASE:?CLICKHOUSE_DATABASE is required}"
: "${CLICKHOUSE_TABLE:?CLICKHOUSE_TABLE is required}"
: "${CLICKHOUSE_MCP_USER:?CLICKHOUSE_MCP_USER is required}"
: "${CLICKHOUSE_MCP_PASSWORD:?CLICKHOUSE_MCP_PASSWORD is required}"
: "${CLICKHOUSE_METABASE_USER:?CLICKHOUSE_METABASE_USER is required}"
: "${CLICKHOUSE_METABASE_PASSWORD:?CLICKHOUSE_METABASE_PASSWORD is required}"
: "${CLICKHOUSE_LOADER_USER:?CLICKHOUSE_LOADER_USER is required}"
: "${CLICKHOUSE_LOADER_PASSWORD:?CLICKHOUSE_LOADER_PASSWORD is required}"

validate_identifier() {
  local name="$1"
  local value="$2"

  if [[ ! "$value" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]]; then
    echo "Invalid ${name}: ${value}" >&2
    exit 1
  fi
}

quote_identifier() {
  printf '`%s`' "$1"
}

quote_string() {
  local value

  value="$(printf '%s' "$1" | sed "s/'/''/g")"
  printf "'%s'" "$value"
}

run_sql() {
  clickhouse-client \
    --user "$CLICKHOUSE_USER" \
    --password "$CLICKHOUSE_PASSWORD" \
    --multiquery \
    --query "$1"
}

create_or_update_user() {
  local user="$1"
  local password="$2"
  local user_sql
  local password_sql

  validate_identifier "ClickHouse user" "$user"
  user_sql="$(quote_identifier "$user")"
  password_sql="$(quote_string "$password")"

  run_sql "
    CREATE USER IF NOT EXISTS ${user_sql}
      IDENTIFIED WITH sha256_password BY ${password_sql};
    ALTER USER ${user_sql}
      IDENTIFIED WITH sha256_password BY ${password_sql};
  "
}

validate_identifier "ClickHouse database" "$CLICKHOUSE_DATABASE"
validate_identifier "ClickHouse table" "$CLICKHOUSE_TABLE"

db_sql="$(quote_identifier "$CLICKHOUSE_DATABASE")"
table_sql="$(quote_identifier "$CLICKHOUSE_TABLE")"

create_or_update_user "$CLICKHOUSE_MCP_USER" "$CLICKHOUSE_MCP_PASSWORD"
create_or_update_user "$CLICKHOUSE_METABASE_USER" "$CLICKHOUSE_METABASE_PASSWORD"
create_or_update_user "$CLICKHOUSE_LOADER_USER" "$CLICKHOUSE_LOADER_PASSWORD"

mcp_user_sql="$(quote_identifier "$CLICKHOUSE_MCP_USER")"
metabase_user_sql="$(quote_identifier "$CLICKHOUSE_METABASE_USER")"
loader_user_sql="$(quote_identifier "$CLICKHOUSE_LOADER_USER")"

run_sql "
  GRANT SELECT, SHOW ON ${db_sql}.* TO ${mcp_user_sql};
  GRANT SELECT, SHOW ON ${db_sql}.* TO ${metabase_user_sql};
  GRANT INSERT ON ${db_sql}.${table_sql} TO ${loader_user_sql};
  GRANT CREATE TEMPORARY TABLE, S3 ON *.* TO ${loader_user_sql};
"
