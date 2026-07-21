CREATE DATABASE IF NOT EXISTS analytics;

CREATE TABLE IF NOT EXISTS analytics.events
(
    event_id Nullable(String),
    app_id LowCardinality(String),
    environment LowCardinality(String),
    event_name LowCardinality(String),

    event_time DateTime64(3, 'UTC'),
    event_date Date,

    received_time Nullable(DateTime64(3, 'UTC')),
    user_id Nullable(String),
    user_pseudo_id String,
    session_id Nullable(String),
    platform LowCardinality(Nullable(String)),
    app_version Nullable(String),
    os_version Nullable(String),
    device_model Nullable(String),
    locale Nullable(String),
    timezone Nullable(String),
    event_params_json Nullable(String),
    user_properties_json Nullable(String),
    traffic_source_json Nullable(String),
    geo_json Nullable(String),
    consent_json Nullable(String),

    ingest_request_id Nullable(String),
    ingest_user_agent Nullable(String),
    ingest_ip_hash Nullable(String),
    nats_stream Nullable(String),
    nats_sequence Nullable(Int64)
)
ENGINE=MergeTree
PARTITION BY toYYYYMM(event_date)
ORDER BY (app_id, environment, event_name, event_date, user_pseudo_id, event_time);
