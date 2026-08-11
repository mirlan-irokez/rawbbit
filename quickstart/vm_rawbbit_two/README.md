# Rawbbit Two-VM Quickstart

Status: production-oriented single-VM analytics quickstart
Audience: operator deploying ClickHouse, Rawbbit dbt, Rawbbit MCP, Metabase, and Metabase Postgres
Scope: Docker Compose on one Ubuntu 22.04 or 24.04 VM

This guide is provider-neutral. It assumes a fresh Linux VM with a public IP,
DNS control, SSH access from a workstation, and a running Rawbbit ingestion VM
from [`../vm_rawbbit_one/README.md`](../vm_rawbbit_one/README.md).

This document remains the transparent manual installation and troubleshooting
path. For automated host preparation and deployment, including authenticated
Dozzle, use [`../ansible/README.md`](../ansible/README.md). The Ansible path can
deploy VM two independently against any configured S3-compatible raw endpoint.

The two VMs are intentionally coupled through object storage, not direct
service calls:

```text
VM one:
  Collector API -> NATS JetStream -> raw-writer -> SeaweedFS/S3 Parquet

VM two:
  SeaweedFS/S3 Parquet -> scheduled dbt ingestion -> ClickHouse
  -> MCP / Metabase / SQL clients
```

This quickstart runs:

- Caddy
- ClickHouse
- Rawbbit dbt runner
- Rawbbit MCP server
- Metabase
- PostgreSQL for Metabase application state

Optionally, Dozzle can be enabled as a separate Compose overlay for browser
and MCP log access.

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

Scheduled dbt ingestion
  -> SeaweedFS/S3 endpoint from VM one
  -> bounded Parquet windows
  -> ClickHouse analytics.events
```

Public traffic should enter through Caddy only:

```text
mcp.yourdomain.com        -> mcp-server:8000
metabase.yourdomain.com   -> metabase:3000
clickhouse.yourdomain.com -> clickhouse:8123
```

Optional observability path:

```text
Browser / MCP client
  -> Caddy :443
  -> Dozzle :8080
  -> Docker socket
```

ClickHouse direct container ports are bound to `127.0.0.1` for SSH tunnels and
operator checks. The public ClickHouse option is HTTPS through Caddy, not raw
port `8123` or native TCP port `9000`.

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

## 1. Initial VM sizing

For initial setup, the low-memory profile can run on:

- 2 vCPU
- 4 GB RAM
- 80-120 GB SSD
- static public IPv4
- system time configured for UTC
- Ubuntu 24.04 LTS preferred

For small production, use:

- 8 vCPU
- 32 GB RAM
- 200-500 GB SSD or NVMe
- enough disk headroom for ClickHouse merges and temporary query spill

For a larger all-in-one analytics VM, use:

- 8 vCPU or more
- 64 GB RAM
- 500 GB or more SSD/NVMe, sized for retention and query spill

ClickHouse is the main pressure point. Metabase is a Java process, Postgres
wants cache, Docker and the OS need headroom, and ClickHouse needs memory for
scans, joins, grouping, sorting, inserts, background merges, and S3 reads.

## 2. Configure DNS

Create three DNS `A` records pointing to the VM:

```text
mcp.yourdomain.com        -> VM_PUBLIC_IP
metabase.yourdomain.com   -> VM_PUBLIC_IP
clickhouse.yourdomain.com -> VM_PUBLIC_IP
```

If you want optional browser and MCP log access with Dozzle, also create:

```text
logs.yourdomain.com       -> VM_PUBLIC_IP
```

Confirm them before launching:

```bash
dig +short mcp.yourdomain.com
dig +short metabase.yourdomain.com
dig +short clickhouse.yourdomain.com
# Optional Dozzle hostname:
dig +short logs.yourdomain.com
```

Caddy needs working DNS and public access to ports 80 and 443 to obtain TLS
certificates.

## 3. Make the first root connection

**Workstation:** connect with the initial root password or provider console
access:

```bash
ssh root@VM_PUBLIC_IP
```

**Root:** update Ubuntu and install host utilities:

```bash
apt update
apt upgrade -y
apt install -y \
  ca-certificates \
  curl \
  gnupg \
  ufw \
  openssl \
  htop \
  jq \
  nano \
  dnsutils \
  rsync \
  unzip \
  cron

