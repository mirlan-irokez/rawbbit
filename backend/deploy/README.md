# Deploy

The `backend/deploy/` folder contains the reference scaffolding for running the public Rawbbit stack in local or simple self-hosted environments.

## What is here

- Docker Compose definitions
- environment-file examples
- service wiring for NATS and backend components
- supporting runtime configuration for local validation

## What this folder is for

Use `backend/deploy/` when you want to:

- boot the stack locally
- understand how `collector-api`, `raw-writer`, and NATS are connected
- provide the environment variables and credentials required by the public runtime path

## What the current deployment covers

The reference deployment brings up the ingestion path:

```text
Producer -> Collector API -> NATS JetStream -> Raw Writer -> Parquet in object storage
```

It is intended as the operator path for the current open-source release.

## Public images

The ingestion services are published as public GHCR images:

- `collector-api` -> `ghcr.io/mirlan-irokez/rawbbit-collector-api:0.1.7`
- `raw-writer` -> `ghcr.io/mirlan-irokez/rawbbit-raw-writer:0.1.8`
- `nats` -> upstream `nats:2.10-alpine`

Use pinned image tags for deployments. The `latest` tags exist for convenience, but they make rollbacks and incident analysis needlessly vague.

`docker-compose.example.yml` is image-pull oriented by default and points at the pinned GHCR images.

The Compose files can still be used in two modes:

- source-build mode: keep `build:` entries and run with `--build`
- image-pull mode: point `image:` to the GHCR tags and run with `--no-build`

Runtime configuration still comes from `.env` or secret management. Do not bake API keys, object-storage credentials, salts, or service-account JSON files into images.

## Related docs

- `../../docs/quickstart.md`
- `../../docs/configuration.md`
- `../../docs/architecture.md`

## Related downstream deployables

The `backend/deploy/` folder is for the ingestion runtime only. It does not start ClickHouse, MCP, or Metabase.

For downstream analytics deployment guides, see:

- `../../clickhouse/README.md` for loading/querying Rawbbit data in ClickHouse
- `../../mcp-server/README.md` for the Rawbbit MCP server and optional Metabase deployment
- `../../metabase/README.md` for standalone Metabase
