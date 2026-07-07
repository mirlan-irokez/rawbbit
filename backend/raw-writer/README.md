# Raw Writer

`raw-writer` is the worker that turns accepted events into durable raw data.

## Responsibilities

- consume events from NATS JetStream
- buffer them into micro-batches
- assign output partitions
- write Snappy Parquet files
- upload them to object storage
- acknowledge messages only after successful durable landing

## Boundary

This component is the storage boundary of the ingestion pipeline.

It preserves raw events in a portable format that downstream query engines and modeling layers can read without changing the ingestion contract.

## Published image

Public GHCR image:

```text
ghcr.io/mirlan-irokez/rawbbit-raw-writer:0.1.8
```

The worker needs NATS connectivity and raw-storage configuration through environment variables. It can write to GCS or an S3-compatible object store such as SeaweedFS.

Use the pinned version tag for deployments. The `latest` tag is available for convenience.

## Related docs

- `../collector-api/README.md`
- `../../docs/architecture.md`
- `../../docs/configuration.md`
