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
: "${CLICKHOUSE_DBT_USER:?CLICKHOUSE_DBT_USER is required}"
: "${CLICKHOUSE_DBT_PASSWORD:?CLICKHOUSE_DBT_PASSWORD is required}"

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

enable_admin_named_collection_control() {
  local config_file="/etc/clickhouse-server/users.d/rawbbit-admin-named-collection.xml"

  cat >"$config_file" <<EOF
<clickhouse>
  <users>
    <${CLICKHOUSE_USER}>
      <named_collection_control>1</named_collection_control>
    </${CLICKHOUSE_USER}>
  </users>
</clickhouse>
EOF
  chmod 0644 "$config_file"
  run_sql "SYSTEM RELOAD CONFIG;"
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

validate_identifier "ClickHouse admin user" "$CLICKHOUSE_USER"
validate_identifier "ClickHouse database" "$CLICKHOUSE_DATABASE"
validate_identifier "ClickHouse table" "$CLICKHOUSE_TABLE"

# The official container grants access management to its configured admin but
# named-collection delegation is controlled separately in ClickHouse 24.8.
# Generate a secret-free users.d overlay for the configured admin before
# delegating access to the dbt service user.
enable_admin_named_collection_control

db_sql="$(quote_identifier "$CLICKHOUSE_DATABASE")"
table_sql="$(quote_identifier "$CLICKHOUSE_TABLE")"

create_or_update_user "$CLICKHOUSE_MCP_USER" "$CLICKHOUSE_MCP_PASSWORD"
create_or_update_user "$CLICKHOUSE_METABASE_USER" "$CLICKHOUSE_METABASE_PASSWORD"
create_or_update_user "$CLICKHOUSE_LOADER_USER" "$CLICKHOUSE_LOADER_PASSWORD"
create_or_update_user "$CLICKHOUSE_DBT_USER" "$CLICKHOUSE_DBT_PASSWORD"

mcp_user_sql="$(quote_identifier "$CLICKHOUSE_MCP_USER")"
metabase_user_sql="$(quote_identifier "$CLICKHOUSE_METABASE_USER")"
loader_user_sql="$(quote_identifier "$CLICKHOUSE_LOADER_USER")"
dbt_user_sql="$(quote_identifier "$CLICKHOUSE_DBT_USER")"

run_sql "
  GRANT SELECT, SHOW ON ${db_sql}.* TO ${mcp_user_sql};
  GRANT SELECT, SHOW ON ${db_sql}.* TO ${metabase_user_sql};
  GRANT INSERT ON ${db_sql}.${table_sql} TO ${loader_user_sql};
  GRANT CREATE TEMPORARY TABLE, S3 ON *.* TO ${loader_user_sql};

  GRANT SELECT, SHOW, INSERT, CREATE TABLE, CREATE VIEW, DROP TABLE, DROP VIEW
    ON ${db_sql}.* TO ${dbt_user_sql};
  GRANT ALTER DELETE ON ${db_sql}.* TO ${dbt_user_sql};
  GRANT ALTER UPDATE(_row_exists) ON ${db_sql}.${table_sql} TO ${dbt_user_sql};
  GRANT CREATE TEMPORARY TABLE, S3 ON *.* TO ${dbt_user_sql};
  GRANT NAMED COLLECTION ON rawbbit_raw_s3 TO ${dbt_user_sql};
"
