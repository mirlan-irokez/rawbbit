#!/usr/bin/env bash
set -euo pipefail

cd /opt/clickhouse
source .env

HOUR_PATH=$(date -u -d "1 hour ago" +"event_date=%Y-%m-%d/hour=%H")

: "${CLICKHOUSE_USER:?CLICKHOUSE_USER is required}"
: "${CLICKHOUSE_PASSWORD:?CLICKHOUSE_PASSWORD is required}"
: "${CLICKHOUSE_RAW_S3_BASE_URL:?CLICKHOUSE_RAW_S3_BASE_URL is required}"
: "${CLICKHOUSE_RAW_S3_ACCESS_KEY:?CLICKHOUSE_RAW_S3_ACCESS_KEY is required}"
: "${CLICKHOUSE_RAW_S3_SECRET_KEY:?CLICKHOUSE_RAW_S3_SECRET_KEY is required}"

RAW_S3_URL="${CLICKHOUSE_RAW_S3_BASE_URL%/}/*/${HOUR_PATH}/*.parquet"

docker exec -i clickhouse clickhouse-client \
  -u "$CLICKHOUSE_USER" \
  --password "$CLICKHOUSE_PASSWORD" \
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
