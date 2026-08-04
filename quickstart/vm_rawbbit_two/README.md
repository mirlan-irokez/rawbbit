# Rawbbit Two-VM Quickstart

Status: production-oriented single-VM analytics quickstart
Audience: operator deploying ClickHouse, Rawbbit MCP, Metabase, and Metabase Postgres
Scope: Docker Compose on one Linux VM

This quickstart runs:

- Caddy
- ClickHouse
- Rawbbit dbt runner
- Rawbbit MCP server
- Metabase
- PostgreSQL for Metabase application state

The approach matches `quickstart/vm_rawbbit_one`: one Compose stack, one Caddy
ingress, pinned images, explicit host directories under `/srv`, and private
`.env` values kept out of Git.

## Architecture

```text
MCP clients / agents
  -> Caddy :443
  -> mcp-server :8000
  -> ClickHouse :8123
  -> /srv/rawbbit-two/clickhouse

IDE / HTTPS ClickHouse clients
  -> Caddy :443
  -> ClickHouse :8123
  -> /srv/rawbbit-two/clickhouse

Browser
  -> Caddy :443
  -> Metabase :3000
  -> Postgres :5432
  -> /srv/rawbbit-two/postgres

Metabase analytics connection
  -> ClickHouse :8123

Raw Parquet in S3 / SeaweedFS
  -> dbt-runner scheduled job (dbt mode)
  -> ClickHouse analytics.events
```

Public traffic should enter through Caddy only:

```text
mcp.example.com      -> mcp-server:8000
metabase.example.com -> metabase:3000
clickhouse.example.com -> clickhouse:8123
```

ClickHouse direct container ports are still bound to `127.0.0.1` for SSH
tunnels and local operator checks. The public option is HTTPS through Caddy,
not opening raw `8123` or native `9000` to the internet.

## Files

```text
quickstart/vm_rawbbit_two/
  README.md
  docker-compose.yml
  docker-compose.dozzle.yml
  .env.example
  Caddyfile
  Caddyfile.dozzle.example
  bootstrap-host-dirs.sh
  install-hourly-loader-cron.sh
  ../../dbt_project/
    Dockerfile
    dbt_project.yml
    profiles.yml
    selectors.yml
    crontab
    bin/dbt-job
    models/
    macros/
    tests/
  clickhouse/
    config.d/
      low-memory.xml
      production-small.xml
      production-medium.xml
      raw-s3-named-collection.xml
    initdb.d/
      02_service_users.sh
    users.d/
      low-memory-users.xml
      production-small-users.xml
      production-medium-users.xml
    load_events_hourly.sh
    schema_analytics_events.sql
  postgres/
    initdb.d/
      01_metabase_database.sh
```

## Plan

1. Prepare the VM the same way as `vm_rawbbit_one`: Ubuntu, SSH keys, `deploy`
   user, UFW, Docker Engine, and Docker Compose plugin.
2. Point DNS records for `MCP_PUBLIC_HOSTNAME`, `METABASE_PUBLIC_HOSTNAME`,
   and `CLICKHOUSE_PUBLIC_HOSTNAME` to the VM.
3. Copy or clone this quickstart to the VM.
4. Run `sudo ./bootstrap-host-dirs.sh` to create persistent state paths.
5. Create `.env` from `.env.example` and generate real passwords and MCP
   bearer tokens.
6. Validate with `docker compose config`.
7. Pull the published service images and start the stack.
8. Confirm first-run init created `analytics.events`, ClickHouse service
   users, and the Metabase Postgres application database.
9. Configure Metabase's analytics database connection to ClickHouse.
10. Start in `RAWBBIT_RAW_LOAD_MODE=legacy`, then optionally install the
    existing hourly loader cron after validating S3 reader credentials.
11. Validate dbt ingestion in a temporary environment before cutting raw
    ingestion over from the legacy loader.

## Persistent State

`bootstrap-host-dirs.sh` creates:

```text
/srv/rawbbit-two/clickhouse/data
/srv/rawbbit-two/clickhouse/logs
/srv/rawbbit-two/postgres
/srv/rawbbit-two/caddy/data
/srv/rawbbit-two/caddy/config
/srv/rawbbit-two/dbt
/srv/rawbbit-two/dbt/logs
/srv/rawbbit-two/dbt/target
```

These paths are the operational state boundary. They are deliberately outside
Docker-managed named volumes so they can be inspected, backed up, migrated, and
recovered the same way as the `vm_rawbbit_one` quickstart state.

The Compose file mounts one selected ClickHouse server config file and one
selected ClickHouse users/profile config file. Do not mount the whole
`users.d` directory read-only: the ClickHouse Docker entrypoint writes
`default-user.xml` there when `CLICKHOUSE_ADMIN_USER` /
`CLICKHOUSE_ADMIN_PASSWORD` are set.

## DNS

Create three DNS `A` records pointing to the VM:

```text
mcp.example.com        -> VM_PUBLIC_IP
metabase.example.com   -> VM_PUBLIC_IP
clickhouse.example.com -> VM_PUBLIC_IP
```

If you want optional browser and MCP log access with Dozzle, also create:

```text
logs.example.com       -> VM_PUBLIC_IP
```

Confirm them before launching:

```bash
dig +short mcp.example.com
dig +short metabase.example.com
dig +short clickhouse.example.com
# Optional Dozzle hostname:
dig +short logs.example.com
```

Caddy needs working DNS and public access to ports `80` and `443` to obtain
TLS certificates.

## Firewall

Allow inbound:

- TCP `22`, preferably restricted to your administrative IP
- TCP `80`, public
- TCP `443`, public

Do not expose these ports publicly:

- `5432`: Postgres application database
- `8000`: MCP server direct port
- `3000`: Metabase direct port
- `8123`: ClickHouse HTTP
- `9000`: ClickHouse native protocol

The Compose file binds ClickHouse, MCP, and Metabase direct ports to
`127.0.0.1` only. ClickHouse HTTPS access is provided by Caddy on port `443`.

## Configure

Create private env:

```bash
cp .env.example .env
chmod 600 .env
nano .env
```

At minimum replace:

```env
MCP_PUBLIC_HOSTNAME=mcp.yourdomain.com
METABASE_PUBLIC_HOSTNAME=metabase.yourdomain.com
CLICKHOUSE_PUBLIC_HOSTNAME=clickhouse.yourdomain.com

# Optional; used only if Dozzle is enabled later.
DOZZLE_PUBLIC_HOSTNAME=logs.yourdomain.com
DOZZLE_AUTH_TTL=48h

CLICKHOUSE_ADMIN_PASSWORD=...
CLICKHOUSE_MCP_PASSWORD=...
CLICKHOUSE_METABASE_PASSWORD=...
CLICKHOUSE_LOADER_PASSWORD=...
CLICKHOUSE_DBT_PASSWORD=...
MCP_API_KEYS_JSON='{"mirlan":"long-random-token"}'
POSTGRES_SUPERUSER_PASSWORD=...
METABASE_DB_PASSWORD=...
CLICKHOUSE_RAW_S3_ACCESS_KEY=...
CLICKHOUSE_RAW_S3_SECRET_KEY=...
CLICKHOUSE_SEAWEED_S3_ENDPOINT=https://s3.yourdomain.com
CLICKHOUSE_RAW_S3_BUCKET=rawbbit_raw
CLICKHOUSE_RAW_S3_PREFIX=raw
CLICKHOUSE_RAW_S3_URL=https://s3.yourdomain.com/rawbbit_raw/raw/
RAWBBIT_RAW_LOAD_MODE=legacy
DBT_RUNNER_UID=1000
DBT_RUNNER_GID=1000
```

`CLICKHOUSE_RAW_S3_URL` is the full prefix used by the ClickHouse named
collection and must end with `/`. Keep it consistent with the separate
endpoint, bucket, and prefix values used by the legacy loader.

The published dbt-runner image uses numeric UID/GID `1000:1000`. Confirm that
the deployment user has those IDs so the non-root dbt process and legacy host
loader can share the lock and runtime directory:

