# Rawbbit dbt project

This dbt Core project loads bounded raw Parquet windows into the configured
ClickHouse `events` table in the Rawbbit two-VM deployment. The default target
is `CLICKHOUSE_DATABASE.events` (normally `analytics.events`); optional app-ID
routing can send selected apps to other existing `events` tables.

`RAWBBIT_RAW_LOAD_MODE` controls which implementation owns raw ingestion:

- `dbt`: `rawbbit_events_load` reads bounded Parquet paths, incrementally
  updates the configured default target, and runs ingestion data tests. With
  routing disabled or unconfigured, this retains the current all-app behavior;
- `legacy`: the host cron shell loader owns the default events table (normally
  `analytics.events`), while scheduled
  dbt jobs log a skip because v1 has no downstream models.

The two paths share `/srv/rawbbit-two/dbt/pipeline.lock` and must not ingest
simultaneously.

## Model

```text
rawbbit_events_load (alias events)
```

The model uses dbt-clickhouse's `delete_insert` incremental strategy with
`(app_id, event_id)` as its stable key. It selects one winner for each key in
the current window, deletes matching target keys, and inserts the winners.
This makes overlapping hourly and daily windows safe to replay at the event-key
level.

The model rejects empty event IDs and rows whose event timestamp cannot be
parsed. It preserves the configured default table's physical schema and has
`full_refresh: false`; do not remove that protection because the table is also
the legacy loader target and an operational boundary. Optional routing reuses
this one model and adds no per-dataset model or cron.

The current project contains only this ingestion model. Staging, intermediate,
and mart models can be added when their grains, consumers, and refresh
requirements are defined.

## Tests

The ingestion selector runs the model and its data tests through `dbt build`.
The tests require non-null `event_id`, `app_id`, and `event_time`, plus global
uniqueness of `(app_id, event_id)` in the configured default events table.

The tests inspect the complete target table, not only the current input window.
Audit data written by the legacy loader before switching ingestion ownership to
dbt.

## Progress and diagnostics

When an attempt launches dbt, callbacks emit bounded, best-effort progress
lines on stderr for observed starts and finishes of the approved ingestion
model and its selected non-null tests:

- model `rawbbit_events_load`
- tests `not_null_event_id`, `not_null_app_id`, and `not_null_event_time`

For example:

```text
2026-10-05T12:00:00Z [dbt-progress job=<uuid> attempt=<uuid> kind=default db=analytics] START model rawbbit_events_load
2026-10-05T12:00:02Z [dbt-progress job=<uuid> attempt=<uuid> kind=default db=analytics] OK model rawbbit_events_load elapsed_seconds=2.400
```

The runner supplies the job, attempt, attempt kind (`default`, `primary`, or
`fallback`), and destination database. Progress statuses and labels are fixed
and allowlisted; they do not include dbt messages or arbitrary event metadata.
Observed model finishes map to `OK`, `ERROR`, `SKIP`, or `PARTIAL`; test
finishes map to `PASS`, `FAIL`, `WARN`, `ERROR`, or `SKIP`. Unknown events,
labels, and status values are omitted rather than forwarded.
Attempt-level lines also mark `START attempt`, the `VERIFY attempt
phase=server_settlement` check, and the terminal `END attempt` outcome.
An attempt that fails destination-readiness checks can therefore have attempt
lines without any model/test lifecycle lines.
The stderr stream is informational, not a job-failure signal. The existing
final JSON summary remains on stdout, and the job/attempt/request ledgers,
exit codes, routing, and server-settlement checks are unchanged. Relative order
between stdout and stderr is not guaranteed; use the final summary, exit code,
and persistent records for the authoritative outcome.

