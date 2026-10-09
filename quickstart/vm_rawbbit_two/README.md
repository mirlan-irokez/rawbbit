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
  -> ClickHouse configured default events table (normally analytics.events)
  -> optional explicit app_id routes to other existing events tables
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
  docker-compose.datasets.yml  # opt-in multi-dataset MCP runtime overlay
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
    provision_datasets.py      # host-side, privileged dataset provisioner
    tests/
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

<!-- dataset-mcp:start -->
## Optional multi-dataset MCP

The default stack remains unchanged with no dataset registry. Multi-dataset
support is opt-in through `docker-compose.datasets.yml` and requires a
published, versioned MCP image newer than `0.0.2`; do not use `0.0.2`, `latest`,
or an unpublished tag for this feature. Keep the existing image until a
compatible release is published. Caddy already forwards the MCP hostname, so
dataset paths do not need separate proxy rules.

Use separate protected version-1 files: `datasets-runtime.json` for MCP and
`datasets-provision.json` for the VM-two host provisioner. The runtime file has
only enabled dataset metadata, restricted per-dataset MCP ClickHouse
credentials, and scoped bearer credentials. The provisioning file has admin
connection details and managed-object settings/secrets; keep it root-owned,
mode `0600`, host-only, and never mount it into Compose. The MCP container gets
only the runtime file, mode `0600`, mounted read-only at
`/run/rawbbit/datasets.json`. Keep both files out of Git and do not print
rendered secret-bearing configuration.

The VM-two main target remains `analytics.events`; dataset databases must be
different from `analytics`. The provisioning contract carries the non-secret
`main_database: "analytics"` and `main_user: "rawbbit_mcp"` values. The
privileged preflight checks whether the main user has direct `SELECT`/`ALL`
access, or inherits it through nested roles, to any dataset that is disabled or
not explicitly exposed to `/mcp`, plus previously registered targets retained
in the ownership journal after their registry entries are removed. If it finds
access, it stops before provisioning and does not change the main user's
existing grants; narrow those grants manually, then rerun the preflight. Ansible
runs this read-only check for all-disabled registries and for an empty registry
when a prior ownership journal remains; a fresh empty installation needs no
dataset preflight.

`managed` mode creates missing databases and, when requested, the canonical
compatible events table; it reconciles only explicitly recorded restricted
identities. `reference` mode verifies an externally managed compatible table
and restricted credentials without changing ClickHouse schema or access
entities. Before creating or reconciling a table, managed mode checks a
pre-existing target against the canonical columns, types, engine, partition
key, and sorting key; it refuses incompatible schemas rather than replacing
them. The root-owned, mode-`0600` ownership journal is
`/srv/rawbbit-two/clickhouse/datasets-ownership.json`, outside the ClickHouse
data directory and MCP mounts. Back it up with ClickHouse state and protected
configuration. It also retains the validated database/table names of registered
datasets as main-access isolation targets, including reference datasets; these
targets are not ClickHouse object ownership claims and are not discarded when a
registry entry is removed. If it is lost, restore it or perform an explicit
reviewed ownership check; do not silently adopt same-named ClickHouse objects.
Managed adoption of an existing database requires an `Atomic` database engine
and non-nil stable ClickHouse UUIDs for the database and any existing table.
Reference mode can verify an externally managed non-Atomic database because it
does not claim ownership of or mutate those database objects. Its validated
database/table names are still retained in the journal for main-access
isolation checks.

Dataset endpoints and ClickHouse grants provide logical access separation, not
resource isolation: all datasets still share the same ClickHouse server's CPU,
memory, storage, administrator, and host failure domain.

An interrupted `create_*` intent can be retried only when the object is still
absent, or when ClickHouse identity matches an object ID already in the journal.
An object found by name without a recorded identity and any interrupted
non-creation mutation stop preflight for operator-reviewed recovery. Do not
delete or hand-edit the journal to bypass this check. Stop MCP, preserve the
ClickHouse state and journal, restore a trusted journal backup only after its
recorded IDs match, and rerun `--preflight-only` before provisioning. If there is
no trusted matching backup, keep MCP stopped until an explicit recovery review
establishes ownership and effective grants.

Provision and activate in this order:

1. Stage the protected files and validate the opt-in Compose render without
   printing it:

   ```bash
   docker compose -f docker-compose.yml -f docker-compose.datasets.yml config --quiet
   ```