```bash
id -u deploy
id -g deploy
```

If either value differs, use the local-build fallback in `.env`, set the
matching IDs, and build the image on the VM:

```env
DBT_RUNNER_IMAGE=rawbbit-dbt-runner:local
DBT_RUNNER_UID=YOUR_DEPLOY_UID
DBT_RUNNER_GID=YOUR_DEPLOY_GID
```

```bash
docker compose build dbt-runner
```

## ClickHouse Resource Profiles

This quickstart includes three ClickHouse resource profiles:

```text
low-memory.xml + low-memory-users.xml
  Original 2 vCPU / 4 GB RAM VM guardrails. This is the default.

production-small.xml + production-small-users.xml
  All-in-one 8 vCPU / 32 GB RAM production VM.

production-medium.xml + production-medium-users.xml
  All-in-one 8 vCPU / 64 GB RAM production VM.
```

The profile files are not all mounted at once. Compose selects exactly one
server config file and one users/profile config file using `.env`.

For a 32 GB RAM VM:

```env
CLICKHOUSE_CONFIG_PROFILE_FILE=production-small.xml
CLICKHOUSE_USERS_PROFILE_FILE=production-small-users.xml
```

For a 64 GB RAM VM:

```env
CLICKHOUSE_CONFIG_PROFILE_FILE=production-medium.xml
CLICKHOUSE_USERS_PROFILE_FILE=production-medium-users.xml
```

Apply a profile change by recreating only the ClickHouse container:

```bash
docker compose up -d --force-recreate clickhouse
```

Then verify the active settings:

```bash
docker compose exec -T clickhouse bash -lc \
  'clickhouse-client -u "$CLICKHOUSE_USER" --password "$CLICKHOUSE_PASSWORD" --query "
SELECT
  getSetting('\''max_memory_usage'\''),
  getSetting('\''max_memory_usage_for_user'\''),
  getSetting('\''max_threads'\''),
  getSetting('\''max_bytes_before_external_group_by'\''),
  getSetting('\''max_bytes_before_external_sort'\'')
"'
```

Generate secrets on the VM and move them directly into `.env` or a password
manager:

```bash
openssl rand -hex 32  # ClickHouse admin password
openssl rand -hex 32  # Rawbbit MCP server password
openssl rand -hex 32  # ClickHouse Metabase password
openssl rand -hex 32  # ClickHouse loader password
openssl rand -hex 32  # ClickHouse dbt password
openssl rand -hex 32  # MCP bearer token
openssl rand -hex 32  # Postgres superuser password
openssl rand -hex 32  # Metabase application DB password
openssl rand -hex 24  # S3 reader access key
openssl rand -hex 32  # S3 reader secret key
```

## Credential Model

The quickstart separates bootstrap credentials from service credentials:

- `CLICKHOUSE_ADMIN_USER` / `CLICKHOUSE_ADMIN_PASSWORD`: ClickHouse
  bootstrap and operator account. Use this for trusted administrative work and
  IDE access.
- `CLICKHOUSE_MCP_USER` / `CLICKHOUSE_MCP_PASSWORD`: read-only ClickHouse
  account used by the MCP server.
- `CLICKHOUSE_METABASE_USER` / `CLICKHOUSE_METABASE_PASSWORD`: read-only
  ClickHouse account for Metabase analytics queries.
- `CLICKHOUSE_LOADER_USER` / `CLICKHOUSE_LOADER_PASSWORD`: ClickHouse account
  used by the hourly Parquet loader to insert into `analytics.events`.
- `CLICKHOUSE_DBT_USER` / `CLICKHOUSE_DBT_PASSWORD`: ClickHouse account used
  by dbt for bounded raw ingestion and analytics model materialization.
- `POSTGRES_SUPERUSER` / `POSTGRES_SUPERUSER_PASSWORD`: Postgres bootstrap
  superuser for container initialization only.
- `METABASE_DB_USER` / `METABASE_DB_PASSWORD`: Postgres application database
  account used by Metabase.

