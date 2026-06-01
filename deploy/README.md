# Deploy

The `deploy/` folder contains the reference scaffolding for running the public Rawbbit stack in local or simple self-hosted environments.

## What is here

- Docker Compose definitions
- environment-file examples
- service wiring for NATS and backend components
- supporting runtime configuration for local validation

## What this folder is for

Use `deploy/` when you want to:

- boot the stack locally
- understand how `collector-api`, `raw-writer`, and NATS are connected
- provide the environment variables and credentials required by the public runtime path

## What the current deployment covers

The reference deployment brings up the ingestion path:

```text
Producer -> Collector API -> NATS JetStream -> Raw Writer -> Parquet in object storage
```

It is intended as the operator path for the current open-source release.

## Related docs

- `../docs/quickstart.md`
- `../docs/configuration.md`
- `../docs/architecture.md`