2. Wait for ClickHouse health, then run the host provisioner from the
   deployment directory. The commands use this guide's default paths; adjust
   file paths if you changed them. Ansible installs both the provisioner and
   canonical schema under the root-owned `/srv/rawbbit-two/clickhouse/`
   directory. The provisioner accepts protected file paths, not secret values:

   ```bash
   cd /home/deploy/rawbbit-two
   dataset_args=(
     --config /srv/rawbbit-two/clickhouse/datasets-provision.json
     --runtime-config /home/deploy/rawbbit-two/datasets-runtime.json.stage
     --schema /srv/rawbbit-two/clickhouse/schema_analytics_events.sql
     --journal /srv/rawbbit-two/clickhouse/datasets-ownership.json
   )
   sudo /usr/bin/python3 /srv/rawbbit-two/clickhouse/provision_datasets.py "${dataset_args[@]}" --preflight-only
   sudo /usr/bin/python3 /srv/rawbbit-two/clickhouse/provision_datasets.py "${dataset_args[@]}"
   ```

   `managed` mode applies only journal-owned objects; `reference` mode only
   verifies them. The provisioner is a host command, not an MCP-container task.
   A no-op rerun reports `configuration unchanged` and avoids ClickHouse writes.
3. Only after success, atomically activate the runtime file and immediately
   recreate MCP before waiting for unrelated services:

   ```bash
   mv -f -- datasets-runtime.json.stage datasets-runtime.json
   docker compose -f docker-compose.yml -f docker-compose.datasets.yml up -d --force-recreate mcp-server
   ```

   On failure, leave the live runtime file untouched (or keep MCP stopped on a
   first install). Do not restart ClickHouse, dbt, or Metabase just to register
   a dataset.

Example paths are `/datasets/team_a/mcp` and `/datasets/team_b/mcp`. Main `/mcp`
users all see the same explicitly exposed dataset set; exposure is not
per-user. Each scoped path is fixed to one dataset and accepts only its own
bearer credentials. Scoped tokens do not authenticate `/mcp` or another
dataset's path. Give each endpoint a restricted ClickHouse identity as well as
its own bearer token.