timedatectl set-timezone UTC
```

The host does not initially need:

- Python or pip
- Node.js or Java
- a host-level Caddy package
- a host-level ClickHouse package
- Metabase binaries

Check whether the upgrade requires a reboot:

```bash
if [ -f /var/run/reboot-required ]; then
  cat /var/run/reboot-required
fi
```

If required, reboot before continuing:

```bash
reboot
```

Reconnect after the VM becomes available.

## 4. Configure SSH-key access

**Workstation:** create a dedicated key if necessary:

```bash
ssh-keygen -t ed25519 -C "rawbbit-vm-two" -f ~/.ssh/rawbbit_vm_two
```

Install it for root initially:

```bash
ssh-copy-id -i ~/.ssh/rawbbit_vm_two.pub root@VM_PUBLIC_IP
```

Test key-based access before changing SSH authentication settings:

```bash
ssh -i ~/.ssh/rawbbit_vm_two root@VM_PUBLIC_IP
```

Do not disable password or root access until the long-lived operator login has
also been tested successfully.

## 5. Create the operator account

**Root:** create the long-lived operator account:

```bash
adduser deploy
usermod -aG sudo deploy
```

Copy the authorized SSH keys to it:

```bash
install -d -m 700 -o deploy -g deploy /home/deploy/.ssh
cp /root/.ssh/authorized_keys /home/deploy/.ssh/authorized_keys
chown deploy:deploy /home/deploy/.ssh/authorized_keys
chmod 600 /home/deploy/.ssh/authorized_keys
```

**Workstation:** open a second terminal and test the operator login:

```bash
ssh -i ~/.ssh/rawbbit_vm_two deploy@VM_PUBLIC_IP
```

Keep the existing root session open until this succeeds.

## 6. Configure firewalls

Allow inbound:

- TCP 22, preferably restricted to your administrative IP
- TCP 80, public
- TCP 443, public

Do not expose these container or internal service ports publicly:

- `5432`: Postgres application database
- `8000`: MCP server direct port
- `3000`: Metabase direct port
- `8123`: ClickHouse HTTP
- `9000`: ClickHouse native protocol

The ClickHouse, MCP, and Metabase direct ports are exposed through localhost
bindings for SSH tunnels and host-level checks. Public access goes through
Caddy on port 443.

**Root:** allow SSH from your administrative public IP before enabling the
firewall. Replace `YOUR_ADMIN_PUBLIC_IP` with the workstation's public IP:

```bash
ufw allow from YOUR_ADMIN_PUBLIC_IP to any port 22 proto tcp
```

If you cannot use a stable source IP, `ufw allow OpenSSH` is a broader fallback:

```bash
ufw allow OpenSSH
```

Then allow public HTTP/HTTPS:

```bash
ufw allow 80/tcp
ufw allow 443/tcp
ufw default deny incoming
ufw default allow outgoing
ufw enable
ufw status verbose
```

Keep the existing SSH session open and test a second SSH connection before
closing it.

## 6.1. Remove password-based SSH access

Do this only after confirming that SSH-key login works for the deploy user.

From another terminal, confirm:

```bash
ssh -i ~/.ssh/rawbbit_vm_two deploy@VM_PUBLIC_IP
```

Also confirm deploy can use sudo:

```bash
sudo -v
```

3. Edit the SSH configuration.

Run as root:

```bash
nano /etc/ssh/sshd_config
```

Or as deploy:

```bash
sudo nano /etc/ssh/sshd_config
```

Set:

- `PasswordAuthentication no`
- `PubkeyAuthentication yes`
- `PermitRootLogin prohibit-password`

Meaning:

- `PasswordAuthentication no`: disables SSH password login for every account.
- `PubkeyAuthentication yes`: enables SSH-key authentication.
- `PermitRootLogin prohibit-password`: root may log in using an SSH key, but
  not a password.

Ensure there are no contradictory active declarations later in the file.

4. Create the early-loading Rawbbit policy:

```bash
sudo nano /etc/ssh/sshd_config.d/00-rawbbit-hardening.conf
```

Add:

```text
PasswordAuthentication no
KbdInteractiveAuthentication no
PubkeyAuthentication yes
PermitRootLogin prohibit-password
```

`KbdInteractiveAuthentication no` prevents PAM-backed keyboard-interactive
authentication from remaining as a password-like path. Ubuntu may load
additional configuration from `/etc/ssh/sshd_config.d/`, so the main file and
this early-loading policy must agree.

Inspect effective settings:

```bash
sudo sshd -T | grep -E 'passwordauthentication|kbdinteractiveauthentication|pubkeyauthentication|permitrootlogin'
```

Expected result:

- `passwordauthentication no`
- `kbdinteractiveauthentication no`
- `pubkeyauthentication yes`
- `permitrootlogin prohibit-password` or its equivalent output alias,
  `permitrootlogin without-password`

5. Validate before applying:

```bash
sudo sshd -t
```

6. Reload SSH:

```bash
sudo systemctl reload ssh
sudo systemctl status ssh --no-pager
```

7. Test a new deploy connection before closing the original session.

## 7. Install Docker Engine and Compose

Docker installation modifies apt repositories, system packages, services, and
system groups. Install it as root. Routine Rawbbit Compose operations will be
run by `deploy` afterward.

**Root:** configure Docker's official Ubuntu repository:

```bash
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
  | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
