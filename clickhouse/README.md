# ClickHouse

This guide shows how to use ClickHouse as an optional downstream query layer for Rawbbit raw Parquet data.

Rawbbit still treats object storage as the durable raw boundary:

```text
Producer -> Collector API -> NATS JetStream -> Raw Writer -> Parquet in object storage
```

ClickHouse is added after that boundary:

```text
Producer -> Collector API -> NATS JetStream -> Raw Writer -> Parquet -> ClickHouse
```

This is intentionally not a full VM hardening guide. Use your normal server, Docker, backup, TLS, and secret-management practices for production deployments.

## When to Use ClickHouse

Use ClickHouse when you want:

- fast analytical queries over event data
- local serving tables derived from Rawbbit raw Parquet
- a warehouse/query layer that you control
- dashboards or application-facing analytics without scanning object storage on every query

Keep using the raw Parquet objects as the rebuildable source of truth. If a ClickHouse table is lost or redesigned, reload it from raw storage.

## Prerequisites

You need:

- a running Rawbbit stack that writes Parquet to GCS or S3-compatible storage
- a ClickHouse server
- network access from ClickHouse to the object-storage endpoint
- read credentials for the raw bucket or prefix

The public Rawbbit storage layout is:

```text
<RAW_PREFIX>/app_id=<app_id>/event_date=YYYY-MM-DD/hour=HH/
```

For S3-compatible storage, a typical ClickHouse glob looks like:

```text
https://s3.example.com/your-bucket/raw/*/event_date=2026-06-09/hour=13/*.parquet
```

For GCS, use an S3-compatible access path if available, or adapt the examples to ClickHouse's object-storage functions supported by your deployment.

## Minimal Docker Compose Example

For a small self-hosted deployment, keep ClickHouse ports bound to localhost and connect with an SSH tunnel or private network:

```yaml
services:
  clickhouse:
    image: clickhouse/clickhouse-server:24.8
    container_name: clickhouse
    restart: unless-stopped
    env_file:
      - .env
    ulimits:
      nofile:
        soft: 262144
        hard: 262144
    ports:
      - "127.0.0.1:8123:8123"
      - "127.0.0.1:9000:9000"
    volumes:
      - ./data:/var/lib/clickhouse
      - ./logs:/var/log/clickhouse-server
```

Example `.env`:

```env
CLICKHOUSE_USER=admin
CLICKHOUSE_PASSWORD=replace_with_a_strong_password
CLICKHOUSE_DB=default
```

Start and test:

```bash
docker compose pull
docker compose up -d
curl "http://127.0.0.1:8123/ping"
```

Do not publish ports `8123` or `9000` directly to the public internet. Put ClickHouse behind private networking, an SSH tunnel, or a carefully controlled HTTPS proxy.

## Small VM Settings

ClickHouse can run on small VMs, but it should be given explicit memory and thread limits. The values below are practical starting points for a single-node Rawbbit analytics deployment, not universal ClickHouse defaults.

Put server-level settings in a file such as:

```text
config.d/low-memory.xml
```

For a small 2 CPU / 4 GB VM:

```xml
<clickhouse>
    <max_server_memory_usage>2500000000</max_server_memory_usage>
    <max_server_memory_usage_to_ram_ratio>0.70</max_server_memory_usage_to_ram_ratio>
    <mark_cache_size>268435456</mark_cache_size>
    <uncompressed_cache_size>134217728</uncompressed_cache_size>
    <listen_host>0.0.0.0</listen_host>
</clickhouse>
```

For a 4 CPU / 8 GB VM:

```xml
<clickhouse>
    <max_server_memory_usage>5500000000</max_server_memory_usage>
    <max_server_memory_usage_to_ram_ratio>0.70</max_server_memory_usage_to_ram_ratio>
    <mark_cache_size>536870912</mark_cache_size>
    <uncompressed_cache_size>268435456</uncompressed_cache_size>
    <listen_host>0.0.0.0</listen_host>
</clickhouse>
```

