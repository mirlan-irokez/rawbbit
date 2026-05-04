MODEL (
  name staging.base_dataquery__events,
  kind INCREMENTAL_BY_TIME_RANGE (
    time_column received_at_ts,
    lookback 6,
    partition_by_time_column false
  ),
  dialect bigquery,
  start '2026-03-30',
  cron '@hourly',
  partitioned_by (event_date),
  allow_partials true,
  grain (app_id, event_id),
  column_descriptions (
    event_id = 'Unique event identifier',
    app_id = 'Application identifier',
    environment = 'Application environment',
    event_name = 'Event name',
    event_timestamp = 'Event timestamp in UTC',
    received_at = 'Raw event receive timestamp in UTC',
    user_id = 'User unique identifier',
    user_pseudo_id = 'User pseudo identifier',
    session_id = 'Session identifier',
    platform = 'Platform',
    app_version = 'Application version',
    os_version = 'Operating system version',
    device_model = 'Device model',
    locale = 'Locale, device language setting',
    timezone = 'Timezone of the user',
    event_params_json = 'JSON string of event parameters',
    user_properties_json = 'JSON string of user properties',
    traffic_source_json = 'JSON string of traffic source',
    geo_json = 'JSON string of geo location',
    consent_json = 'JSON string of consent',
    ingest_request_id = 'Unique identifier for the ingestion request',
    ingest_user_agent = 'User agent of the ingestion request',
    ingest_ip_hash = 'IP hash of the ingestion request',
    nats_stream = 'NATS stream name',
    nats_sequence = 'NATS sequence number',
    event_date = 'UTC calendar date derived',
    hour = 'UTC hour derived',
    geo_country_code = 'ISO country code extracted from geo_json'
  )
);

JINJA_QUERY_BEGIN;
{% set promoted_events = var('promoted_events') %}
{% set raw_fields = promoted_events['raw_fields'] %}
{% set enabled_promoted_fields = promoted_events['promoted_fields'] | selectattr('enabled') | list %}
WITH source AS (
  SELECT
    {%- for col in raw_fields %}
    {{ col }}{{ "," if not loop.last }}
    {%- endfor %}
    ,SAFE.PARSE_TIMESTAMP('%Y-%m-%dT%H:%M:%E*S%Ez', received_at) AS received_at_ts
  FROM {{ promoted_events['source_table'] }}
  WHERE event_date BETWEEN DATE(@start_dt) AND DATE(@end_dt)
      AND SAFE.PARSE_TIMESTAMP('%Y-%m-%dT%H:%M:%E*S%Ez', received_at) >= @start_dt
      AND SAFE.PARSE_TIMESTAMP('%Y-%m-%dT%H:%M:%E*S%Ez', received_at) < @end_dt
),

deduplicated AS (
  SELECT
    *,
    ROW_NUMBER() OVER (
      PARTITION BY app_id, event_id
      ORDER BY
        received_at_ts DESC NULLS LAST,
        SAFE_CAST(nats_sequence AS INT64) DESC NULLS LAST,
        event_timestamp DESC NULLS LAST,
        ingest_request_id DESC NULLS LAST
    ) AS _dedupe_rank
  FROM source
)

SELECT
  {%- for col in raw_fields %}
  {{ col }},
  {%- endfor %}
  {%- for field in enabled_promoted_fields %}
  {{ field['expression'] }}{{ "," if not loop.last }}
  {%- endfor %}
  ,received_at_ts
FROM deduplicated
WHERE _dedupe_rank = 1
;
JINJA_END;
