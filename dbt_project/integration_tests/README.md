# Disposable dbt routing integration harness

Opt-in; requires Docker, a cached/pullable `clickhouse/clickhouse-server:24.8`,
and `chrislusf/seaweedfs:3.99`. The fixture copies the current dbt project,
including `Dockerfile` and `crontab`, then builds a temporary candidate directly
from the production `dbt_project/Dockerfile` and pinned requirements. It does
not use a published dbt-runner image as its base. The candidate checks dbt-core
1.9.10, dbt-clickhouse 1.9.8, and clickhouse-connect 1.6.0. No production
endpoints or credentials are used.
The test launches an isolated network, ClickHouse, and SeaweedFS S3 store;
all tables, Parquet objects, ClickHouse accounts and job journals disappear at
teardown, including on test failure. Docker must be accessible to the caller.

From the repository root, with the repo-local venv (requires `pytest`,
`pyarrow`, `requests`):

```sh
RAWBBIT_DBT_INTEGRATION=1 ./venv/bin/python -m pytest -q dbt_project/integration_tests
```

Without `RAWBBIT_DBT_INTEGRATION=1` the tests skip.
`RAWBBIT_INTEGRATION_KEEP=1` retains containers temporarily for debugging;
**never use it with production data**. Without that flag cleanup runs even
when assertions fail. No Compose project, privileged host filesystem, or
schema migration is required.

The integration cases exercise real Parquet ingestion and the pinned
adapter, scoped grants and named-collection access, routing placement and
default-complement filtering, invalid-config rejection, per-route fallback,
delete-before-insert faults with and without partial inserts, replay, and
recovery. Control-plane checks cover exact ClickHouse query IDs, cancellation
limited to the runner's owned tagged work, terminal `system.query_log` proof,
pending-mutation fencing, and denied-observation behavior. The client-timeout
case requires the server request to be observed terminal before fallback is
allowed. Fallback is intentionally **not** cross-database deduplication: the
tests inspect both destinations after a failed primary and replay.

Progress-focused integration coverage uses the disposable candidate and pinned
dbt API. The cases exercise live-child callback output, a pinned-API failure
matrix, manual `RAWBBIT_DBT_MIRROR_PID1=1` visibility, and a test-only
Supercronic schedule. Assertions cover approved `NodeStart`/`NodeFinished`
labels, safe stderr formatting and job/attempt context, separation of the final
JSON summary on stdout, and sanitized failure diagnostics. They do not expose
raw dbt messages or exceptions, and progress is not proof of ClickHouse
completion; existing server-settlement checks remain authoritative. This
describes test coverage, not a claim that the integration suite has passed.
Case counts can change as coverage evolves.