Progress is not a raw dbt debug or error log. Child stdout/stderr remain
suppressed, dbt file logging remains disabled, and JSON artifacts remain
disabled. Raw exceptions, SQL, credentials, paths, vars, backend responses, and
event/user data are not forwarded. A failed dbt child always gets the fixed
terminal category `execution_failed`, with the last observed phase and, when
available, last observed node (or `before_first_node`/`unknown`). This remains
generic regardless of where dbt stopped; phase and node identify observed
activity, not the cause. A pre-node failure does not establish a parse, startup,
authentication, grants, network, or server problem.

Node-level `FAIL test ...` lines add `category=test_failed`; model or test
`ERROR` lines add `category=node_error`. These describe the observed dbt node
status, not a database diagnosis; the runner does not emit `database_error`.
Exception-specific compilation, startup, and connection categories are
disabled because a safe end-to-end mapping has not been verified. Test failure
counts, backend codes, and request-level correlation are also disabled. Do not
expect complete raw dbt error detail.

Only observed node events are reported. With `--fail-fast`, selected tests may
never start, and interrupted nodes may not finish; the runner does not invent
skipped or terminal events. Logging is bounded and best-effort, so progress can
be dropped under pipe or sink failure, contention, or resource pressure. A
model/test success line is not proof that ClickHouse completed the write: the
runner still performs its existing server-settlement verification before
confirming an attempt.

