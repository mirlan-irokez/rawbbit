# DataQueryEvent

DataQueryEvent is a self-hosted in-app event tracking, ingestion, and raw-storage pipeline for product, application, and game analytics.
It was created for teams that want to keep control of their event data, reduce vendor lock-in, and run analytics infrastructure without depending on heavyweight enterprise platforms. 
The system is designed to stay portable and maintainable for small teams operating their own stack.

It accepts batched events over HTTP, validates and enriches them in the collector, buffers them through [NATS JetStream](https://nats.io/), writes partitioned Parquet files to object storage, and includes a small [SQLMesh](https://sqlmesh.readthedocs.io/en/stable/) starter project for querying the raw layer through BigQuery external tables.

Current public runtime shape:

```text
Producer -> Collector API -> NATS JetStream -> Raw Writer -> Parquet in object storage
```

Current public query path:

```text
Producer -> Collector API -> NATS JetStream -> Raw Writer -> Parquet -> BigQuery external table -> SQLMesh base model
```

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
- `deploy/` — Docker Compose and environment scaffolding for local or simple self-hosted setups
- `sqlmesh_project/` — starter [SQLMesh](https://sqlmesh.readthedocs.io/en/stable/) project for reading the raw external-table layer

## Architecture

The system is built around a few explicit boundaries:

- the collector accepts and validates event batches
- [NATS JetStream](https://nats.io/) separates request handling from storage writes
- the raw writer lands durable Parquet files in object storage
- raw Parquet is the system-of-record boundary for downstream analytics work
- downstream modeling can evolve without changing the ingestion contract

For the deeper architecture note, see [`docs/architecture.md`](docs/architecture.md).

## Quickstart

The shortest path to a working local setup is:

1. copy `deploy/.env.example` to `deploy/.env`
2. set API keys, object-storage bucket, and credentials
3. start the stack with Docker Compose
4. send a test batch to `POST /v1/events:batch`
5. verify that Parquet files land in object storage

For the full walkthrough, see [`docs/quickstart.md`](docs/quickstart.md).

## Configuration

The canonical environment-variable reference is `deploy/.env.example`.

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
deploy/            Local and self-hosted runtime scaffolding
sqlmesh_project/   Starter downstream modeling project
docs/              OSS documentation
```

Component reference notes:

- [`backend/README.md`](backend/README.md)
- [`backend/collector-api/README.md`](backend/collector-api/README.md)
- [`backend/raw-writer/README.md`](backend/raw-writer/README.md)
- [`deploy/README.md`](deploy/README.md)

## Project status

Current maturity:

- ingestion path is implemented
- raw Parquet landing path is implemented
- BigQuery external-table querying is supported
- [SQLMesh](https://sqlmesh.readthedocs.io/en/stable/) is included as a starter downstream layer

The current release is intentionally narrow: it focuses on reliable ingestion, durable raw storage, and a simple first query path.

The included [SQLMesh](https://sqlmesh.readthedocs.io/en/stable/) model is intentionally small. It reads from the BigQuery external table over the raw Parquet layer and serves as an optional starter path for downstream shaping rather than a large modeling system.

## Documentation

- [`docs/architecture.md`](docs/architecture.md)
- [`docs/quickstart.md`](docs/quickstart.md)
- [`docs/configuration.md`](docs/configuration.md)

## License

This project is released under the Apache License 2.0. See [`LICENSE`](LICENSE).
