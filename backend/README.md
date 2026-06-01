# Backend

The `backend/` folder contains the executable ingestion-side services in Rawbbit.

## Components

- `collector-api` — request-facing HTTP ingestion service
- `raw-writer` — background worker that consumes queued events and writes partitioned Parquet files

Together these services implement the public runtime boundary:

```text
Producer -> Collector API -> NATS JetStream -> Raw Writer -> Parquet
```

## Role in the system

The backend layer is responsible for:

- accepting event batches
- validating and enriching requests
- publishing accepted events into JetStream
- consuming queued events and landing them durably in object storage

The backend does not include a full reporting or dashboard layer. Its responsibility ends at reliable ingestion and durable raw storage.

## Related docs

- `collector-api/README.md`
- `raw-writer/README.md`
- `../docs/architecture.md`
- `../docs/configuration.md`
