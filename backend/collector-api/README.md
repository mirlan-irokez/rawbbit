# Collector API

`collector-api` is the public ingestion edge for DataQueryEvent.

## Responsibilities

- accept `POST /v1/events:batch`
- validate request payloads
- map API keys to `app_id`
- attach ingest-time metadata
- publish accepted events into NATS JetStream
- handle browser-facing ingress concerns such as CORS

## Boundary

The collector is a stateless HTTP service. Its contract ends when an accepted event has been handed off to the durable message layer.

That separation keeps request handling narrow and leaves durable storage and downstream modeling to later stages in the pipeline.

## Related docs

- `../raw-writer/README.md`
- `../../docs/architecture.md`
- `../../docs/configuration.md`
