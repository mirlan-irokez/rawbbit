# Configuration

The canonical environment-variable reference for the public stack is `deploy/.env.example`.

This guide groups the main settings by responsibility.

## NATS and stream settings

Main variables:

- `NATS_URL`
- `NATS_STREAM`
- `NATS_SUBJECT_PREFIX`
- `NATS_STREAM_MAX_AGE_SECONDS`
- `NATS_DUPLICATE_WINDOW_SECONDS`

These settings control the durable buffer between ingress and raw storage.

## Collector API settings

Main variables:

- `PORT`
- `COLLECTOR_API_KEYS_JSON`
- `MAX_EVENTS_PER_REQUEST`
- `MAX_REQUEST_BYTES`
- `IP_HASH_SALT`
- `STORE_RAW_IP`
- `GEOIP_ENABLED`
- `GEOIP_MMDB_PATH`
- `CORS_ALLOW_ORIGINS`
- `CORS_ALLOW_METHODS`
- `CORS_ALLOW_HEADERS`
- `CORS_ALLOW_CREDENTIALS`
- `CORS_MAX_AGE_SECONDS`

Notes:

- `COLLECTOR_API_KEYS_JSON` is a JSON map of `{ "api_key": "app_id" }`
- browser ingestion requires correct HTTPS and CORS configuration
- API keys, salts, and credentials should come from private environment or secret management, not committed files
- if GeoIP enrichment uses DB-IP data and your web application displays or uses those geolocation results, include the required attribution link in the UI: `<a href='https://db-ip.com'>IP Geolocation by DB-IP</a>`
  - download IP database, double check license requirements and attribution before using - https://db-ip.com/db/download/ip-to-country-lite 

## Raw-writer settings

Main variables:

- `NATS_CONSUMER`
- `NATS_FETCH_BATCH`
- `NATS_FETCH_TIMEOUT_SECONDS`
- `NATS_ACK_WAIT_SECONDS`
- `NATS_MAX_DELIVER`
- `RAW_FLUSH_INTERVAL_SECONDS`
- `RAW_MAX_EVENTS_PER_FILE`
- `RAW_MAX_BYTES_PER_FILE`

These settings control how aggressively queued events are batched and flushed into Parquet.

## Object-storage settings

Main variables:

- `GCS_RAW_BUCKET`
- `GCS_RAW_PREFIX`
- `GOOGLE_APPLICATION_CREDENTIALS`

Notes:

- `GCS_RAW_BUCKET` is required for durable raw landing
- `GCS_RAW_PREFIX` defaults to `raw`
- in the current deployment, `GOOGLE_APPLICATION_CREDENTIALS` usually points to the mounted service-account JSON inside the container

## Raw partition layout

Expected object layout:

```text
<GCS_RAW_PREFIX>/app_id=<app_id>/event_date=YYYY-MM-DD/hour=HH/
```

- `event_date` and `hour` are derived from `event_timestamp` when valid
- otherwise they fall back to collector receive time

## Raw Parquet columns

Current raw output includes:

- `event_id`, `app_id`, `environment`, `event_name`, `event_timestamp`, `received_at`
- `user_id`, `user_pseudo_id`, `session_id`
- `platform`, `app_version`, `os_version`, `device_model`, `locale`, `timezone`
- `event_params_json`, `user_properties_json`, `traffic_source_json`, `geo_json`, `consent_json`
- `ingest_request_id`, `ingest_user_agent`, `ingest_ip_hash`
- `nats_stream`, `nats_sequence`

The raw layer stays intentionally simple and mostly string-typed for compatibility and portability.