chmod a+r /etc/apt/keyrings/docker.gpg

. /etc/os-release
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" \
  > /etc/apt/sources.list.d/docker.list

apt update
apt install -y \
  docker-ce \
  docker-ce-cli \
  containerd.io \
  docker-buildx-plugin \
  docker-compose-plugin
```

Add the operator to the Docker group:

```bash
usermod -aG docker deploy
```

Docker-group access is effectively root-equivalent. Only trusted operators
should belong to this group.

Log out and reconnect as `deploy` so the new group membership takes effect.

**Deploy:** verify Docker and Compose:

```bash
id
docker version
docker compose version
docker run --rm hello-world
```

From this point onward, run normal Rawbbit Docker and Compose operations as
`deploy`, not root.

## 8. Put the quickstart on the VM

Use a local public-repo checkout on your workstation. Do not clone the
repository on the VM.

**Workstation:** from the repository root, copy the runtime quickstart files:

```bash
rsync -av \
  -e "ssh -i ~/.ssh/rawbbit_vm_two" \
  quickstart/vm_rawbbit_two/ \
  deploy@VM_PUBLIC_IP:/home/deploy/rawbbit-two/
```

**Deploy:** enter the copied directory:

```bash
cd /home/deploy/rawbbit-two
```

Create private env:

```bash
cp .env.example .env
chmod 600 .env
```

Never commit or share `.env`.

## 9. Create persistent host directories

The script needs root privileges because it creates directories under `/srv`.

**Deploy:** from the quickstart directory, run:

```bash
sudo ./bootstrap-host-dirs.sh
```

It creates:

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

These directories hold persistent state. Do not treat them as disposable
container data. The dbt directories are owned by the deployment user so the
non-root dbt-runner can write its lock, logs, and artifacts.

## 10. Generate deployment secrets

**Deploy:** generate independent random values. Place them directly into your
private `.env` or a password manager rather than leaving them in shared notes.

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

Do not place secrets in Git, shared shell history, logs, or chat messages.

## 11. Configure `.env`

**Deploy:** edit the private environment file:

```bash
nano .env
```

At minimum, replace these values:

```env
MCP_PUBLIC_HOSTNAME=mcp.yourdomain.com
METABASE_PUBLIC_HOSTNAME=metabase.yourdomain.com
CLICKHOUSE_PUBLIC_HOSTNAME=clickhouse.yourdomain.com

# Optional; used only if Dozzle is enabled later.
DOZZLE_PUBLIC_HOSTNAME=logs.yourdomain.com