No operator setting, dependency, schema, grant, or Compose change is required.
Production activation requires a runner image containing the change, separate
authorization, and the existing [VM-two safe drain/gate procedure](../quickstart/vm_rawbbit_two/README.md#safe-first-upgrade-and-route-changes);
do not recreate a live production runner outside that procedure.

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

The image's `DBT_UID` and `DBT_GID` build arguments must match the owner of
the host-mounted `/srv/rawbbit-two/dbt` directory. The Compose defaults are
`1000:1000`, matching the usual first non-root Linux user.

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
Manual backfill requires `RAWBBIT_RAW_LOAD_MODE=dbt`; in `legacy` mode,
scheduled jobs skip and manual dbt backfill is rejected.

## Optional app-ID routing

Routing is an opt-in dbt-runner feature, independent of MCP endpoint
enablement. VM-two Ansible sources it from the non-secret
`rawbbit_two_dbt_routing_enabled` and `rawbbit_two_dbt_routes` variables and
renders one version-1 route snapshot for the runner. A route's `dataset_id`
references `rawbbit_two_datasets[].id`; Ansible resolves its ClickHouse target
from that metadata even when the dataset's MCP `enabled` flag is false. The MCP
runtime registry and MCP credentials are not used by ingestion.

The runner reads:

```env
RAWBBIT_DBT_ROUTING_ENABLED=0
RAWBBIT_DBT_ROUTES_FILE=/app/runtime/dbt-routes.json
```

Enabled routing also requires `RAWBBIT_RAW_LOAD_MODE=dbt`; the legacy shell
loader is not app-routed.

The checked-in Compose and Ansible role defaults remain
`ghcr.io/mirlan-irokez/rawbbit-dbt-runner:0.1.1`. That image predates the drain
and routing contract, and the deployment helper rejects it as a
routing-capable candidate; this README does not change either default. A tested
local build is an alternative to selecting a tested, compatible released image:
VM-two Ansible can select `rawbbit-dbt-runner:local` and build from this checked-in
project before candidate validation and safe activation. A local build does not
require a GHCR upload, and source copying alone does not update the running
container. For remote image selections, use a tested compatible release rather
than an untested/unpublished registry tag or registry `latest`. See the [VM-two
routing and deployment guide](../quickstart/vm_rawbbit_two/README.md#dbt-app-id-routing)
for image selection and cutover instructions.

With routing disabled (`0`), or with no routing configuration, ingestion keeps
loading all apps into `CLICKHOUSE_DATABASE.events`. Set the switch to `1` only
with a valid version-1 snapshot, for example:

```json
{
  "version": 1,
  "routes": [
    {
      "app_id": "example_app",
      "dataset_id": "example_dataset",
      "database": "example_events",
      "table": "events"
    }
  ]
}
```

Enabled routing requires a readable, supported snapshot before the first write,
even when its route list is empty. Missing, unreadable, malformed, unsupported,
duplicate, unsafe, unresolved, conflicting, or default-colliding route
configuration fails closed with exit code `3` and no writes. A valid destination
that is missing, inaccessible, or incompatible is instead a route-load failure
and falls back for that same app and window to the configured default table.
The runner validates and freezes one route snapshot under the shared lock per
invocation; every window/chunk uses that same recorded revision. A later
configuration change applies only to a later invocation.

For each window, the runner uses the single ingestion model sequentially:

1. Load the default-table complement, excluding every configured routed
   `app_id`.
2. Attempt each route using both an exact raw Parquet app path and an exact SQL
   `app_id` include filter.
3. If a routed attempt fails, try only that app and window against the default
   table with the same include filter, then continue to the next route.

The default complement's exclusion filter is never used for fallback. A known
default, route, or fallback failure does not prevent later route attempts or
remaining bounded windows from running. If a ClickHouse query or mutation may
still be running and its outcome is unknown, the runner records a durable fence
and stops conflicting writes until an operator resolves it. The runner tags
attempts and checks ClickHouse process and mutation state; a client
exit or cancellation alone is not proof that ClickHouse stopped the work.
Do not clear an unknown-work fence as a retry shortcut; verify the tagged work
is terminal, reconcile any partial output, and have an operator authorize
recovery first.

The existing hourly and daily schedules are unchanged. There is no per-route
cron, automatic replay queue, automatic cross-database delete/move, or full
refresh. Fallback rows are visible in the main/default dataset. A later
successful route does not remove those copies. `(app_id, event_id)` is a
per-table replay key; it does not guarantee uniqueness across databases, so
partial writes or fallback can leave copies in both places.
Removing the last route returns that app to default-table loading on later
lookback/backfill windows, but does not move or delete existing routed or
fallback rows. Rolling back to a pre-routing image while routes are enabled
requires the same drain procedure and explicit approval of all-app default
loading; otherwise stop ingestion rather than silently changing destinations.

Every attempt and terminal outcome is persisted under `/app/runtime` on the
existing `/srv/rawbbit-two/dbt` volume, together with the route revision used by
the invocation. Interpret job exit codes as follows:

| Exit | Meaning |
| --- | --- |
| `0` | All planned work completed without fallback. |
| `2` | Work completed using one or more default-table fallbacks; monitor as degraded. |
| `1` | One or more app-windows were not confirmed loaded. |
| `3` | Configuration or invocation input was invalid; no writes were made. |
| `4` | Deferred/skipped work or an unconfirmed invocation; inspect the recorded status and fence. |

Runner state is on the persistent `/srv/rawbbit-two/dbt` volume mounted at
`/app/runtime`. Inspect these atomic, mode-`0600` JSON records before replay:

| Path under `/app/runtime` | Contents |
| --- | --- |
| `jobs/<job_id>.json` | Invocation status, route revision, windows, and per-attempt outcomes. |
| `attempts/<attempt_id>.json` | Intended and actual table, window, route revision, and write status. |
| `requests/<attempt_id>/<query_id>.json` | Dispatched/confirmed ClickHouse request IDs and transport status; preflight uses `requests/preflight-<job_id>/`. |
| `write-fence.json` | Durable operator-review fence when ClickHouse work cannot be proved terminal. |

Exit `4` is shared by deferrals and unresolved invocations. The persisted
statuses `gated`, `lock_skipped`, `lock_wait_expired`, and `mode_disabled` are
non-success skips and do not themselves create a fence. An unconfirmed
ClickHouse server outcome writes `write-fence.json`; no later job may write
until an operator verifies tagged work is terminal and reconciles possible
partial output. Use the job, attempt, request, and fence records together; exit
code `4` alone does not distinguish these cases or prove ingestion completed.

Runner timeout environment settings accept positive integer seconds, bounded as
follows:

| Variable | Default | Maximum | Applies to |
| --- | ---: | ---: | --- |
| `RAWBBIT_DBT_ATTEMPT_TIMEOUT_SECONDS` | `900` | `7200` | One dbt child attempt. |
| `RAWBBIT_DBT_JOB_TIMEOUT_SECONDS` | `21600` | `172800` | Total invocation budget. |
| `RAWBBIT_DBT_CONTROL_TIMEOUT_SECONDS` | `10` | `60` | One ClickHouse control HTTP request. |
| `RAWBBIT_DBT_SETTLE_TIMEOUT_SECONDS` | `30` | `300` | Proving tagged requests, loader queries, and mutations are terminal. |
| `RAWBBIT_DBT_LOCK_WAIT_SECONDS` | `300` | `7200` | Manual backfill lock wait; scheduled jobs skip immediately. |

These optional timeout variables are read by the runner but are not currently
exported by the checked-in Compose or Ansible service configuration, so those
deployments use the defaults. Adding overrides requires an explicit reviewed
service-environment wiring change; putting a value in `.env` alone does not
pass it through.

There is no automatic retry. Before bounded manual replay, inspect the
persistent outcome and compare its route revision with the active snapshot. If
the revision differs, choose and review the intended mapping rather than
silently replaying to another destination. Replay only the required UTC window
range, before its source Parquet expires. Back up and retain outcome records and
route revisions long enough for investigation and that manual replay.

## Credentials

The dbt container receives only ClickHouse dbt credentials. S3 credentials
remain inside the ClickHouse container and are exposed to SQL through the
`rawbbit_raw_s3` named collection. Consequently they do not appear in dbt
compiled SQL or dbt artifacts.

The ClickHouse configuration marks every S3 named-collection value as
non-overridable. The dbt user receives permission to use only the
`rawbbit_raw_s3` named collection, without named-collection administration or
secret-inspection privileges. The service-user initialization script creates a
secret-free `users.d` overlay that enables the configured ClickHouse admin to
delegate this privilege. Keep loader data privileges scoped to the configured
default and explicit route `events` tables. Before enabling a route, an operator
must verify the effective grants against the pinned dbt-clickhouse adapter's
`delete_insert` behavior on the deployed ClickHouse version and grant only the
verified operations. Do not grant MCP write access or broad `*.*` data
privileges. Routing does not create databases, tables, or grants, and the
first-boot user script is not a migration mechanism for persistent
installations. See
`quickstart/vm_rawbbit_two/clickhouse/initdb.d/02_service_users.sh` for the
current service-user baseline. Do not use the ClickHouse admin, MCP, Metabase,
or legacy loader account for dbt.

The safety control plane also requires explicit narrow `SELECT` grants to the
dedicated loader, even when routing is disabled. First-boot baseline grants do
not include these and do not migrate existing installations. In an authorized
ClickHouse admin session, review and apply only:

```sql
GRANT SELECT ON system.processes TO rawbbit_dbt;
GRANT SELECT ON system.mutations TO rawbbit_dbt;
GRANT SELECT ON system.query_log TO rawbbit_dbt;
GRANT SELECT ON system.tables TO rawbbit_dbt;
GRANT SELECT ON system.columns TO rawbbit_dbt;
```

The deployment runs `dbt-job verify-control` before releasing its gate; missing
observation access fails closed. Separately verify permission to cancel only the
loader's own tagged query on the deployed ClickHouse version. Do not grant
global query cancellation to bypass a fence.

## Local validation

From `quickstart/vm_rawbbit_two`, with a populated private `.env`:

```bash
docker compose build dbt-runner
docker compose config
docker compose run --rm --no-deps dbt-runner dbt parse \
  --project-dir /app --profiles-dir /app
```

This local build and parse are development checks, not production image
publication. The production Dockerfile build, full runner/integration checks,
and immutable image publication are a separate release gate.

Integration builds require a running ClickHouse instance and reachable S3
endpoint. See the quickstart README for deployment, cutover, logs, and rollback.
