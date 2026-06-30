# Rawbbit

Rawbbit is a self-hosted in-app event tracking and analytics pipeline for product, application, and game analytics.
It was created for teams that want to keep control of their event data, reduce vendor lock-in, and run analytics infrastructure without depending on heavyweight enterprise platforms. 
The system is designed to stay portable and maintainable for small teams operating their own stack.

It accepts batched events over HTTP, validates and enriches them in the collector, buffers them through [NATS JetStream](https://nats.io/), writes partitioned Parquet files to S3-compatible object storage such as [SeaweedFS](https://github.com/seaweedfs/seaweedfs), loads the raw layer into ClickHouse, and exposes the ClickHouse-backed data through MCP, Metabase, agents, and direct SQL access.

![Rawbbit](./assets/Rawbbit.png)

Main Rawbbit path:

```text
Producer -> Collector API -> NATS JetStream -> Raw Writer
  -> S3-compatible object storage / SeaweedFS Parquet
  -> ClickHouse loader cron job
  -> ClickHouse
  -> MCP / Metabase / agents / users
```

Optional BigQuery path:

```text
Producer -> Collector API -> NATS JetStream -> Raw Writer
  -> object storage Parquet
  -> BigQuery external table
  -> SQLMesh base model
```

Storage note:
- SeaweedFS/S3-compatible storage is the preferred OSS raw-storage path
- GCS remains supported
- the BigQuery external-table path remains a supported optional downstream path, not the main architecture

## Table of contents

- [What is included](#what-is-included)
- [Architecture](#architecture)
- [Quickstart](#quickstart)
- [Configuration](#configuration)
- [Repository layout](#repository-layout)
- [Project status](#project-status)
- [Documentation](#documentation)
- [License](#license)

## What is included

This repository contains the public ingestion-to-raw-storage path:

- `backend/collector-api` — HTTP ingestion service
- `backend/raw-writer` — JetStream consumer that writes partitioned Parquet files
- `backend/deploy/` — Docker Compose and environment scaffolding for local or simple self-hosted setups
- `clickhouse/` — main analytical/query path for loading and querying raw Parquet in ClickHouse
- `clickhouse-mcp/` — ClickHouse-backed MCP server and combined MCP + Metabase deployment guide
- AI agents and MCP clients such as OpenCode or OpenClaw can connect to the ClickHouse MCP endpoint for read-only analytics exploration
- `metabase/` — optional standalone Metabase deployment guide
- `sqlmesh_project/` — optional [SQLMesh](https://sqlmesh.readthedocs.io/en/stable/) project for the BigQuery external-table path

## Architecture

The system is built around a few explicit boundaries:

- the collector accepts and validates event batches
- [NATS JetStream](https://nats.io/) separates request handling from storage writes
- the raw writer lands durable Parquet files in object storage
- raw Parquet is the system-of-record boundary for downstream analytics work
- ClickHouse is the main analytical database and serving path
- MCP, Metabase, agents, and direct SQL are consumption surfaces on top of ClickHouse
- downstream modeling can evolve without changing the ingestion contract

For the deeper architecture note, see [`docs/architecture.md`](docs/architecture.md).

## Quickstart

The shortest path to a working local setup is:

1. copy `backend/deploy/.env.example` to `backend/deploy/.env`
2. set API keys, object-storage bucket, and credentials
3. start the stack with Docker Compose
4. send a test batch to `POST /v1/events:batch`
5. verify that Parquet files land in object storage

For the full walkthrough, see [`docs/quickstart.md`](docs/quickstart.md).

## Configuration

The canonical environment-variable reference is `backend/deploy/.env.example`.

Important configuration groups:

- NATS and stream settings
- collector API limits, API keys, CORS settings, and optional GeoIP-related attribution requirements
- raw-writer batching and ACK behavior
- object-storage bucket, prefix, and credentials

For the grouped configuration guide, see [`docs/configuration.md`](docs/configuration.md).

## Repository layout

```text
backend/
  collector-api/   HTTP ingestion service
  raw-writer/      Parquet landing worker
  deploy/          Local and self-hosted runtime scaffolding
sqlmesh_project/   Optional BigQuery SQLMesh starter model
clickhouse/        Main ClickHouse query/loading path
clickhouse-mcp/    ClickHouse MCP and optional Metabase deploy path
metabase/          Metabase OSS ver. deploy instructions
docs/              OSS documentation
```

Component reference notes:

- [`backend/README.md`](backend/README.md)
- [`backend/collector-api/README.md`](backend/collector-api/README.md)
- [`backend/raw-writer/README.md`](backend/raw-writer/README.md)
- [`backend/deploy` runtime notes](backend/deploy/README.md)
- [`clickhouse/README.md`](clickhouse/README.md)
- [`clickhouse-mcp/README.md`](clickhouse-mcp/README.md)
- [`metabase/README.md`](metabase/README.md)

## Project status

Current maturity:

- ingestion path is implemented
- raw Parquet landing path is implemented
- raw storage backend selection is implemented for both GCS and S3-compatible targets
- SeaweedFS/S3-compatible storage is the preferred OSS raw-storage path
- ClickHouse loading from raw Parquet is the main analytical/query path
- ClickHouse MCP can expose a read-only analytical tool surface over a configured Rawbbit ClickHouse events table
- AI agents and MCP clients can use that MCP surface without direct access to the ingestion runtime
- Metabase can be deployed separately or together with the ClickHouse MCP package
- BigQuery external-table querying is supported as an optional path
- [SQLMesh](https://sqlmesh.readthedocs.io/en/stable/) is included as an optional starter layer for the BigQuery path

The current release is intentionally narrow: it focuses on reliable ingestion, durable raw storage, ClickHouse-backed analytics, and agent/BI access on top of that serving layer.

The included [SQLMesh](https://sqlmesh.readthedocs.io/en/stable/) model is intentionally small. It reads from the BigQuery external table over the raw Parquet layer and serves as an optional BigQuery starter path rather than the central Rawbbit modeling layer.

## Documentation

- [`docs/architecture.md`](docs/architecture.md)
- [`docs/quickstart.md`](docs/quickstart.md)
- [`docs/configuration.md`](docs/configuration.md)
- [`clickhouse/README.md`](clickhouse/README.md)
- [`clickhouse-mcp/README.md`](clickhouse-mcp/README.md)
- [`metabase/README.md`](metabase/README.md)

## License

This project is released under the Apache License 2.0. See [`LICENSE`](LICENSE).

---

## Inspired by

- [awesome-data-engineering](https://github.com/igorbarinov/awesome-data-engineering) — for the broader data engineering ecosystem