Do not give MCP or Metabase the ClickHouse admin password. Do not give
Metabase the Postgres superuser password.

## Start

Create host directories:

```bash
sudo ./bootstrap-host-dirs.sh
```

Validate Compose:

```bash
docker compose config
```

Start the stack:

```bash
docker compose pull caddy clickhouse dbt-runner mcp-server postgres metabase
docker compose up -d
docker compose ps
```

Watch logs:

```bash
docker compose logs -f
```

## Optional Dozzle Log Access

Dozzle can be added to an already-running Rawbbit analytics VM for browser log
viewing and read-only container log access through its MCP endpoint.

This quickstart keeps Dozzle outside the default stack. Start it only when you
want this operator surface, using the separate overlay file after the user file
and Caddy route are prepared.

Dozzle does not enable authentication by default. The provided overlay enables
Dozzle simple auth and persists its user database under:

```text
/srv/rawbbit-two/dozzle/users.yml
```

Create the Dozzle data directory and first admin user:

```bash
sudo mkdir -p /srv/rawbbit-two/dozzle
sudo chown -R deploy:deploy /srv/rawbbit-two/dozzle

docker run -it --rm amir20/dozzle:v10.6.13 \
  generate admin \
  --email admin@example.com \
  --name "Admin" \
  > /srv/rawbbit-two/dozzle/users.yml

chmod 600 /srv/rawbbit-two/dozzle/users.yml
```

Omit `--password` as shown above so Dozzle prompts for it interactively instead
of storing the password in shell history.

To expose Dozzle at `https://logs.example.com`, set:

```env
DOZZLE_PUBLIC_HOSTNAME=logs.example.com
```

Then append the optional Caddy route:

```bash
grep -q 'DOZZLE_PUBLIC_HOSTNAME' Caddyfile || cat Caddyfile.dozzle.example >> Caddyfile
```

Validate the combined Compose configuration:

```bash
docker compose -f docker-compose.yml -f docker-compose.dozzle.yml config
```

Start Dozzle and update the Caddy container with the Dozzle hostname:

```bash
docker compose -f docker-compose.yml -f docker-compose.dozzle.yml up -d caddy dozzle
```

Open:

```text
https://logs.example.com
```

Dozzle MCP is available at:

```text
https://logs.example.com/api/mcp
```

Dozzle MCP authentication is separate from Rawbbit MCP authentication. Do not
put Dozzle tokens in `MCP_API_KEYS_JSON`; that setting belongs only to the
Rawbbit analytics MCP server.

With Dozzle simple auth, MCP clients need a JWT from Dozzle:

```bash
read -rsp "Dozzle password: " DOZZLE_PASSWORD; echo

DOZZLE_JWT=$(
  curl -sSi -X POST https://logs.example.com/api/token \
    -F username=admin \
    -F password="$DOZZLE_PASSWORD" |
    tr -d '\r' |
    awk '/^[Ss]et-[Cc]ookie: jwt=/ { sub(/^[Ss]et-[Cc]ookie: jwt=/, ""); sub(/;.*/, ""); print; exit }'
)

unset DOZZLE_PASSWORD
printf '%s\n' "$DOZZLE_JWT"
```

Configure MCP clients to send:

```text
Authorization: Bearer YOUR_DOZZLE_JWT
```

The MCP endpoint is part of Dozzle's authenticated API group. If the token
expires, request a new one using the same `/api/token` flow.

Security notes:

- Do not expose Dozzle without authentication.
- Do not expose Dozzle's raw container port directly.
- Keep Dozzle shell and container actions disabled.
- The Docker socket is sensitive even when mounted read-only.

## First-Run Initialization

On first initialization of an empty ClickHouse data directory, the Compose stack
mounts these files into `/docker-entrypoint-initdb.d/`:

```text
clickhouse/schema_analytics_events.sql
clickhouse/initdb.d/02_service_users.sh
```

The ClickHouse Docker entrypoint runs them automatically, creating:

```text
analytics.events
CLICKHOUSE_MCP_USER
CLICKHOUSE_METABASE_USER
CLICKHOUSE_LOADER_USER
CLICKHOUSE_DBT_USER
```

On first initialization of an empty Postgres data directory, the Compose stack
mounts this file into `/docker-entrypoint-initdb.d/`:

```text
postgres/initdb.d/01_metabase_database.sh
```

It creates:

```text
METABASE_DB_NAME
METABASE_DB_USER
```

The ClickHouse SQL uses `IF NOT EXISTS`, so it can also be applied manually
when needed.
For an existing `/srv/rawbbit-two/clickhouse/data` directory, ClickHouse will
not re-run first-init scripts. Use the commands below as the manual recovery or
upgrade path:

```bash
docker compose exec -T clickhouse bash -lc \
  'clickhouse-client -u "$CLICKHOUSE_USER" --password "$CLICKHOUSE_PASSWORD"' \
  < clickhouse/schema_analytics_events.sql

docker compose exec -T clickhouse \
  bash /docker-entrypoint-initdb.d/02_service_users.sh
```

Verify the table exists:

```bash
docker compose exec -T clickhouse bash -lc \
  'clickhouse-client -u "$CLICKHOUSE_USER" --password "$CLICKHOUSE_PASSWORD" --query "SHOW TABLES FROM analytics"'
```

Postgres also only runs `/docker-entrypoint-initdb.d/` on an empty
`/srv/rawbbit-two/postgres` state directory. If the Postgres state directory
already exists, create or migrate the Metabase application database manually
with the Postgres superuser.

## Metabase

Metabase uses the Compose-managed Postgres service for its application
database:

```text
metabase -> postgres:5432 -> /srv/rawbbit-two/postgres
```

After first login, add ClickHouse as an analytics database in Metabase:

```text
Host: clickhouse
Port: 8123
Database: analytics
User: value of CLICKHOUSE_METABASE_USER
Password: value of CLICKHOUSE_METABASE_PASSWORD
SSL: disabled inside the Compose network
```

Keep this separate from the Metabase application database settings. The
Postgres database stores Metabase state; ClickHouse stores Rawbbit analytics
events.

## ClickHouse Access

Recommended operator access is still an SSH tunnel:

```bash
ssh -L 8123:127.0.0.1:8123 -L 9000:127.0.0.1:9000 deploy@VM_PUBLIC_IP
```

Then connect IDEs or local tools to:

```text
Host: 127.0.0.1
HTTP port: 8123
Native port: 9000
Protocol: HTTP or native TCP, depending on the tool
User: value of CLICKHOUSE_ADMIN_USER
Password: value of CLICKHOUSE_ADMIN_PASSWORD
Database: analytics
```

For remote tools that support ClickHouse over HTTP/HTTPS, use the public Caddy
route:

```text
Host: clickhouse.example.com
Port: 443
Protocol: HTTPS / SSL enabled
User: value of CLICKHOUSE_ADMIN_USER
Password: value of CLICKHOUSE_ADMIN_PASSWORD
Database: analytics
```

Do not configure remote tools to use native TCP on `9000` over the public
internet. If a tool requires native TCP, use the SSH tunnel.

## MCP

The public MCP endpoint is:

```text
https://mcp.example.com/mcp
```

Clients must send a bearer token matching `MCP_API_KEYS_JSON`:

```text
Authorization: Bearer long-random-token
```

Do not publish MCP publicly with `MCP_ALLOW_UNAUTHENTICATED=1`.

## Hourly Loader

`clickhouse/load_events_hourly.sh` loads Parquet files from the previous UTC
hour into `analytics.events`. It remains the supported legacy ingestion path.

The loader runs only when `.env` contains:

```env
RAWBBIT_RAW_LOAD_MODE=legacy
```

When the value is `dbt`, both the loader and its cron entry safely no-op or
refuse installation. The shell loader and dbt runner share
`/srv/rawbbit-two/dbt/pipeline.lock` so ingestion jobs cannot overlap
accidentally.

