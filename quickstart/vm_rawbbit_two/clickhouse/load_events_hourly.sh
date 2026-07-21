#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
QUICKSTART_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

cd "${RAWBBIT_TWO_QUICKSTART_DIR:-${QUICKSTART_DIR}}"

ENV_FILE=".env"
if [ ! -f "$ENV_FILE" ]; then
  echo ".env file not found in $(pwd)" >&2
  exit 1
fi

load_env_key() {
  local key="$1"
  local line=""
  local value=""

  line="$(grep -E "^${key}=" "$ENV_FILE" | tail -n 1 || true)"
  if [ -z "$line" ]; then
    return 0
  fi

  value="${line#*=}"
  value="${value%$'\r'}"

  if [[ "$value" == \"*\" && "$value" == *\" ]]; then
    value="${value:1:${#value}-2}"
  elif [[ "$value" == \'*\' && "$value" == *\' ]]; then
    value="${value:1:${#value}-2}"
  fi

  printf -v "$key" '%s' "$value"
  export "$key"
}

for key in \
  CLICKHOUSE_LOADER_USER \
  CLICKHOUSE_LOADER_PASSWORD \
  CLICKHOUSE_RAW_S3_ACCESS_KEY \
  CLICKHOUSE_RAW_S3_SECRET_KEY \
  CLICKHOUSE_SEAWEED_S3_ENDPOINT \
  CLICKHOUSE_RAW_S3_BUCKET \
  CLICKHOUSE_RAW_S3_PREFIX
do
  load_env_key "$key"
done

: "${CLICKHOUSE_LOADER_USER:?CLICKHOUSE_LOADER_USER is required}"
: "${CLICKHOUSE_LOADER_PASSWORD:?CLICKHOUSE_LOADER_PASSWORD is required}"
: "${CLICKHOUSE_RAW_S3_ACCESS_KEY:?CLICKHOUSE_RAW_S3_ACCESS_KEY is required}"
: "${CLICKHOUSE_RAW_S3_SECRET_KEY:?CLICKHOUSE_RAW_S3_SECRET_KEY is required}"
: "${CLICKHOUSE_SEAWEED_S3_ENDPOINT:?CLICKHOUSE_SEAWEED_S3_ENDPOINT is required}"
: "${CLICKHOUSE_RAW_S3_BUCKET:?CLICKHOUSE_RAW_S3_BUCKET is required}"
: "${CLICKHOUSE_RAW_S3_PREFIX:?CLICKHOUSE_RAW_S3_PREFIX is required}"

S3_ENDPOINT="${CLICKHOUSE_SEAWEED_S3_ENDPOINT%/}"
S3_BUCKET="${CLICKHOUSE_RAW_S3_BUCKET%/}"
S3_PREFIX="${CLICKHOUSE_RAW_S3_PREFIX%/}"

HOUR_PATH=$(date -u -d "1 hour ago" +"event_date=%Y-%m-%d/hour=%H")
RAW_S3_URL="${S3_ENDPOINT}/${S3_BUCKET}/${S3_PREFIX}/*/${HOUR_PATH}/*.parquet"
LIST_URI="s3://${S3_BUCKET}/${S3_PREFIX}/"

LIST_OUTPUT=$(mktemp)
MATCHED_FILES=$(mktemp)
trap 'rm -f "$LIST_OUTPUT" "$MATCHED_FILES"' EXIT

echo "$(date -u '+%F %T') checking ${HOUR_PATH}"
echo "$(date -u '+%F %T') listing ${LIST_URI} via ${S3_ENDPOINT}"

AWS_ACCESS_KEY_ID="$CLICKHOUSE_RAW_S3_ACCESS_KEY" \
AWS_SECRET_ACCESS_KEY="$CLICKHOUSE_RAW_S3_SECRET_KEY" \
aws --endpoint-url "$S3_ENDPOINT" \
  s3 ls "$LIST_URI" --recursive \
  > "$LIST_OUTPUT"

grep -F "${HOUR_PATH}/" "$LIST_OUTPUT" | grep -E '\.parquet$' > "$MATCHED_FILES" || true

if [ ! -s "$MATCHED_FILES" ]; then
  echo "$(date -u '+%F %T') no parquet files for ${HOUR_PATH}, skipping"
  exit 0
fi

echo "$(date -u '+%F %T') loading ${HOUR_PATH}"
echo "$(date -u '+%F %T') matched $(wc -l < "$MATCHED_FILES") parquet files"

docker compose exec -T clickhouse clickhouse-client \
  -u "$CLICKHOUSE_LOADER_USER" \
  --password "$CLICKHOUSE_LOADER_PASSWORD" \
  --query "
  INSERT INTO analytics.events
  SELECT
      event_id,
      ifNull(app_id, '') AS app_id,
      ifNull(environment, '') AS environment,
      ifNull(event_name, '') AS event_name,
      event_time,
      toDate(event_time) AS event_date,
      received_time,
      user_id,
      ifNull(user_pseudo_id, '') AS user_pseudo_id,
      session_id,
      platform,
      app_version,
      os_version,
      device_model,
      locale,
      timezone,
      event_params_json,
      user_properties_json,
      traffic_source_json,
      geo_json,
      consent_json,
      ingest_request_id,
      ingest_user_agent,
      ingest_ip_hash,
      nats_stream,
      nats_sequence
  FROM
  (
      SELECT DISTINCT
          *,
          parseDateTime64BestEffortOrNull(event_timestamp, 3, 'UTC') AS event_time,
          parseDateTime64BestEffortOrNull(received_at, 3, 'UTC') AS received_time
      FROM s3(
                 '${RAW_S3_URL}',
                  '${CLICKHOUSE_RAW_S3_ACCESS_KEY}',
                  '${CLICKHOUSE_RAW_S3_SECRET_KEY}',
                  'Parquet')
  )
  WHERE event_time IS NOT NULL
  "

echo "$(date -u '+%F %T') loaded ${HOUR_PATH}"