CLICKHOUSE_ADMIN_PASSWORD=...
CLICKHOUSE_MCP_PASSWORD=...
CLICKHOUSE_METABASE_PASSWORD=...
CLICKHOUSE_LOADER_PASSWORD=...
CLICKHOUSE_DBT_PASSWORD=...
MCP_API_KEYS_JSON='{"operator":"long-random-token"}'
POSTGRES_SUPERUSER_PASSWORD=...
METABASE_DB_PASSWORD=...
```

Dozzle uses a browser-session cookie by default, so its login expires when the
browser closes. Leave `DOZZLE_AUTH_TTL` unset unless you deliberately want a
persistent login.

Configure VM-two to read raw Parquet from the S3 endpoint exposed by VM one:

```env
CLICKHOUSE_RAW_S3_ACCESS_KEY=...
CLICKHOUSE_RAW_S3_SECRET_KEY=...
CLICKHOUSE_SEAWEED_S3_ENDPOINT=https://s3.yourdomain.com
CLICKHOUSE_RAW_S3_BUCKET=rawbbit_raw
CLICKHOUSE_RAW_S3_PREFIX=raw
CLICKHOUSE_RAW_S3_URL=https://s3.yourdomain.com/rawbbit_raw/raw/
```

Use a read/list credential from the VM-one SeaweedFS S3 configuration. Do not
use the SeaweedFS administrator credential for the ClickHouse loader.

Start with one explicit raw-ingestion owner and configure the dbt runner:

```env
RAWBBIT_RAW_LOAD_MODE=legacy
DBT_RUNNER_IMAGE=ghcr.io/mirlan-irokez/rawbbit-dbt-runner:0.1.1
DBT_RUNNER_UID=1000
DBT_RUNNER_GID=1000
DBT_THREADS=2
DBT_CLICKHOUSE_MAX_THREADS=2
RAWBBIT_DBT_HOURLY_LOOKBACK_HOURS=3
RAWBBIT_DBT_DAILY_LOOKBACK_DAYS=3
RAWBBIT_DBT_BACKFILL_CHUNK_HOURS=24
```

The public image runs as `1000:1000`. If the deployment user's numeric IDs
differ, use a locally built image with matching `DBT_RUNNER_UID` and
`DBT_RUNNER_GID` so bind-mounted runtime paths remain writable.

Keep MCP authenticated:

```env
MCP_ALLOW_UNAUTHENTICATED=0
```

Do not give MCP or Metabase the ClickHouse admin password.

## 12. Select a ClickHouse resource profile

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

The low-memory defaults are useful for initial setup, testing but should not be mistaken for a
comfortable production analytics machine.

## 13. Validate before launching

**Deploy:** render and validate the Compose configuration:

```bash
docker compose config
```

Review the rendered output carefully, especially:

- public hostnames
- selected ClickHouse resource profile files
- ClickHouse service credentials
- raw-ingestion ownership and dbt reconciliation settings
- MCP authentication settings
- S3 endpoint, bucket, and prefix
- persistent bind-mount paths
- pinned container image tags

Confirm DNS once more:

```bash
dig +short mcp.yourdomain.com
dig +short metabase.yourdomain.com
dig +short clickhouse.yourdomain.com
```

At this point, the VM is prepared for deployment.

## 14. Start the Rawbbit two-VM stack

Pull images:

```bash
docker compose pull
```

Start:

```bash
docker compose up -d
```

Show services:

```bash
docker compose ps
```

Follow logs:

```bash
docker compose logs -f
```

Stop containers without deleting data:

```bash
docker compose down
```

Avoid:

```bash
docker compose down -v
```

This quickstart uses host bind mounts for important state, but avoiding
`down -v` keeps the operational habit simple and prevents accidental named
volume deletion if the file changes later.

## Optional Dozzle Log Access

Dozzle can be added to an already-running Rawbbit analytics VM for browser log
viewing and read-only container log access through its MCP endpoint.

This quickstart keeps Dozzle outside the default stack. Start it only when you
want this operator surface, using the separate overlay file after the user file
and Caddy route are prepared.

Dozzle shows logs for the analytics VM containers, including ClickHouse, the
Rawbbit MCP server, Metabase, Postgres, and Caddy.

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

To expose Dozzle at `https://logs.yourdomain.com`, set:

```env
DOZZLE_PUBLIC_HOSTNAME=logs.yourdomain.com
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
https://logs.yourdomain.com
```

Dozzle MCP is available at:

```text
https://logs.yourdomain.com/api/mcp
```

Dozzle MCP authentication is separate from Rawbbit MCP authentication. Do not
put Dozzle tokens in `MCP_API_KEYS_JSON`; that setting belongs only to the
Rawbbit analytics MCP server.

With Dozzle simple auth, MCP clients need a JWT from Dozzle:

```bash
DOZZLE_JWT=$(
  curl -sSi -X POST https://logs.yourdomain.com/api/token \
    -F username=admin \
    -F password="YOUR_DOZZLE_PASSWORD" |
    tr -d '\r' |
    awk '/^[Ss]et-[Cc]ookie: jwt=/ { sub(/^[Ss]et-[Cc]ookie: jwt=/, ""); sub(/;.*/, ""); print; exit }'
)

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

## 15. First-run initialization

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

Compose also mounts `clickhouse/config.d/raw-s3-named-collection.xml`. The dbt
user receives permission to use only the `rawbbit_raw_s3` named collection;
its S3 values remain non-overridable and are not exposed to the dbt container.

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

If `/srv/rawbbit-two/clickhouse/data` already exists, ClickHouse will not
re-run first-init scripts. Use this manual recovery or upgrade path:

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

## 16. Metabase

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

## 17. ClickHouse access

Recommended operator access is an SSH tunnel:

```bash
ssh -i ~/.ssh/rawbbit_vm_two \
  -L 8123:127.0.0.1:8123 \
  -L 9000:127.0.0.1:9000 \
  deploy@VM_PUBLIC_IP
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
Host: clickhouse.yourdomain.com
Port: 443
Protocol: HTTPS / SSL enabled
User: value of CLICKHOUSE_ADMIN_USER
Password: value of CLICKHOUSE_ADMIN_PASSWORD
Database: analytics
```

Do not configure remote tools to use native TCP on `9000` over the public
internet. If a tool requires native TCP, use the SSH tunnel.

## 18. MCP

The public MCP endpoint is:

```text
https://mcp.yourdomain.com/mcp
```

Clients must send a bearer token matching `MCP_API_KEYS_JSON`:

```text
Authorization: Bearer long-random-token
```

Do not publish MCP publicly with `MCP_ALLOW_UNAUTHENTICATED=1`.

## 19. Raw ingestion ownership

`RAWBBIT_RAW_LOAD_MODE` gives exactly one implementation ownership of
`analytics.events` ingestion:

```env
# Recommended after cutover validation
RAWBBIT_RAW_LOAD_MODE=dbt

# Rollback and compatibility path
# RAWBBIT_RAW_LOAD_MODE=legacy
```

In `dbt` mode, the persistent dbt-runner loads bounded Parquet windows and the
shell loader exits without loading. In `legacy` mode, scheduled dbt jobs log a
skip and the host shell loader can run. Both paths share
`/srv/rawbbit-two/dbt/pipeline.lock`, so scheduled jobs, manual backfills, and
the legacy loader cannot overlap.

Use `legacy` for the first safe deployment, audit the existing table, and then
cut over deliberately.

## 20. dbt runner

`dbt-runner` is an always-running container with Supercronic as PID 1. It runs
hourly and daily `dbt build` jobs and writes stdout/stderr to Docker's log
stream. Dozzle discovers that stream without another log sidecar.

Pull, start, and inspect it:

```bash
docker compose pull dbt-runner
docker compose up -d --no-build dbt-runner
docker compose logs -f dbt-runner
```

The UTC schedule is versioned in `../../dbt_project/crontab`:

```text
12 * * * * hourly build
30 3 * * * daily reconciliation
```

The hourly job loads a short overlapping window. The daily job reconciles a
longer range for late files. Supercronic does not replay missed ticks; these
overlapping windows provide the recovery path.

The selected model uses `(app_id, event_id)` as its stable replacement key and
dbt-clickhouse's `delete_insert` incremental strategy. `dbt build` then runs
the ingestion data tests. The current project has only this ingestion model;
it does not yet create staging or mart tables.

An hour with no matching Parquet files succeeds with zero input rows. S3
availability, credential, or invalid-Parquet failures still fail the build.

Manual backfills require `RAWBBIT_RAW_LOAD_MODE=dbt` and use the same lock and
model selector as scheduled jobs:

```bash
docker compose exec -T \
  -e RAWBBIT_DBT_MIRROR_PID1=1 \
  dbt-runner \
  /app/bin/dbt-job backfill \
  2026-07-01T00:00:00Z \
  2026-07-05T00:00:00Z
```

The timestamps must be hour-aligned UTC. The wrapper waits for the lock and
processes the range in configurable chunks. `RAWBBIT_DBT_MIRROR_PID1=1`
mirrors manual output into the persistent container's Docker log stream for
Dozzle.

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
rows. Investigate blank application IDs because unrelated events sharing the
same fallback application identity can collide. Clean incompatible legacy data
before switching modes.

### Cut over raw ingestion to dbt

1. Run the existing-event audit above.
2. Validate several windows against a temporary environment or shadow table.
3. Verify every ClickHouse service-user password in `.env`, then set
   `RAWBBIT_RAW_LOAD_MODE=dbt`.
4. Recreate ClickHouse and wait for it to load the named S3 collection:

```bash
docker compose up -d --force-recreate --wait clickhouse
```

5. Existing ClickHouse data directories do not rerun first-init scripts. Apply
   the idempotent service-user script explicitly; it creates the dbt user,
   reapplies grants, and synchronizes service-user passwords with `.env`:

```bash
docker compose exec -T clickhouse \
  bash /docker-entrypoint-initdb.d/02_service_users.sh
