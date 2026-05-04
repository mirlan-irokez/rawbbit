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

## Related docs

- `../collector-api/README.md`
- `../../docs/architecture.md`
- `../../docs/configuration.md`
