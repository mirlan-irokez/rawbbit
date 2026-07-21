#!/usr/bin/env bash
set -euo pipefail

: "${POSTGRES_USER:?POSTGRES_USER is required}"
: "${METABASE_DB_NAME:?METABASE_DB_NAME is required}"
: "${METABASE_DB_USER:?METABASE_DB_USER is required}"
: "${METABASE_DB_PASSWORD:?METABASE_DB_PASSWORD is required}"

validate_identifier() {
  local name="$1"
  local value="$2"

  if [[ ! "$value" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]]; then
    echo "Invalid ${name}: ${value}" >&2
    exit 1
  fi
}

quote_identifier() {
  local value="${1//\"/\"\"}"
  printf '"%s"' "$value"
}

quote_literal() {
  local value

  value="$(printf '%s' "$1" | sed "s/'/''/g")"
  printf "'%s'" "$value"
}

psql_superuser() {
  psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres "$@"
}

validate_identifier "Metabase database name" "$METABASE_DB_NAME"
validate_identifier "Metabase database user" "$METABASE_DB_USER"

db_sql="$(quote_identifier "$METABASE_DB_NAME")"
user_sql="$(quote_identifier "$METABASE_DB_USER")"
password_sql="$(quote_literal "$METABASE_DB_PASSWORD")"

if [ "$(psql_superuser -tAc "SELECT 1 FROM pg_roles WHERE rolname = $(quote_literal "$METABASE_DB_USER")")" != "1" ]; then
  psql_superuser -c "CREATE USER ${user_sql} WITH PASSWORD ${password_sql};"
else
  psql_superuser -c "ALTER USER ${user_sql} WITH PASSWORD ${password_sql};"
fi

if [ "$(psql_superuser -tAc "SELECT 1 FROM pg_database WHERE datname = $(quote_literal "$METABASE_DB_NAME")")" != "1" ]; then
  psql_superuser -c "CREATE DATABASE ${db_sql} OWNER ${user_sql};"
fi

psql_superuser -c "ALTER DATABASE ${db_sql} OWNER TO ${user_sql};"