Registering or provisioning a dataset does not itself ingest events. With dbt
routing disabled, dbt continues loading all apps into the configured default
`CLICKHOUSE_DATABASE.events` table (normally `analytics.events`). Optional
app-ID routing can direct selected apps to existing events tables; dataset MCP
enablement controls endpoint exposure, not ingestion. See [dbt app-ID routing](#dbt-app-id-routing).
Disabling/removing a registry entry and recreating MCP withdraws MCP access but
preserves the database, table, and data. It also leaves the provisioner-managed
MCP ClickHouse user, role, and grants in place; it does not automatically
revoke separately managed direct ClickHouse credentials either. Revoke either
kind of database identity explicitly through the operator-reviewed procedure
below. The journal retains the removed dataset's database/table target so the
main-user isolation check remains active. Rollback restores the prior protected
runtime config and published MCP image, not ClickHouse data or journal state.
Verify the old MCP token is rejected after a rotation/removal; back up before
making changes.

### Offboard database identities separately

1. Disable/remove the endpoint registration and successfully apply the Ansible
   change. Verify the old endpoint rejects initialization and prior MCP sessions
   no longer work. Do not proceed if the endpoint is still reachable.
2. For the provisioner-managed MCP identity, copy the exact `mcp_user` and
   `mcp_role` names from the protected registry/journal. In an authorized
   ClickHouse admin session, inspect all grants and role assignments before
   revocation. For a neutral `team_a` example:

   ```sql
   SELECT user_name, role_name, access_type, database, table, column,
          is_partial_revoke, grant_option
   FROM system.grants
   WHERE user_name = 'rawbbit_mcp_team_a'
      OR role_name = 'rawbbit_role_team_a';

   SELECT user_name, role_name, granted_role_name, granted_role_is_default,
          with_admin_option
   FROM system.role_grants
   WHERE user_name = 'rawbbit_mcp_team_a'
      OR role_name = 'rawbbit_role_team_a'
      OR granted_role_name = 'rawbbit_role_team_a';
   ```

   If the user has any direct grants, the role does not have exactly one
   whole-table `SELECT` grant on the target with no grant option, or the role
   has unexpected grantees/assignments, stop and review them individually.
   Otherwise revoke the table grant and role assignment, then remove only the
   dedicated MCP login and role:

   ```sql
   REVOKE SELECT ON `team_a`.`events` FROM `rawbbit_role_team_a`;
   REVOKE `rawbbit_role_team_a` FROM `rawbbit_mcp_team_a`;
   DROP USER `rawbbit_mcp_team_a`;
   DROP ROLE `rawbbit_role_team_a`;
   ```

   Verify the user/role and their grant rows are absent. Do not drop the dataset
   database/table or delete/edit the ownership journal; its retained target
   continues to protect the main endpoint's isolation check. Re-enabling a
   destructively offboarded managed identity requires an operator-reviewed
   journal recovery, not a routine Ansible rerun.
3. Separately managed direct ClickHouse/BI users are not owned by this
   provisioner. Have each credential owner identify and revoke its exact user
   and grants, then verify the effective grants are gone. Do not delete data or
   unrelated users/grants as part of endpoint offboarding.
<!-- dataset-mcp:end -->

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

<!-- dbt-routing:start -->
### dbt app-ID routing

Routing is disabled unless explicitly enabled. It uses the same one ingestion
model and the existing hourly/daily schedules; it does not create one model or
cron per app.

#### Live progress output

When an attempt launches dbt, callbacks report observed, allowlisted lifecycle
progress for `rawbbit_events_load` and the selected `not_null_event_id`,
`not_null_app_id`, and `not_null_event_time` tests on stderr. Lines include
trusted job/attempt IDs, attempt kind (`default`, `primary`, or `fallback`),
and destination database, for example:

```text
2026-10-05T12:00:02Z [dbt-progress job=<uuid> attempt=<uuid> kind=primary db=example_events] START test not_null_event_id
```

Attempt-level lines also show start, server-settlement verification, and the
terminal attempt outcome. An attempt that fails destination-readiness checks
may have only these attempt-level lines.

Scheduled output is available in `docker compose logs -f dbt-runner` and
Dozzle. Manual `dbt-job` output is shown in the invoking terminal; the existing
`RAWBBIT_DBT_MIRROR_PID1=1` option also mirrors both streams to the persistent
container's Docker log stream. No new logging setting is required. Progress on
stderr is informational, not a job-failure signal; stdout retains the final JSON
summary, and exit status plus the persistent records remain authoritative.
Relative ordering between stdout and stderr is not strict. Progress is
best-effort and may be dropped under backpressure. A successful model/test line
does not prove ClickHouse completed the write; server-settlement verification
still determines attempt confirmation. With `--fail-fast`, later selected
tests may never start and no skipped event is fabricated. See the [dbt progress
and diagnostic limits](../../dbt_project/README.md#progress-and-diagnostics)
for privacy and failure-classification details. Production image activation is
separately authorized and must use the existing [safe gate/lock/drain
procedure](#safe-first-upgrade-and-route-changes), not an uncoordinated runner
recreation.

#### Routing configuration

The runner reads:

```env
RAWBBIT_DBT_ROUTING_ENABLED=0
RAWBBIT_DBT_ROUTES_FILE=/app/runtime/dbt-routes.json
```

For Ansible deployments, configure `rawbbit_two_dbt_routing_enabled` and
`rawbbit_two_dbt_routes` in the ignored non-secret `main.yml`. A route's
`dataset_id` references `rawbbit_two_datasets[].id`; Ansible resolves the
database/table metadata even when that dataset's MCP `enabled` flag is false.
Keep referenced metadata while routing is configured. The MCP endpoint and its
credentials do not control ingestion. For Compose-only installs, maintain the
same version-1 JSON snapshot at `/srv/rawbbit-two/dbt/dbt-routes.json` on the
persistent runtime mount, set routing to `1`, and use `RAWBBIT_RAW_LOAD_MODE=dbt`.
Activate it only with the safe drain/gate procedure below. Example contract:

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

`app_id` must match the raw event value exactly. Enabled routing validates one
readable, supported snapshot before any writes; invalid/missing configuration,
unresolved dataset references, duplicate app IDs, unsafe identifiers, conflicting
targets, or a target colliding with the resolved default table exits `3` without
writes. A valid but missing, inaccessible, or incompatible physical destination
is a per-route failure and falls back for that same app and window.
The runner freezes one validated snapshot under the shared lock per invocation;
all windows/chunks use that same revision.

For each window the runner first loads the default-table complement, excluding
all routed IDs; it then loads routes sequentially using both the exact raw app
path and exact SQL app-ID filter. A fallback uses that same include filter in
the default table, never the complement's exclusion filter. Known default,
route, and fallback failures continue to later routes and bounded windows. If
ClickHouse may still have unknown query or mutation work, attempt tagging and
checks of ClickHouse process/mutation state are used before classification;
cancelling the client alone is not proof of termination.
An unresolved outcome writes a durable fence and stops later ingestion writes
until operator recovery. This safety exception overrides continuing to later
routes. Do not remove the fence to force a retry; an operator must verify the
tagged ClickHouse work is terminal and reconcile possible partial output first.

Each attempt, outcome, and route revision is persisted on `/srv/rawbbit-two/dbt`,
mounted at `/app/runtime`. The runner's atomic, mode-`0600` JSON ledger is:

| Runtime path | Contents |
| --- | --- |
| `jobs/<job_id>.json` | Invocation status, route revision, windows, and attempt outcomes. |
| `attempts/<attempt_id>.json` | Intended/actual destination, window, revision, and write status. |
| `requests/<attempt_id>/<query_id>.json` | ClickHouse request dispatch/confirmation; preflight uses `requests/preflight-<job_id>/`. |
| `write-fence.json` | Durable fence when ClickHouse work cannot be proved terminal. |

Exit codes are `0` completed, `2` completed with one or more fallbacks, `1`
one or more app-windows not confirmed loaded, `3` invalid enabled config with
no writes, and `4` deferred/skipped or unconfirmed invocation. The persisted
statuses `gated`, `lock_skipped`, `lock_wait_expired`, and `mode_disabled` are
non-success skips and do not themselves create a fence. An unconfirmed
ClickHouse server outcome writes `write-fence.json`; no later job may write
until an operator verifies tagged work is terminal and reconciles possible
partial output. Use the ledgers and fence together; exit code `4` alone does
not distinguish a skip from unresolved work or prove ingestion completed.
Treat `2` as degraded and inspect per-destination outcomes; investigate `1`,
unknown attempts, or a fence before replay. There is no automatic retry queue.

Runner timeout variables accept positive integer seconds. Defaults and maximums
are: `RAWBBIT_DBT_ATTEMPT_TIMEOUT_SECONDS=900` (7200),
`RAWBBIT_DBT_JOB_TIMEOUT_SECONDS=21600` (172800),
`RAWBBIT_DBT_CONTROL_TIMEOUT_SECONDS=10` (60),
`RAWBBIT_DBT_SETTLE_TIMEOUT_SECONDS=30` (300), and
`RAWBBIT_DBT_LOCK_WAIT_SECONDS=300` (7200). The lock-wait setting applies to
manual backfills; scheduled jobs skip immediately. The checked-in Compose and
Ansible service environment does not pass these optional overrides, so current
deployments use the defaults; putting them in `.env` alone does not pass them
through. Ansible's separate `rawbbit_two_dbt_drain_timeout_seconds` defaults to
600 seconds for the deployment-lock and shared pipeline-lock waits.

Fallback rows are visible in the main/default dataset, which is an accepted
availability tradeoff. Per-table `(app_id, event_id)` replacement does not
provide cross-database uniqueness: partial writes and fallback can leave copies
in both databases, and a later successful route does not remove the default
copy. Removing the last route returns that app to default-table loading on later
windows/backfills; existing routed or fallback rows remain. No automatic
cross-database delete, move, cleanup, or `full_refresh` is performed. Before
replaying a bounded UTC range with the backfill command above, compare the
recorded route revision with the active snapshot and explicitly
review any mapping change. Replay before raw Parquet expires; retain/back up the
outcome and revision records long enough to investigate and replay.

The Compose and Ansible role image defaults remain
`ghcr.io/mirlan-irokez/rawbbit-dbt-runner:0.1.1`; it predates the drain and
routing contract, and the helper deliberately refuses it as a routing-capable
candidate. Select a tested local build or a tested, compatible released image
before enabling routing. For remote registry selections, do not substitute an
unpublished tag or `latest`.

The tracked Ansible example selects `rawbbit-dbt-runner:local`. With that exact
selection, each normal VM-two playbook run that reaches the build task copies
`dbt_project/` to the VM and invokes a build even if the tag exists. Docker layers
remain cacheable; the task reports `changed`, and unchanged inputs need not
produce a different image ID. This does not switch to GHCR or require an upload.
Copying source alone does not update a running container: the build retags the
candidate before the pipeline lock is acquired, but the live runner keeps its
current image until the safe cutover below. A failed build aborts before runner
activation; a drain timeout leaves the live runner/config unchanged and the
gate retained. Serialize deployments and local-tag builds; do not manually
recreate the runner while deployment is in progress. The local tag is mutable,
not a rollback guarantee; retain the previous image ID for a reviewed rollback.
The existing recursive copy does not guarantee removal of deleted source files
from the VM build context.

Keep the existing `rawbbit_dbt` identity. Before activation, an operator must verify its
effective grants against the pinned adapter's `delete_insert` behavior on the
deployed ClickHouse version and apply only the required operations scoped to
the default and route `events` tables. No broad grants are applied
automatically; routing does not create tables or grants. An MCP-disabled
dataset still needs a ready physical ClickHouse target and dbt-loader grants.

#### dbt observation access

The safety control plane also requires explicit observation grants, **even
with routing disabled**. The first-boot baseline script does not add these
grants, so review effective access for fresh as well as persistent installs.
In an authorized admin session, review and grant only these system tables to
the dedicated loader (replace the example identity if configured differently):

```sql
GRANT SELECT ON system.processes TO rawbbit_dbt;
GRANT SELECT ON system.mutations TO rawbbit_dbt;
GRANT SELECT ON system.query_log TO rawbbit_dbt;
GRANT SELECT ON system.tables TO rawbbit_dbt;
GRANT SELECT ON system.columns TO rawbbit_dbt;
```

No grants are applied automatically. Query logs contain privileged operational
metadata: the loader is a trusted central service, not a team/MCP identity.
Verify own tagged-query cancellation permissions on the installed ClickHouse
version; do not grant global cancellation merely to bypass a fence. Deployment
runs `dbt-job verify-control` while the gate is closed and before releasing it.
Missing observation access fails closed. The host deployment helper records a
verified `deploy-fence.json` before removing the gate. Request ownership is pinned to
`dbt-clickhouse==1.9.8` and `clickhouse-connect==1.6.0`; upgrading either requires
rerunning the disposable query-ID/cancellation tests.

#### Fresh VM-two installation

After [SSH/deploy-account setup](../ansible/README.md#manual-server-bootstrap),
[inventory/configuration and Vault setup](../ansible/README.md#configure-inventory-and-variables),
and the supported image selection described above, run on the controller from
`quickstart/ansible`:

```bash
ansible-playbook -i inventory.yml vm_two.yml --ask-vault-pass
```

This applies to a genuinely fresh install: no existing runner container,
retained deployment gate/write fence, restored ClickHouse/runtime state, or
prior ingestion needing upgrade/recovery. Leave both bootstrap/recovery flags
false. No old-runner launcher shim, drain, or stop is needed.

The standard command does not guarantee unattended completion in one run.
In dbt mode, missing [observation access](#dbt-observation-access) can make
deployment fail closed and retain its gate, even with routing disabled. An
authorized administrator reviews effective access and applies the existing
narrow grants as needed; Ansible does not apply them automatically.

If a failed first attempt leaves a gate or runner container, treat the next
attempt as recovery, not a fresh-state rerun. Inspect the failure, effective
grants, retained gate/fence, container state, and relevant records before
choosing acknowledgements. `rawbbit_two_dbt_recover_gate` is only for an inspected
retained gate. `rawbbit_two_dbt_bootstrap_stopped` is only needed when existing
runner containers are present and none is running, after verified stopped
state and server quiescence. Neither flag repairs grants or drains jobs; do not
blindly enable both, remove a gate/write fence, or force-stop active work to
retry. After successful deployment, use the [post-activation checks](#routine-deployment-and-post-activation-checks).

#### Safe first upgrade and route changes

The launcher-shim/drain/bootstrap procedure below upgrades an **existing
pre-routing runner**, not a fresh installation or a universal grant-recovery
procedure. A pre-routing runner does not recognize the gate. The helper deliberately
refuses a running ungated runner. Bootstrap it once in an exclusive operator
maintenance window; **do not force-stop a running load**:

1. Select the tested routing-capable image and review loader grants first.
   For the local selection, first run the ordinary VM-two Ansible playbook from
   the controller using the reviewed source to copy the build context and build
   the candidate. This staging attempt may refuse the still-running ungated
   runner, retaining `deploy.gate` and a private `.dbt-deploy-*` directory without
   publishing runner config. This host gate does not guard the pre-routing
   runner: it cannot read the gate. Do not install the old launcher's shim
   (step 3) or stop the runner until the candidate check below passes.
   Leave those artifacts in place. If the tag is
   absent or stale but the copied context matches the reviewed source, rebuild
   on the VM as the deployment account (adjust paths for non-default installs):

   ```bash
   cd /home/deploy/rawbbit-two
   test -f .dbt-build-context/runner/runner.py &&
   docker build --build-arg DBT_UID="$(id -u)" --build-arg DBT_GID="$(id -g)" \
     -t rawbbit-dbt-runner:local .dbt-build-context
   ```

   A build failure is not permission to stop the old runner or bypass a gate.
   Before installing the old launcher's guard or stopping any old runner,
   verify the prepared candidate's capabilities on the VM. For remote installs,
   explicitly pull the selected tested release first and substitute its image:

   ```bash
   docker run --rm --pull never --network none --entrypoint /app/bin/dbt-job \
     rawbbit-dbt-runner:local --capabilities
   ```

   Require the full helper contract: `version: 1`, `drain_aware: true`,
   `pipeline_lock: /app/runtime/pipeline.lock`,
   `deploy_gate: /app/runtime/deploy.gate`, `route_snapshot_version: 1`, and
   `validate-config` in `commands`. If any field is missing/incompatible, stop
   and investigate without stopping the old runner or bypassing a gate. Keep
   deployments/builds serialized so the checked candidate cannot change during
   the procedure; activation rechecks the contract and freezes the image ID.
2. Suspend manual submissions (including one-off `docker compose run`), and
   obtain each submitter's queued backfill windows for the recovery record.
   Direct `docker exec ... dbt build` bypasses the lock and is unsupported.
   All operators must honor maintenance; a Docker administrator can bypass any
   ingestion guard. If legacy host ingestion is enabled, remove its cron launch
   and rename its entrypoint before switching modes; let active work drain.
3. Deny new cron/manual `dbt-job` launches inside **every old runner container**.
   This changes the launcher, not any active dbt process or its open lock file.
   Run on the Linux VM as the deployment operator:

   ```bash
   ids=$(docker ps -q --filter label=com.docker.compose.project=rawbbit-two \
     --filter label=com.docker.compose.service=dbt-runner)
   for id in $ids; do
     docker exec --user 0 "$id" sh -c '
       test -e /app/bin/dbt-job.before-drain || cp -p /app/bin/dbt-job /app/bin/dbt-job.before-drain
       printf "#!/bin/sh\necho legacy-maintenance-deferred >&2\nexit 4\n" > /app/bin/dbt-job.drain
       chmod 755 /app/bin/dbt-job.drain
       mv /app/bin/dbt-job.drain /app/bin/dbt-job'
   done
   ```

4. Set `stage` to the retained validated candidate directory. Use its exact
   desired overlay list (omit dataset overlay only for an approved removal).
   Hold the **existing same inode** lock while checking server completion and
   stopping old containers. Queued old backfills may finish before the lock is
    obtained; those still waiting when it is held are stopped without writing and
    must be recorded for bounded manual replay. Do not delete/replace the lock.
    The helper's `--check-quiescence` mode is read-only: it checks configured
    loader queries and unfinished ClickHouse mutations, does not stop jobs or
    change server state, and requires the caller to keep the same pipeline lock
    held across the check and any subsequent old-container stop.

   ```bash
   cd /home/deploy/rawbbit-two
   stage=/home/deploy/rawbbit-two/.dbt-deploy-REPLACE_WITH_VALIDATED_CANDIDATE
   check=(python3 ./deploy-dbt-runner.py --install-dir "$PWD" --stage-dir "$stage"
     --runtime-dir /srv/rawbbit-two/dbt --compose-file docker-compose.yml
     --compose-file docker-compose.dozzle.yml --compose-file docker-compose.datasets.yml
     --check-quiescence)
   (
     flock -w 600 9 || exit 1
     "${check[@]}" || exit 1
      for id in $ids; do docker stop --timeout 30 "$id" || exit 1; done
     "${check[@]}" || exit 1
   ) 9>>/srv/rawbbit-two/dbt/pipeline.lock
   ```

   Stop occurs only after lock acquisition and verified server quiescence, not
   as a drain technique. A timeout leaves active work alive and the old guarded
   launcher in place; do not bypass the checks or continue publication. An
   abnormal prior termination, unaccounted one-off/manual process, or unavailable
   server proof requires operator investigation, not a bootstrap acknowledgement.
5. Verify old runners remain stopped, then run from `quickstart/ansible` on the
   controller:

   ```bash
   ansible-playbook -i inventory.yml vm_two.yml --ask-vault-pass \
     -e '{"rawbbit_upgrade_packages": false, "rawbbit_two_dbt_bootstrap_stopped": true, "rawbbit_two_dbt_recover_gate": true}'
   ```

   JSON extra-vars preserve actual booleans; `-e key=true` passes a string and
   fails the routing switch validation. These flags acknowledge the verified
   old-runner stop and inspected retained gate; they do not perform the drain.
   The helper reacquires the pipeline lock, rechecks all containers/server state,
   publishes validated config, recreates only the runner and verifies its image,
   revision, mount and overlays before reopening the gate. Remove the one-time
   flags after success. Preserve records for deferred/cancelled queued windows.
   If abandoning bootstrap, restore the backed-up launcher only after operator
   verification; do not clear an unknown-work fence to resume old ingestion.

For routing-capable runners, establish the gate before changing any
runner-observed environment, snapshot, image, or Compose configuration. New
scheduled/manual entries check the gate before acquiring and again while
holding the same shared host lock, `/srv/rawbbit-two/dbt/pipeline.lock`. The
deployment acquires that lock with a bounded wait and holds it throughout
publication and runner-only recreation. On timeout, abort without killing work
or replacing live state; leave the gate closed if
deployment state is uncertain. Release it only after verifying the runner,
route revision, and active overlays. Preserve every active Compose overlay
(including Dozzle, dataset/MCP, and any dbt-routing overlays) on all deployment
calls; recreate only `dbt-runner` for a route change. For Ansible installs, see the VM-two
[routing and deployment instructions](../ansible/README.md#vm-two-dbt-app-id-routing).
For Compose-only installs, follow the same maintenance/gate/lock protocol; do
not use an uncoordinated whole-stack `docker compose up` for routing changes.
Rolling back to a pre-routing image while routes are enabled requires the same
drain protocol and explicit approval of all-app default loading; otherwise keep
the gate closed and stop ingestion rather than silently changing destinations.
The two existing UTC schedules remain unchanged.

#### Routine deployment and post-activation checks

After a successful fresh deployment or first upgrade, use the ordinary VM-two Ansible deployment
command without the one-time bootstrap/recovery flags. Use a recovery
acknowledgement only after inspecting a retained gate and following the recovery
procedure; never remove a gate or write fence merely to retry.

Only after deployment succeeds, run on the VM from the installation directory:

```bash
docker compose exec -T dbt-runner /app/bin/dbt-job --capabilities
docker compose exec -T dbt-runner /app/bin/dbt-job validate-config
docker compose exec -T dbt-runner /app/bin/dbt-job verify-control
```

Require `drain_aware: true`, configuration status `valid` with the expected
snapshot/revision, and `control_access_verified`. These checks do not load data.
An optional bounded hourly load uses the unchanged lookback window:

```bash
docker compose exec -T dbt-runner /app/bin/dbt-job hourly
rc=$?
printf 'Exit code: %s\n' "$rc"
```

Inspect the reported windows, revision, destinations, and per-attempt outcomes
using the exit-code guidance above. Exit `0` can process an empty raw window;
it does not prove row placement. Using an authorized admin or dbt SQL connection,
check the routed table's row count and time range for the actual raw `app_id`
and the recorded UTC window. Compare default/fallback placement separately:
historical default rows are not automatically moved or deleted. Review deferred
backfills for bounded manual replay; do not clear a fence to force a retry.
<!-- dbt-routing:end -->

### Audit existing events before cutover

The dbt ingestion tests validate the complete configured default events table
(normally `analytics.events`), not only the current load window. Audit data
written by the legacy loader before enabling dbt ingestion:

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

This procedure covers the legacy-to-dbt ownership change with app-ID routing
disabled. It is not a routing activation or runner-image upgrade procedure; for
those changes, follow [dbt app-ID routing](#dbt-app-id-routing) and its drain
requirements instead of using the uncoordinated runner recreation below.

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

This rollback sequence assumes routing is disabled. If routing was active, do
not switch to a pre-routing runner or legacy all-app loader until the same drain
protocol is completed and all-app default loading is explicitly approved;
otherwise keep ingestion gated. Existing routed/fallback rows are not moved or
deleted by this rollback.

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
