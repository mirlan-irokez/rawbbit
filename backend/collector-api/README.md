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

Currently published public GHCR image (owner-confirmed 2026-10-22):

```text
ghcr.io/mirlan-irokez/rawbbit-collector-api:0.1.9
```

Platform: `linux/amd64`; digest:
`sha256:bbb96b074a42640c8472aaf601c8187f068590b60abc2afe699d9f77f0758941`.

The service listens on `PORT`, defaulting to `8080`. It needs NATS connectivity and collector API-key configuration through environment variables.

Use the pinned version tag for deployments. The `latest` tag is available for convenience.

## Release notes

### 0.1.9 — published 2026-10-22

- Browser JavaScript may read only the `Retry-After` response header through the
  existing CORS policy; allowed origins and other exposed headers are unchanged.
- Body-supplied `received_at`, `ingest.request_id`, `ingest.user_agent`,
  `ingest.ip_hash`, and `ingest.ip` are always replaced with server-derived
  values. `ingest.ip` is omitted when raw-IP storage is disabled or no valid
  request IP is available. Top-level `received_at` is always populated.
- The VM quickstart pins the published 0.1.9 image.

## Related docs

- `../raw-writer/README.md`
- `../../docs/architecture.md`
- `../../docs/configuration.md`
