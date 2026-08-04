# Rawbbit dbt project

This dbt Core project loads bounded raw Parquet windows into the existing
ClickHouse `analytics.events` table in the Rawbbit two-VM deployment.

`RAWBBIT_RAW_LOAD_MODE` controls which implementation owns raw ingestion:

- `dbt`: `rawbbit_events_load` reads bounded Parquet paths, incrementally
  updates `analytics.events`, and runs ingestion data tests;
- `legacy`: the host cron shell loader owns `analytics.events`, while scheduled
  dbt jobs log a skip because v1 has no downstream models.

The two paths share `/srv/rawbbit-two/dbt/pipeline.lock` and must not ingest
simultaneously.

## Model

```text
rawbbit_events_load (alias analytics.events)
```

The model uses dbt-clickhouse's `delete_insert` incremental strategy with
`(app_id, event_id)` as its stable key. It selects one winner for each key in
the current window, deletes matching target keys, and inserts the winners.
This makes overlapping hourly and daily windows safe to replay at the event-key
level.

The model rejects empty event IDs and rows whose event timestamp cannot be
parsed. It preserves the existing `analytics.events` physical schema and has
`full_refresh: false`; do not remove that protection because the table is also
the legacy loader target and an operational boundary.

The current project contains only this ingestion model. Staging, intermediate,
and mart models can be added when their grains, consumers, and refresh
requirements are defined.

## Tests

The ingestion selector runs the model and its data tests through `dbt build`.
The tests require non-null `event_id`, `app_id`, and `event_time`, plus global
uniqueness of `(app_id, event_id)` in `analytics.events`.

The tests inspect the complete target table, not only the current input window.
Audit data written by the legacy loader before switching ingestion ownership to
dbt.

## Scheduling and recovery

The persistent `dbt-runner` container runs Supercronic in the foreground. Its
UTC schedule is versioned with the project:

```text
12 * * * * hourly build
30 3 * * * daily reconciliation
```

The hourly job uses a short configurable lookback. The daily job reconciles a
longer range for late files. Supercronic does not replay missed ticks; these
overlapping, idempotent windows are the recovery path.

Scheduled jobs skip if another pipeline job holds the shared lock. Manual
backfills wait for the lock, require hour-aligned UTC timestamps, and process
the requested range in configurable chunks:

```bash
docker compose exec -T \
  -e RAWBBIT_DBT_MIRROR_PID1=1 \
  dbt-runner \
  /app/bin/dbt-job backfill \
  2026-07-01T00:00:00Z \
  2026-07-05T00:00:00Z
```

`RAWBBIT_DBT_MIRROR_PID1=1` mirrors the manual job output into the persistent
container's Docker log stream so it remains visible through Dozzle.

## Credentials and permissions

The dbt container receives only the dedicated ClickHouse dbt credentials. S3
credentials remain inside ClickHouse's `rawbbit_raw_s3` named collection and do
not appear in compiled SQL or dbt artifacts.

The named-collection values are non-overridable. The dbt user can use only that
collection and has the narrow ClickHouse table privileges required by the
incremental strategy and tests. Do not use the ClickHouse admin, MCP, Metabase,
or legacy loader account for dbt.

## Validation

From `quickstart/vm_rawbbit_two`, with a populated private `.env`:

```bash
docker compose config
docker compose pull dbt-runner
docker compose run --rm --no-deps dbt-runner dbt parse \
  --project-dir /app --profiles-dir /app
```

Integration builds require a running ClickHouse instance and a reachable S3
endpoint. The [VM-two quickstart](../quickstart/vm_rawbbit_two/README.md)
documents deployment, existing-data audits, cutover, backfills, logs, and
rollback.