The loader connects to ClickHouse with `CLICKHOUSE_LOADER_USER` and
`CLICKHOUSE_LOADER_PASSWORD`, not the admin account.

It requires AWS CLI on the host. Install AWS CLI v2 with the official Linux
installer:

```bash
sudo apt update
sudo apt install -y curl unzip

tmpdir="$(mktemp -d)"
arch="$(uname -m)"
case "$arch" in
  x86_64) aws_arch="x86_64" ;;
  aarch64|arm64) aws_arch="aarch64" ;;
  *) echo "Unsupported architecture: ${arch}" >&2; exit 1 ;;
esac

curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-${aws_arch}.zip" \
  -o "${tmpdir}/awscliv2.zip"
unzip -q "${tmpdir}/awscliv2.zip" -d "${tmpdir}"
sudo "${tmpdir}/aws/install" --update
rm -rf "${tmpdir}"

aws --version
```

Run manually first:

```bash
bash clickhouse/load_events_hourly.sh
```

Then install the user cron entry:

```bash
bash install-hourly-loader-cron.sh
```

By default, the installer writes this schedule for the current user:

```text
7 * * * * cd QUICKSTART_DIR && bash clickhouse/load_events_hourly.sh >> ~/rawbbit-two-load-events.log 2>&1
```

To override the schedule or log file for installation:

```bash
RAWBBIT_TWO_LOADER_CRON="12 * * * *" \
RAWBBIT_TWO_LOADER_LOG="/home/deploy/rawbbit-two-load-events.log" \
bash install-hourly-loader-cron.sh
```

The installer is idempotent: it replaces an existing line marked
`rawbbit-two-load-events` instead of adding duplicates.

Inspect the installed cron:

```bash
crontab -l | grep rawbbit-two-load-events
```

## dbt Runner

`dbt-runner` is an always-running container with Supercronic as PID 1. It
runs hourly and daily dbt builds and writes stdout/stderr to the same Docker
JSON log stream used by the rest of the stack. Dozzle discovers that stream
without another logging container.

The safe initial deployment mode is:

```env
RAWBBIT_RAW_LOAD_MODE=legacy
```

In that mode, the existing shell loader owns `analytics.events`; dbt has no
models to build because v1 contains only the optional ingestion model. The
runner's scheduled jobs log a skip. Pull and start the runner:

```bash
docker compose pull dbt-runner
docker compose up -d --no-build dbt-runner
docker compose logs -f dbt-runner
```

The schedules are UTC and versioned in `../../dbt_project/crontab`:

```text
12 * * * * hourly build
30 3 * * * daily reconciliation
```

Configure lookbacks and concurrency in `.env`:

```env
DBT_THREADS=2
DBT_CLICKHOUSE_MAX_THREADS=2
RAWBBIT_DBT_HOURLY_LOOKBACK_HOURS=3
RAWBBIT_DBT_DAILY_LOOKBACK_DAYS=3
RAWBBIT_DBT_BACKFILL_CHUNK_HOURS=24
```

Manual backfill requires `RAWBBIT_RAW_LOAD_MODE=dbt` and uses the same lock
and model selection as scheduled jobs:

```bash
docker compose exec -T \
  -e RAWBBIT_DBT_MIRROR_PID1=1 \
  dbt-runner \
  /app/bin/dbt-job backfill \
  2026-07-01T00:00:00Z \
  2026-07-05T00:00:00Z
```

The timestamps must be hour-aligned UTC. The wrapper waits for a running job
and processes the range in configurable chunks. Setting
`RAWBBIT_DBT_MIRROR_PID1=1` mirrors manual output into the persistent
container's Docker log stream for Dozzle.

### Audit existing events before cutover

The dbt ingestion tests validate the complete `analytics.events` table, not
only the current load window. Audit data written by the legacy loader before
enabling dbt ingestion:

```bash
docker compose exec -T clickhouse bash -lc \
  'clickhouse-client -u "$CLICKHOUSE_USER" --password "$CLICKHOUSE_PASSWORD" --multiquery' <<'SQL'
SELECT
    countIf(event_id IS NULL OR event_id = '') AS invalid_event_ids,
    countIf(trim(app_id) = '') AS blank_app_ids
FROM analytics.events;

SELECT app_id, event_id, count() AS rows
FROM analytics.events
GROUP BY app_id, event_id
HAVING rows > 1
LIMIT 100;
SQL
```

`invalid_event_ids` must be zero and the duplicate-key query must return no
rows before cutover. Investigate blank application IDs because unrelated
events sharing the same fallback application identity can collide. Stop and
clean the legacy data before switching modes if either dbt invariant fails.

### Cut over raw ingestion to dbt

1. Run the existing-event audit above.
2. Validate several windows against a temporary environment or shadow table.
3. Verify every ClickHouse service-user password in `.env`, then set
   `RAWBBIT_RAW_LOAD_MODE=dbt`.
4. Recreate ClickHouse and wait for it to load the named S3 collection:

```bash
docker compose up -d --force-recreate --wait clickhouse
```

5. Existing ClickHouse data directories do not rerun first-init scripts.
   Apply the idempotent service-user script explicitly; it creates the dbt
   user, reapplies grants, and synchronizes all service-user passwords with
   the current `.env` values:

```bash
docker compose exec -T clickhouse \
  bash /docker-entrypoint-initdb.d/02_service_users.sh
```

6. Recreate and observe the dbt runner:

```bash
docker compose up -d --force-recreate --wait dbt-runner
docker compose logs -f dbt-runner
```

The installed legacy cron can remain in place because the shell loader reads
the mode on every invocation and exits without loading. Removing the cron is
still recommended to reduce operational noise.

### Roll back to the shell loader

1. Set `RAWBBIT_RAW_LOAD_MODE=legacy`.
2. Recreate `dbt-runner`.
3. Run `bash clickhouse/load_events_hourly.sh` manually.
4. Reinstall the cron with `bash install-hourly-loader-cron.sh` if needed.

The dbt runner's scheduled jobs now skip; the legacy cron owns ingestion.

## Verification

Caddy and Metabase:

```bash
curl -I https://metabase.example.com
```

MCP initialize through Caddy:

```bash
curl -i https://mcp.example.com/mcp \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Accept: application/json, text/event-stream" \
  -H "Content-Type: application/json" \
  --data '{
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
      "protocolVersion": "2025-03-26",
      "capabilities": {},
      "clientInfo": {
        "name": "curl",
        "version": "0.1.0"
      }
    }
  }'
```

ClickHouse local health:

```bash
curl http://127.0.0.1:8123/ping
```

ClickHouse HTTPS health through Caddy:

```bash
curl -u "$CLICKHOUSE_ADMIN_USER:$CLICKHOUSE_ADMIN_PASSWORD" https://clickhouse.example.com/ping
```

Postgres state path:

```bash
sudo du -sh /srv/rawbbit-two/postgres
```

## Operations

Routine stack operations run as `deploy`:

```bash
docker compose ps
docker compose logs --tail=100 clickhouse
docker compose logs --tail=100 dbt-runner
docker compose logs --tail=100 mcp-server
docker compose logs --tail=100 metabase
docker compose logs --tail=100 postgres
docker compose pull caddy clickhouse dbt-runner mcp-server postgres metabase
docker compose up -d
docker compose down
```

Do not use `docker compose down -v` as a routine command. This quickstart uses
host bind mounts, but destructive volume habits are still how state gets lost
when the architecture later changes.

## Security Notes

- Keep `.env` private and mode `0600`.
- Do not commit real passwords, S3 credentials, or MCP tokens.
- Do not expose Postgres or raw ClickHouse ports directly to the public
  internet.
- Public ClickHouse HTTPS depends on a strong ClickHouse admin password and a
  DNS hostname controlled by the operator.
- Keep bootstrap/admin credentials out of MCP, Metabase, and cron jobs.
- Caddy certificate state is persisted under `/srv/rawbbit-two/caddy`.
- Docker group access is effectively root-equivalent; only trusted operators
  should belong to it.