```

6. Recreate and observe the dbt runner:

```bash
docker compose up -d --force-recreate --wait dbt-runner
docker compose logs -f dbt-runner
```

The installed legacy cron can remain because the shell loader checks the mode
on every invocation and exits without loading. Removing the cron still reduces
operational noise.

## 21. Legacy shell loader and rollback

The legacy loader reads the previous UTC hour into `analytics.events`. It uses
`CLICKHOUSE_LOADER_USER`, not the admin account, and requires AWS CLI on the
host.

Install AWS CLI v2 before relying on this rollback path:

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

To roll back:

1. Set `RAWBBIT_RAW_LOAD_MODE=legacy`.
2. Recreate `dbt-runner` so its scheduled jobs begin skipping.
3. Run `bash clickhouse/load_events_hourly.sh` manually.
4. Run `bash install-hourly-loader-cron.sh` if the legacy cron is absent.

The installer refuses to add a legacy cron while the mode is `dbt`. In legacy
mode it installs this idempotent default schedule:

```text
7 * * * * cd QUICKSTART_DIR && bash clickhouse/load_events_hourly.sh >> ~/rawbbit-two-load-events.log 2>&1
```

Inspect it with:

```bash
crontab -l | grep rawbbit-two-load-events
```

## Verification

Caddy and Metabase:

```bash
curl -I https://metabase.yourdomain.com
```

MCP initialize through Caddy:

```bash
curl -i https://mcp.yourdomain.com/mcp \
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
curl -u "$CLICKHOUSE_ADMIN_USER:$CLICKHOUSE_ADMIN_PASSWORD" https://clickhouse.yourdomain.com/ping
```

ClickHouse table check:

```bash
docker compose exec -T clickhouse bash -lc \
  'clickhouse-client -u "$CLICKHOUSE_USER" --password "$CLICKHOUSE_PASSWORD" --query "SELECT count() FROM analytics.events"'
```

dbt project and connection checks:

```bash
docker compose exec -T dbt-runner dbt debug \
  --project-dir /app --profiles-dir /app
docker compose exec -T dbt-runner dbt parse \
  --project-dir /app --profiles-dir /app
```

Postgres state path:

```bash
sudo du -sh /srv/rawbbit-two/postgres
```

dbt runner and optional legacy-loader logs:

```bash
docker compose logs --tail=100 dbt-runner
tail -n 100 ~/rawbbit-two-load-events.log
```

## Changing ClickHouse profiles later

Update `.env` with the desired profile pair, then recreate the ClickHouse
container:

```bash
docker compose up -d --force-recreate clickhouse
```

A plain restart keeps the existing container and old mounts:

```bash
docker compose restart clickhouse
```

Use recreate when the selected XML profile files change.

Verify the active query settings:

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

## Operations

Routine stack operations run as `deploy`:

```bash
docker compose ps
docker compose logs --tail=100 clickhouse
docker compose logs --tail=100 dbt-runner
docker compose logs --tail=100 mcp-server
docker compose logs --tail=100 metabase
docker compose logs --tail=100 postgres
docker compose pull
docker compose up -d
docker compose down
```

Watch disk and ClickHouse state:

```bash
df -h
docker system df
sudo du -sh /srv/rawbbit-two/clickhouse/data
sudo du -sh /srv/rawbbit-two/postgres
```

Do not use `docker compose down -v` as a routine command.

## Security Notes

- Keep `.env` private and mode `0600`.
- Do not commit real passwords, S3 credentials, or MCP tokens.
- Do not expose Postgres or raw ClickHouse ports directly to the public
  internet.
- Public ClickHouse HTTPS depends on a strong ClickHouse admin password and a
  DNS hostname controlled by the operator.
- Keep bootstrap/admin credentials out of dbt, MCP, Metabase, and cron jobs.
- Use read/list S3 credentials for the ClickHouse named collection and legacy loader.
- Caddy certificate state is persisted under `/srv/rawbbit-two/caddy`.
- Docker group access is effectively root-equivalent; only trusted operators
  should belong to it.
