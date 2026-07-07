# Collector API

`collector-api` is the public ingestion edge for Rawbbit.

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

## Published image

Public GHCR image:

```text
ghcr.io/mirlan-irokez/rawbbit-collector-api:0.1.7
```

The service listens on `PORT`, defaulting to `8080`. It needs NATS connectivity and collector API-key configuration through environment variables.

Use the pinned version tag for deployments. The `latest` tag is available for convenience.

## Related docs

- `../raw-writer/README.md`
- `../../docs/architecture.md`
- `../../docs/configuration.md`
