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

## Release target

Target GHCR image tag:

```text
ghcr.io/mirlan-irokez/rawbbit-raw-writer:0.1.11
```

This is the target tag for the pending malformed-message isolation release; GHCR publication and public-repository promotion are pending. Confirm publication before deploying this tag.

If event transformation fails, the worker logs a warning with the error class and stream sequence, never the payload, and sends TERM for that message. TERM stops that message's consumer redelivery; it does not delete the stream. This adds no schema layer and does not change other ACK/NAK handling, buffering, flushing, or valid-event output.

The worker needs NATS connectivity and raw-storage configuration through environment variables. It can write to GCS or an S3-compatible object store such as SeaweedFS.

Deploy a pinned version only after confirming it has been published; `latest` may still refer to the previous release.

## Related docs

- `../collector-api/README.md`
- `../../docs/architecture.md`
- `../../docs/configuration.md`