For an 8 CPU / 16 GB VM:

```xml
<clickhouse>
    <max_server_memory_usage>11000000000</max_server_memory_usage>
    <max_server_memory_usage_to_ram_ratio>0.70</max_server_memory_usage_to_ram_ratio>
    <mark_cache_size>1073741824</mark_cache_size>
    <uncompressed_cache_size>536870912</uncompressed_cache_size>
    <listen_host>0.0.0.0</listen_host>
</clickhouse>
```

Put user/profile settings in a file such as:

```text
users.d/low-memory-users.xml
```

For a small 2 CPU / 4 GB VM:

```xml
<clickhouse>
    <profiles>
        <default>
            <max_memory_usage>1500000000</max_memory_usage>
            <max_threads>2</max_threads>
            <max_insert_threads>1</max_insert_threads>
            <max_final_threads>1</max_final_threads>
            <max_execution_time>300</max_execution_time>
        </default>
    </profiles>
</clickhouse>
```

For a 4 CPU / 8 GB VM:

```xml
<clickhouse>
    <profiles>
        <default>
            <max_memory_usage>3500000000</max_memory_usage>
            <max_threads>4</max_threads>
            <max_insert_threads>2</max_insert_threads>
            <max_final_threads>2</max_final_threads>
            <max_execution_time>300</max_execution_time>
        </default>
    </profiles>
</clickhouse>
```

For an 8 CPU / 16 GB VM:

```xml
<clickhouse>
    <profiles>
        <default>
            <max_memory_usage>7000000000</max_memory_usage>
            <max_threads>8</max_threads>
            <max_insert_threads>4</max_insert_threads>
            <max_final_threads>4</max_final_threads>
            <max_execution_time>300</max_execution_time>
        </default>
    </profiles>
</clickhouse>
```

After changing these files, restart ClickHouse:

```bash
docker compose restart clickhouse
docker logs --tail=100 clickhouse
```

These limits matter most during `INSERT INTO ... SELECT FROM s3(...)` loads. Larger insert blocks and more parallelism can improve throughput, but they also consume more memory and create merge pressure. Raise limits with the VM, not with optimism.

## Create a Raw Events Table

Create a database and a first serving table:

```sql
CREATE DATABASE IF NOT EXISTS analytics;

CREATE TABLE IF NOT EXISTS analytics.events
(
    event_id String,
    app_id LowCardinality(String),
    environment LowCardinality(String),
    event_name LowCardinality(String),
    event_time DateTime64(3, 'UTC'),
    event_date Date,
    received_time Nullable(DateTime64(3, 'UTC')),
    user_id Nullable(String),
    user_pseudo_id String,
    session_id Nullable(String),
    platform Nullable(String),
    app_version Nullable(String),
    os_version Nullable(String),
    device_model Nullable(String),
    locale Nullable(String),
    timezone Nullable(String),
    event_params_json String,
    user_properties_json String,
    traffic_source_json String,
    geo_json String,
    consent_json String,
    ingest_request_id String,
    ingest_user_agent Nullable(String),
    ingest_ip_hash Nullable(String),
    nats_stream String,
    nats_sequence UInt64
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(event_date)
ORDER BY (app_id, event_name, event_time, event_id);
```

This is a practical starter schema, not a universal final model. Change the `ORDER BY` key to match your common query predicates. In ClickHouse, the `MergeTree` engine and ordering key are physical design choices, not decoration.

## Load Raw Parquet from Object Storage

You can query raw files directly:

```sql
SELECT count()
FROM s3(
    'https://s3.example.com/your-bucket/raw/*/event_date=2026-06-09/hour=13/*.parquet',
    'ACCESS_KEY',
    'SECRET_KEY',
    'Parquet'
);
```

For regular analytics, load raw files into a local `MergeTree` table:

```sql
INSERT INTO analytics.events
SELECT
    event_id,
    ifNull(app_id, '') AS app_id,
    ifNull(environment, '') AS environment,
    ifNull(event_name, '') AS event_name,
    event_time,
    toDate(event_time) AS event_date,
    received_time,
    user_id,
    ifNull(user_pseudo_id, '') AS user_pseudo_id,
    session_id,
    platform,
    app_version,
    os_version,
    device_model,
    locale,
    timezone,
    ifNull(event_params_json, '') AS event_params_json,
    ifNull(user_properties_json, '') AS user_properties_json,
    ifNull(traffic_source_json, '') AS traffic_source_json,
    ifNull(geo_json, '') AS geo_json,
    ifNull(consent_json, '') AS consent_json,
    ifNull(ingest_request_id, '') AS ingest_request_id,
    ingest_user_agent,
    ingest_ip_hash,
    ifNull(nats_stream, '') AS nats_stream,
    toUInt64OrZero(toString(nats_sequence)) AS nats_sequence
FROM
(
    SELECT DISTINCT
        *,
        parseDateTime64BestEffortOrNull(event_timestamp, 3, 'UTC') AS event_time,
        parseDateTime64BestEffortOrNull(received_at, 3, 'UTC') AS received_time
    FROM s3(
        'https://s3.example.com/your-bucket/raw/*/event_date=2026-06-09/hour=13/*.parquet',
        'ACCESS_KEY',
        'SECRET_KEY',
        'Parquet'
    )
)
WHERE event_time IS NOT NULL;
```

The `SELECT DISTINCT` is a defensive first-pass dedupe for replayed files or duplicate raw rows. For stricter idempotency, build a dedicated staging-and-merge flow around `event_id`.

## Hourly Loader Pattern

A simple first production shape is:

```text
Rawbbit raw Parquet -> hourly loader -> analytics.events
```

Example loader environment:

```env
CLICKHOUSE_USER=admin
CLICKHOUSE_PASSWORD=replace_with_a_strong_password
CLICKHOUSE_RAW_S3_BASE_URL=https://s3.example.com/your-bucket/raw
CLICKHOUSE_RAW_S3_ACCESS_KEY=your-access-key
CLICKHOUSE_RAW_S3_SECRET_KEY=your-secret-key
```

The loader computes the previous hour and expands the final URL as:

```text
${CLICKHOUSE_RAW_S3_BASE_URL}/*/event_date=YYYY-MM-DD/hour=HH/*.parquet
```

Example Linux cron entry:

```cron
5 * * * * /opt/clickhouse/load_events_hourly.sh >> /opt/clickhouse/logs/load_events_hourly.log 2>&1
```

This means: at minute `05` every hour, load the previous hour's raw files.

## Direct S3 Reads vs Local Tables

ClickHouse can use object storage in three different ways:

- query Parquet files directly with the `s3()` table function
- load Parquet files into local `MergeTree` tables
- use S3-compatible storage as a ClickHouse storage disk through a storage policy

For Rawbbit, start with the second option: load object-storage Parquet into local `MergeTree` tables. It keeps the raw boundary clean, gives ClickHouse proper table indexes and merges, and remains easy to rebuild.

Using S3-compatible storage as the ClickHouse table storage disk is a separate operational decision. Treat it as storage architecture, not as the first integration step.

## Operations Notes

- keep ClickHouse data outside the container with bind mounts or managed volumes
- pin the ClickHouse image version and upgrade intentionally
- back up or snapshot before upgrades
- keep object-storage credentials out of committed files
- use read-only object-storage credentials for loaders whenever possible
- do not expose the native or HTTP ports publicly without a deliberate access-control layer

## Related Docs

- [`../docs/architecture.md`](../docs/architecture.md)
- [`../docs/configuration.md`](../docs/configuration.md)
- [`../docs/quickstart.md`](../docs/quickstart.md)
- [ClickHouse S3 integration](https://clickhouse.com/docs/integrations/data-ingestion/s3)
- [ClickHouse MergeTree schema design](https://clickhouse.com/docs/data-modeling/schema-design)
