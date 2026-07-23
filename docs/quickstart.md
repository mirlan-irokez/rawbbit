# Quickstart

This quickstart walks through the supported local validation path for the public Rawbbit stack.
It uses a small game-style event so you can verify the path from a producer request to durable raw telemetry storage.

Scope note:
- this quickstart includes both supported raw-storage backends for local validation
- S3-compatible storage such as SeaweedFS is the preferred OSS raw-storage path
- if you want the optional BigQuery external-table path afterward, use GCS

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
cp backend/deploy/.env.example backend/deploy/.env
```

For the default GCS mode, set at least these values:

```text
COLLECTOR_API_KEYS_JSON={"dev-api-key":"com.example.mygame"}
IP_HASH_SALT=replace_me
RAW_STORAGE_BACKEND=gcs
GCS_RAW_BUCKET=your-bucket-name
GCS_RAW_PREFIX=raw
```

For the preferred OSS S3-compatible mode, set:

```text
COLLECTOR_API_KEYS_JSON={"dev-api-key":"com.example.mygame"}
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

Before starting the stack, update the host-side mount in `backend/deploy/docker-compose.yml` so it points to your real key file.

For S3-compatible mode, no Google key is needed.
Instead, make sure the configured endpoint and bucket are reachable and the `S3_*` values are correct.

## 3. Start the stack

For local validation from a source checkout, build the services with Compose:

```bash
docker compose -f backend/deploy/docker-compose.yml up --build
```

Expected services:

- `nats`
- `collector-api`
- `raw-writer`

For a deployment that should consume the public OSS images instead of rebuilding locally, set the Compose service images to pinned GHCR tags:

```text
ghcr.io/mirlan-irokez/rawbbit-collector-api:0.1.7
ghcr.io/mirlan-irokez/rawbbit-raw-writer:0.1.8
```

Then start without `--build`:

```bash
docker compose -f backend/deploy/docker-compose.yml up -d --no-build
```

The `latest` tags are available, but pinned versions are preferred for repeatable deployments.

Environment and secret handling is the same for both modes. API keys, object-storage credentials, salts, and service-account paths come from `backend/deploy/.env` or your secret manager, not from the images.

## 4. Send a test player event

```bash
curl -sS -X POST http://localhost:8080/v1/events:batch \
  -H 'Content-Type: application/json' \
  -H 'X-API-Key: dev-api-key' \
  -d '{"events":[{"event_id":"00000000-0000-0000-0000-000000000001","app_id":"com.example.mygame","environment":"dev","event_name":"level_completed","event_timestamp":"2026-03-20T18:42:15.123Z","user":{"user_pseudo_id":"player_anon_001","session_id":"session_001"},"event_params":{"level_id":3,"duration_sec":91,"platform":"webgl"}}]}'
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
raw/app_id=com.example.mygame/event_date=YYYY-MM-DD/hour=HH/
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
- `../clickhouse/README.md` for the main ClickHouse query/loading path
- `../mcp-server/README.md` for the Rawbbit MCP server and optional Metabase deploy path
- `../metabase/README.md` for standalone Metabase deployment
