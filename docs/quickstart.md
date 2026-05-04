# Quickstart

This quickstart walks through the supported local validation path for the public DataQueryEvent stack.

## Prerequisites

You need:

- Docker and Docker Compose
- a local clone of the repository
- a machine or VM that can run Docker Compose
- a GCS bucket for raw output
- a GCP service account that can write objects to that bucket

## 1. Prepare the environment file

Copy the example environment file:

```bash
cp deploy/.env.example deploy/.env
```

Set at least these values:

```text
COLLECTOR_API_KEYS_JSON={"dev-api-key":"com.example.app"}
IP_HASH_SALT=replace_me
GCS_RAW_BUCKET=your-bucket-name
GCS_RAW_PREFIX=raw
```

## 2. Prepare credentials for `raw-writer`

The current Compose setup expects a service-account key inside the container at:

```text
/var/secrets/gcp/sa.json
```

Before starting the stack, update the host-side mount in `deploy/docker-compose.yml` so it points to your real key file.

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
- `raw-writer` flushed a Parquet object to the configured bucket

Expected partition layout:

```text
raw/app_id=com.example.app/event_date=YYYY-MM-DD/hour=HH/
```

## 6. Troubleshooting checks

If the flow does not complete, check:

- invalid or missing `X-API-Key`
- missing `GCS_RAW_BUCKET`
- wrong mounted service-account path
- collector running but writer unable to authenticate to GCS
- NATS healthy but writer not consuming

## Next

- `architecture.md`
- `configuration.md`
