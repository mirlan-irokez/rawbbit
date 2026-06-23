# Architecture

## Overview

Rawbbit is a self-hosted ingestion and raw-storage pipeline for analytics events.

Current runtime boundary:

```text
Producer -> Collector API -> NATS JetStream -> Raw Writer -> Parquet in object storage
```

Current downstream query path included in the repository:

```text
Producer -> Collector API -> NATS JetStream -> Raw Writer -> Parquet -> BigQuery external table -> SQLMesh base model
```

ClickHouse serving path:

```text
Producer -> Collector API -> NATS JetStream -> Raw Writer -> Parquet -> ClickHouse
```

ClickHouse MCP and Metabase path:

```text
Producer -> Collector API -> NATS JetStream -> Raw Writer -> Parquet -> ClickHouse -> MCP / Metabase
```

AI agent access path:

```text
Raw Parquet -> ClickHouse -> ClickHouse MCP -> AI agents / MCP clients
```

## Components

### Collector API

The collector is the request-facing ingress service.

It is responsible for:

- accepting batched events over HTTP
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

- preserves accepted events in a portable format
- separates ingestion concerns from downstream query and modeling concerns
- makes it possible to change downstream tooling without changing the ingestion contract

### SQLMesh starter project

The repository includes a small [SQLMesh](https://sqlmesh.readthedocs.io/en/stable/) project that reads from the raw external-table layer.

It exists as a starter downstream path, not as the center of the runtime architecture.

### ClickHouse serving layer

ClickHouse can be added as a downstream query layer over raw Parquet.

In this shape, ClickHouse is not the ingestion source of truth. It is a serving analytical database that can query raw files directly or load them into local `MergeTree` tables for faster analytics.

See [`../clickhouse/README.md`](../clickhouse/README.md).

### ClickHouse MCP and Metabase layer

The ClickHouse MCP server can be deployed after ClickHouse is populated with Rawbbit events. It exposes read-only tools for event discovery, JSON-key discovery, sampling, guarded SQL, DAU, and funnel checks.

Metabase can be deployed beside the MCP server as a BI interface over the same ClickHouse data.

MCP clients can include AI coding or operations agents such as OpenCode, OpenClaw, or any other client that supports remote HTTP MCP.

This layer is not part of ingestion. It is a downstream access layer:

```text
Raw Parquet -> ClickHouse -> MCP clients / AI agents / Metabase
```

See [`../clickhouse-mcp/README.md`](../clickhouse-mcp/README.md).

## Design choices

### Raw-first storage boundary

Raw Parquet is the durable truth boundary. That keeps the ingestion system simple and avoids coupling the request path to any single warehouse-specific serving model.

### Queue-separated ingestion

The collector and raw writer are intentionally separated by a durable message layer. This lets the ingress edge stay narrow while the storage side handles batching and retries independently.

### Portable downstream path

The current repository includes a BigQuery external-table path, a small SQLMesh starter model, an optional ClickHouse serving path, and a ClickHouse MCP / Metabase access layer, but the raw storage boundary remains the stable handoff point.

## Delivery semantics

The pipeline should be understood as at-least-once.

That means:

- duplicate events are possible
- the collector reduces duplicate inserts within the JetStream dedupe window using `(app_id, event_id)`
- downstream consumers should still treat `event_id` as the stable event identity key
