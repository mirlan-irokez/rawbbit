# Architecture

## Overview

Rawbbit is a self-hosted telemetry pipeline for mobile and web game events.
It is designed for teams that want to collect player sessions, gameplay events, funnels, retention signals, economy events, monetization events, and backend service events without handing the raw telemetry layer to a proprietary analytics vendor.

The architecture stays intentionally small: game clients and services send events, Rawbbit lands the durable raw layer, ClickHouse serves analytics, and optional tools such as MCP and Metabase sit on top.

Main Rawbbit path:

```text
Producer -> Collector API -> NATS JetStream -> Raw Writer
  -> S3-compatible object storage / SeaweedFS Parquet
  -> scheduled dbt ingestion
  -> ClickHouse analytics.events
  -> MCP / Metabase / agents / users
```

Optional BigQuery path:

```text
Producer -> Collector API -> NATS JetStream -> Raw Writer
  -> object storage Parquet
  -> BigQuery external table
  -> SQLMesh base model
```

## Components

### Collector API

The collector is the request-facing ingress service for game clients, web builds, mobile apps, and backend game services.

It is responsible for:

- accepting batched player and gameplay events over HTTP
- validating payloads
- mapping API keys to `app_id`
- attaching ingest metadata such as request ID and receive time
- publishing one message per event into JetStream

### NATS JetStream

[NATS JetStream](https://nats.io/) provides the durable buffer between request handling and raw storage.

It is responsible for:

- buffering accepted events
- decoupling API response time from storage-write timing
- supporting durable consumer semantics between the collector and raw writer

### Raw Writer

The raw writer consumes queued events and lands them as partitioned Parquet files.

It is responsible for:

- consuming queued events
- buffering micro-batches
- assigning partitions
- writing Snappy Parquet
- uploading objects to storage
- acknowledging messages only after durable landing

### Raw object-storage layer

The raw Parquet layer is the system-of-record boundary for the current release.

This layer:

- preserves accepted player and game-service events in a portable format
- separates ingestion concerns from downstream query and modeling concerns
- makes it possible to change downstream tooling without changing the ingestion contract

### dbt ingestion and ClickHouse serving layer

ClickHouse is the main analytical database and serving layer over raw Parquet.

ClickHouse is not the ingestion source of truth. The durable boundary remains
raw Parquet. A persistent dbt Core runner loads bounded Parquet windows into
the local `analytics.events` `MergeTree` table and runs ingestion data tests.
The current dbt project contains one ingestion model; staging and mart models
can be added later when their grains and consumers are defined.

The runner processes a short overlapping window hourly and reconciles a longer
window daily for late files. Manual backfills use explicit, hour-aligned UTC
ranges and smaller chunks. All dbt jobs and the legacy shell loader share one
lock, while `RAWBBIT_RAW_LOAD_MODE` gives exactly one path ownership of raw
ingestion.

Within each selected window, dbt keeps one winner per `(app_id, event_id)` and
uses the ClickHouse adapter's `delete_insert` incremental strategy to replace
matching keys. This makes overlapping schedules and replayed windows safe at
the event-key level. The legacy hourly shell loader remains available as a
mutually exclusive rollback path.

See [`../clickhouse/README.md`](../clickhouse/README.md).
See [`../dbt_project/README.md`](../dbt_project/README.md).

### Rawbbit MCP server and Metabase layer

The Rawbbit MCP server can be deployed after ClickHouse is populated with Rawbbit events. It exposes read-only tools for event discovery, JSON-key discovery, sampling, guarded SQL, DAU, and funnel checks.

Metabase can be deployed beside the MCP server as a BI interface over the same ClickHouse data for player and gameplay analysis.

MCP clients can include AI coding or operations agents such as OpenCode, OpenClaw, or any other client that supports remote HTTP MCP.

This layer is not part of ingestion. It is a downstream access layer:

```text
SeaweedFS/S3 Parquet -> dbt ingestion -> ClickHouse -> MCP clients / AI agents / Metabase
```

See [`../mcp-server/README.md`](../mcp-server/README.md).

### Optional BigQuery and SQLMesh layer

The repository also includes a BigQuery external-table path and a small [SQLMesh](https://sqlmesh.readthedocs.io/en/stable/) project.

This path reads the same raw Parquet contract through BigQuery and can build the starter `staging.base_dataquery__events` model. It is useful when a deployment wants BigQuery, but it is not the center of the OSS Rawbbit architecture.

## Design choices

### Raw-first storage boundary

Raw Parquet is the durable truth boundary. That keeps the ingestion system simple and avoids coupling game telemetry collection to any single warehouse-specific serving model.

### Queue-separated ingestion

The collector and raw writer are intentionally separated by a durable message layer. This lets the player-facing ingress edge stay narrow while the storage side handles batching and retries independently.

### ClickHouse-first serving with optional warehouses

The current repository uses ClickHouse as the main open analytical database path, with MCP, Metabase, agents, and SQL users consuming the ClickHouse-backed table. BigQuery external tables and SQLMesh remain optional downstream integrations over the same raw Parquet contract.

## Delivery semantics

The pipeline should be understood as at-least-once.

That means:

- duplicate events are possible
- the collector reduces duplicate inserts within the JetStream dedupe window using `(app_id, event_id)`
- the ClickHouse dbt ingestion model uses `(app_id, event_id)` as its replacement key
- downstream consumers should treat the pair, rather than `event_id` alone, as the stable event identity
