# Quickstart

This quickstart walks through the supported local validation path for the public Rawbbit stack.

Scope note:
- this quickstart includes both supported raw-storage backends for local validation
- if you want the documented BigQuery external-table query path afterward, use GCS

## Prerequisites

You need:

- Docker and Docker Compose
- a local clone of the repository
- a machine or VM that can run Docker Compose
- one raw-storage target:
  - either a GCS bucket plus GCP credentials (GCP service account that can write objects to that bucket)
  - or an S3-compatible bucket plus endpoint/credentials

## 1. Prepare the environment file

Copy the example environment file:

```bash
cp deploy/.env.example deploy/.env
```

Set at least these values:

```text
COLLECTOR_API_KEYS_JSON={"dev-api-key":"com.example.app"}
IP_HASH_SALT=replace_me
RAW_STORAGE_BACKEND=gcs
GCS_RAW_BUCKET=your-bucket-name
GCS_RAW_PREFIX=raw
```

For S3-compatible mode instead, set:

```text
COLLECTOR_API_KEYS_JSON={"dev-api-key":"com.example.app"}
IP_HASH_SALT=replace_me
RAW_STORAGE_BACKEND=s3
S3_ENDPOINT_URL=http://seaweedfs:8333
S3_BUCKET=your-bucket-name
S3_PREFIX=raw
S3_ACCESS_KEY=your-access-key
S3_SECRET_KEY=your-secret-key
S3_REGION=us-east-1
S3_FORCE_PATH_STYLE=1
S3_USE_SSL=0
S3_VERIFY_SSL=0
```

## 2. Prepare credentials for `raw-writer`

For GCS mode, the current Compose setup expects a service-account key inside the container at:

```text
/var/secrets/gcp/sa.json
```

Before starting the stack, update the host-side mount in `deploy/docker-compose.yml` so it points to your real key file.

For S3-compatible mode, no Google key is needed.
Instead, make sure the configured endpoint and bucket are reachable and the `S3_*` values are correct.

## 3. Start the stack

```bash
docker compose -f deploy/docker-compose.yml up --build
```

Expected services:

- `nats`
- `collector-api`
- `raw-writer`

## 4. Send a test event

```bash
curl -sS -X POST http://localhost:8080/v1/events:batch \
  -H 'Content-Type: application/json' \
  -H 'X-API-Key: dev-api-key' \
  -d '{"events":[{"event_id":"00000000-0000-0000-0000-000000000001","app_id":"com.example.app","environment":"dev","event_name":"test_event","event_timestamp":"2026-03-20T18:42:15.123Z"}]}'
```

Expected response shape:

```json
{"request_id":"...","accepted_events":1}
```

## 5. Verify durable landing

A successful validation means:

- the collector accepted the batch
- the event was published into JetStream
- `raw-writer` flushed a Parquet object to the configured storage backend

Expected partition layout:

```text
raw/app_id=com.example.app/event_date=YYYY-MM-DD/hour=HH/
```

## 6. Troubleshooting checks

If the flow does not complete, check:

- invalid or missing `X-API-Key`
- missing required storage-backend settings
- wrong mounted service-account path for GCS mode
- collector running but writer unable to authenticate to the selected storage backend
- NATS healthy but writer not consuming

## Next

- `architecture.md`
- `configuration.md`
