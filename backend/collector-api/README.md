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

Currently published public GHCR image (owner-confirmed 2026-10-24):

```text
ghcr.io/mirlan-irokez/rawbbit-collector-api:0.1.10
```

Platform: `linux/amd64`; OCI index digest:
`sha256:710e4bcd5347b15d0f1cac819bc5d26320a39026012264c4ba76a0ca98095fce`.
The `linux/amd64` platform manifest is
`sha256:48014fa043cc8235251d97e6702e0418d92e5f014a19ff724a271a5117faac7a`;
the index also includes an attestation manifest.

The service listens on `PORT`, defaulting to `8080`. It needs NATS connectivity and collector API-key configuration through environment variables.

Use the pinned version tag for deployments. The 0.1.10 publication did not move
the `latest` tag.

## Release notes

### 0.1.9 — published 2026-10-22

- Browser JavaScript may read only the `Retry-After` response header through the
  existing CORS policy; allowed origins and other exposed headers are unchanged.
- Body-supplied `received_at`, `ingest.request_id`, `ingest.user_agent`,
  `ingest.ip_hash`, and `ingest.ip` are always replaced with server-derived
  values. `ingest.ip` is omitted when raw-IP storage is disabled or no valid
  request IP is available. Top-level `received_at` is always populated.
- The VM quickstart pins the published 0.1.9 image.

### 0.1.10 — published 2026-10-24

Validation failures remain HTTP 422 with the existing `{"detail":[...]}`
envelope and return only `type`, `loc`, and `msg`. Up to 20 errors are returned
in order. Only when there are more than 20, the response contains the first 19
errors followed by this fixed marker in slot 20:

```json
{"type":"errors_truncated","loc":[],"msg":"Additional validation errors omitted"}
```

This intentionally removes Pydantic's prior `input`, `ctx`, and `url` fields.
Malformed JSON that previously could fail response serialization as HTTP 500
now returns the sanitized 422 response. SDKs quarantine that response rather
than retrying it as a server error.

Stream startup checks for an existing compatible JetStream stream before adding
one. A compatible stream confirmed after initial `NotFoundError` and a successful
compatible add logs `stream_created` regardless of `did_create` (including when
that attribute is absent); other compatible confirmations log
`stream_exists_and_compatible`. This log describes an observed absent-to-present
transition, not exclusive creation by a particular worker.

The VM-one quickstart now pins published collector-api `0.1.10`.

## Related docs

- `../raw-writer/README.md`
- `../../docs/architecture.md`
- `../../docs/configuration.md`
