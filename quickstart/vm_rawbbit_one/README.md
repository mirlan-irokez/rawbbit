# Rawbbit One-VM Quickstart

Status: production-oriented single-VM quickstart  
Audience: operator deploying Rawbbit ingestion with local SeaweedFS object storage  
Scope: Docker Compose on one Ubuntu 22.04 or 24.04 VM

This guide is provider-neutral. It assumes a fresh Linux VM with a public IP,
DNS control, and SSH access from a workstation.

This quickstart runs:

- Caddy
- NATS JetStream
- collector-api
- raw-writer
- SeaweedFS master
- SeaweedFS volume server
- SeaweedFS filer
- SeaweedFS S3 gateway

It is intentionally a single-VM design.

## Architecture

```text
SDKs / apps
  -> Caddy :443
  -> collector-api :8080
  -> NATS JetStream :4222
  -> raw-writer
  -> SeaweedFS S3 :8333
  -> SeaweedFS filer :8888
  -> SeaweedFS master :9333
  -> SeaweedFS volume :8080
  -> VM disk
```

Caddy is the public endpoint. It terminates TLS on ports 80/443 and proxies to
the internal containers. Public traffic should enter through Caddy only:

```text
collector.yourdomain.com -> collector-api:8080
s3.yourdomain.com        -> seaweed-s3:8333
```

Internal Rawbbit services use Docker DNS:

```text
nats://nats:4222
http://seaweed-s3:8333
```

## Files

```text
quickstart/vm_rawbbit_one/
  README.md
  docker-compose.yml
  .env.example
  Caddyfile
  nats-server.conf
  bootstrap-host-dirs.sh
  seaweedfs/
    filer.toml
    s3.json.example
  geoip/
    README.md
```

## 1. Initial VM sizing

For initial setup, use approximately:

- 2 vCPU
- 4 GB RAM
- 40–80 GB SSD
- static public IPv4
- system time configured for UTC
- Ubuntu 24.04 LTS preferred, Ubuntu 22.04 also supported

Disk is the main constraint because the VM stores:

- JetStream data
- SeaweedFS objects and metadata
- Docker images and logs
- Caddy certificate state

The quickstart limits JetStream file storage to 3 GB and reserves 5 GB of free
space on the SeaweedFS volume path. These are operational guardrails, not a
capacity guarantee.

## 2. Configure DNS

Create two DNS `A` records pointing to the VM:

```text
collector.yourdomain.com -> VM_PUBLIC_IP
s3.yourdomain.com        -> VM_PUBLIC_IP
```

The first hostname exposes event ingestion. The second exposes SeaweedFS S3
through Caddy for external S3 clients such as ClickHouse and operator tools.

**Workstation:** confirm both records resolve before starting Caddy:

```bash
dig +short collector.yourdomain.com
dig +short s3.yourdomain.com
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
  awscli

timedatectl set-timezone UTC
```

Package roles:

| Package | Requirement | Purpose |
|---|---|---|
| `ca-certificates` | required | HTTPS downloads and registry connections |
| `curl` | required | Docker installation and endpoint checks |
| `gnupg` | required | Configure the Docker apt repository |
| `ufw` | recommended | Host firewall |
| `openssl` | recommended | Generate API keys, salts, and S3 credentials |
| `htop` | optional | CPU and memory monitoring |
| `jq` | recommended | Inspect JSON operational output |
| `nano` | optional | Edit environment and configuration files |
| `dnsutils` | recommended | Provides `dig` for DNS verification |
| `rsync` | recommended | Copy the quickstart from a workstation checkout |
| `awscli` | recommended | Create and test the S3 bucket |

The host does not initially need:

- Python or pip
- Node.js or Java
- NATS CLI or server binaries
- SeaweedFS binaries
- a host-level Caddy package
- ClickHouse

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
ssh-keygen -t ed25519 -C "rawbbit-vm" -f ~/.ssh/rawbbit_vm
```

Install it for root initially:

```bash
ssh-copy-id -i ~/.ssh/rawbbit_vm.pub root@VM_PUBLIC_IP
```

Test key-based access before changing SSH authentication settings:

```bash
ssh -i ~/.ssh/rawbbit_vm root@VM_PUBLIC_IP
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
ssh -i ~/.ssh/rawbbit_vm deploy@VM_PUBLIC_IP
```

Keep the existing root session open until this succeeds. Do not disable the
fallback login path while testing.

## 6. Configure firewalls

Allow inbound:

- TCP 22, preferably restricted to your administrative IP
- TCP 80, public
- TCP 443, public

Do not expose these container or internal service ports publicly:

- `4222`: NATS client port
- `8222`: NATS monitoring
- `8080`: collector container
- `8333`: SeaweedFS S3 gateway directly
- `8888`: SeaweedFS filer
- `9333`: SeaweedFS master

The S3 gateway is exposed externally through Caddy on port 443, not by opening
port 8333 directly.

UFW changes require root privileges. During bootstrap, run them as root. Later,
`deploy` may inspect or manage UFW with `sudo`.

**Root:** allow SSH from your administrative public IP before enabling the
firewall. Replace `YOUR_ADMIN_PUBLIC_IP` with the workstation's public IP:

```bash
ufw allow from YOUR_ADMIN_PUBLIC_IP to any port 22 proto tcp
```

If you cannot use a stable source IP, `ufw allow OpenSSH` is a broader fallback.
Keep the provider firewall and SSH-key authentication enabled as additional
controls.

```bash
ufw allow OpenSSH
```

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

1. Keep your current session open.

2. From another terminal, confirm:

```bash
ssh -i ~/.ssh/rawbbit_vm deploy@VM_PUBLIC_IP
```

Also confirm deploy can use sudo:

```bash
sudo -v
```

Do not continue if either test fails.

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

4. Check configuration snippets.

Ubuntu may also have configuration files under:

```bash
/etc/ssh/sshd_config.d/
```

Inspect effective settings:

```bash
sudo sshd -T | grep -E 'passwordauthentication|pubkeyauthentication|permitrootlogin'
```

Expected result:

- `passwordauthentication no`
- `pubkeyauthentication yes`
- `permitrootlogin prohibit-password`

If a file under `/etc/ssh/sshd_config.d/` overrides your settings, update or
remove the conflicting declaration deliberately.

5. Validate before applying:

```bash
sudo sshd -t
```

No output means validation passed. Do not reload SSH if this command reports an
error.

6. Reload SSH:

```bash
sudo systemctl reload ssh
sudo systemctl status ssh --no-pager
```

7. Test a new connection.

Keep the existing session open. From your workstation, open a new terminal:

```bash
ssh -i ~/.ssh/rawbbit_vm deploy@VM_PUBLIC_IP
```

Also test root key access while it remains allowed:

```bash
ssh -i ~/.ssh/rawbbit_vm root@VM_PUBLIC_IP
```

Only close the original session after the new deploy login succeeds.

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

**Workstation:** from the repository root, create the destination directories on
the VM first:

```bash
ssh -i ~/.ssh/rawbbit_vm deploy@VM_PUBLIC_IP 'mkdir -p /home/deploy/rawbbit-one/geoip /home/deploy/rawbbit-one/seaweedfs'
```
Rename files .env.example to .env, seaweedfs/s3.json.example -> seaweedfs/s3.json
Then copy the explicit public quickstart file set with `rsync`:

```bash
rsync -av \
  -e "ssh -i ~/.ssh/rawbbit_vm" \
  quickstart/vm_rawbbit_one/ \
  deploy@VM_PUBLIC_IP:/home/deploy/rawbbit-one/
```

If you prefer individual transfers, use the same key placeholder and copy only
tracked files:

```bash
scp -i ~/.ssh/vm_rawbbit_one quickstart/vm_rawbbit_one/.env.example deploy@VM_PUBLIC_IP:/home/deploy/rawbbit-one/.env
scp -i ~/.ssh/vm_rawbbit_one quickstart/vm_rawbbit_one/bootstrap-host-dirs.sh deploy@VM_PUBLIC_IP:/home/deploy/rawbbit-one/bootstrap-host-dirs.sh
scp -i ~/.ssh/vm_rawbbit_one quickstart/vm_rawbbit_one/docker-compose.yml deploy@VM_PUBLIC_IP:/home/deploy/rawbbit-one/docker-compose.yml
scp -i ~/.ssh/vm_rawbbit_one quickstart/vm_rawbbit_one/Caddyfile deploy@VM_PUBLIC_IP:/home/deploy/rawbbit-one/Caddyfile
scp -i ~/.ssh/demo_rawbbit_one quickstart/vm_rawbbit_one/nats-server.conf deploy@VM_PUBLIC_IP:/home/deploy/rawbbit-one/nats-server.conf
scp -i ~/.ssh/vm_rawbbit_one quickstart/vm_rawbbit_one/geoip/dbip-country-lite.mmdb deploy@VM_PUBLIC_IP:/home/deploy/rawbbit-one/geoip/dbip-country-lite.mmdb
scp -i ~/.ssh/vm_rawbbit_one quickstart/vm_rawbbit_one/seaweedfs/s3.json.example deploy@VM_PUBLIC_IP:/home/deploy/rawbbit-one/seaweedfs/s3.json 
scp -i ~/.ssh/vm_rawbbit_one quickstart/vm_rawbbit_one/seaweedfs/filer.toml deploy@VM_PUBLIC_IP:/home/deploy/rawbbit-one/seaweedfs/filer.toml 
```

**Deploy:** enter the copied directory:

```bash
cd /home/deploy/rawbbit-one
```

## 9. Create persistent host directories

The script needs root privileges because it creates directories under `/srv`.

**Deploy:** from the quickstart directory, run:

```bash
sudo ./bootstrap-host-dirs.sh
```

It creates:

```text
/srv/rawbbit-one/nats/jetstream
/srv/rawbbit-one/seaweedfs/master
/srv/rawbbit-one/seaweedfs/volume
/srv/rawbbit-one/seaweedfs/filerldb2
/srv/rawbbit-one/caddy/data
/srv/rawbbit-one/caddy/config
```

These directories hold persistent state. Do not treat them as disposable
container data.

Manual equivalent:

```bash
sudo install -d -m 0755 /srv/rawbbit-one/nats/jetstream
sudo install -d -m 0755 /srv/rawbbit-one/seaweedfs/master
sudo install -d -m 0755 /srv/rawbbit-one/seaweedfs/volume
sudo install -d -m 0755 /srv/rawbbit-one/seaweedfs/filerldb2
sudo install -d -m 0755 /srv/rawbbit-one/caddy/data
sudo install -d -m 0755 /srv/rawbbit-one/caddy/config
```

## 10. Generate deployment secrets

**Deploy:** generate independent random values. The commands print values to
the terminal; place them directly into your private configuration or password
manager rather than leaving them in shared notes.

```bash
openssl rand -hex 32  # collector API key
openssl rand -hex 32  # IP hash salt
openssl rand -hex 24  # writer access key
openssl rand -hex 32  # writer secret key
openssl rand -hex 24  # reader access key
openssl rand -hex 32  # reader secret key
openssl rand -hex 24  # admin access key
openssl rand -hex 32  # admin secret key
```

Do not place secrets in Git, shared shell history, logs, or chat messages.

## 11. Configure `.env`

**Deploy:** from the quickstart directory:

```bash
cp .env.example .env
chmod 600 .env
nano .env
```

At minimum, replace these values:

```env
COLLECTOR_PUBLIC_HOSTNAME=collector.yourdomain.com
S3_PUBLIC_HOSTNAME=s3.yourdomain.com

COLLECTOR_API_KEYS_JSON={"YOUR_API_KEY":"your.app.id"}
IP_HASH_SALT=YOUR_RANDOM_SALT

S3_BUCKET=rawbbit_raw
S3_ACCESS_KEY=YOUR_WRITER_ACCESS_KEY
S3_SECRET_KEY=YOUR_WRITER_SECRET_KEY
RAW_STORAGE_BACKEND=s3
```

Use the public Caddy hostname for external S3 access:

```env
S3_ENDPOINT_URL=https://s3.yourdomain.com
S3_USE_SSL=1
S3_VERIFY_SSL=1
```

This is the public endpoint used by Caddy. Do not pair the internal Docker DNS
name with the TLS flags above.

**SeaweedFS Volume Sizing defaults:**

```env
SEAWEED_VOLUME_SIZE_LIMIT_MB=5000
SEAWEED_VOLUME_MAX=0
SEAWEED_VOLUME_MIN_FREE_SPACE_GB=5
```

`SEAWEED_VOLUME_SIZE_LIMIT_MB` is the approximate max size of each internal
SeaweedFS volume file. It is not a total disk quota.

`SEAWEED_VOLUME_MAX=0` lets SeaweedFS auto-calculate how many volumes the
volume server can host based on free disk space and volume size.

`SEAWEED_VOLUME_MIN_FREE_SPACE_GB=5` reserves disk headroom. This matters
because NATS, Docker logs, Caddy certificate state, OS services, and SeaweedFS
maintenance all need disk space too.

For very small disks, consider lowering the volume size:

```env
SEAWEED_VOLUME_SIZE_LIMIT_MB=1000
```

Increasing this later mostly affects new volume allocation. Existing stored
objects do not need to be rewritten just because this value changes.

Keep these quickstart-specific defaults unless you have deliberately reviewed
the operational consequences of changing them:

```env
NATS_STREAM_MAX_AGE_SECONDS=86400
NATS_ACK_WAIT_SECONDS=300
RAW_FLUSH_INTERVAL_SECONDS=60

S3_PREFIX=raw
S3_REGION=us-east-1
S3_FORCE_PATH_STYLE=1

GEOIP_ENABLED=0
```

For browser producers, configure exact CORS origins:

```env
CORS_ALLOW_ORIGINS=https://www.example.com,https://app.example.com
```

Do not add trailing slashes to CORS origins.

## 12. Configure SeaweedFS credentials

**Deploy:** create the private SeaweedFS S3 credential file:

```bash
cp seaweedfs/s3.json.example seaweedfs/s3.json
chmod 600 seaweedfs/s3.json
nano seaweedfs/s3.json
```

Replace every `change_me` placeholder. Keep separate credentials for:

- writer: `Read`, `List`, `Write`
- reader: `Read`, `List`
- administrator: `Admin`, `Read`, `List`, `Tagging`, `Write`

Use the administrator credential to create the bucket after the stack has
started, as shown in [Verification](#verification). Use the non-admin writer
credential for raw-writer and for routine object uploads.

The `rawbbit-writer` credentials in `seaweedfs/s3.json` must exactly match the
writer values in `.env`:

```env
S3_ACCESS_KEY=...
S3_SECRET_KEY=...
```

Never commit `.env` or `seaweedfs/s3.json`.

## 13. Set NATS JetStream limits

`nats-server.conf` sets:

```conf
jetstream {
  store_dir: "/data/jetstream"
  max_file_store: "3G"
  max_memory_store: "512M"
}
```

`max_file_store: "3G"` is a backlog cap, not infinite buffering. If raw-writer
is stopped for too long or SeaweedFS is unavailable, JetStream can fill its
allowed storage.

New streams use 24-hour retention by default:

```env
NATS_STREAM_MAX_AGE_SECONDS=86400
```

Inspect the `EVENTS` stream after first startup using the command in
[Verification](#verification), and confirm that its reported maximum age is
`24h0m0s`.

## 14. GeoIP

GeoIP is disabled by default:

```env
GEOIP_ENABLED=0
```

If you want country codes, see [`geoip/README.md`](geoip/README.md) to supply
the database and enable it.

## 15. Validate before launching

**Deploy:** render and validate the Compose configuration:

```bash
docker compose config
```

Review the rendered output carefully, especially:

- public hostnames
- API-key-to-`app_id` mapping
- S3 bucket name
- public Caddy endpoint
- persistent bind-mount paths
- pinned container image tags

**Deploy or workstation:** confirm DNS once more:

```bash
dig +short collector.yourdomain.com
dig +short s3.yourdomain.com
```

At this point, the VM is prepared for deployment.

## 16. Start the Rawbbit one-VM stack

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
volume deletion if the file is changed later.

## Verification

Collector health through Caddy:

```bash
curl -i https://collector.yourdomain.com/healthz
```

S3 endpoint through Caddy:

```bash
curl -i https://s3.yourdomain.com/
```

AWS CLI test with the admin credential:

```bash
export AWS_ACCESS_KEY_ID=replace_with_admin_access_key
export AWS_SECRET_ACCESS_KEY=replace_with_admin_secret_key

aws s3 ls --endpoint-url https://s3.yourdomain.com
aws s3 mb s3://rawbbit_raw --endpoint-url https://s3.yourdomain.com
echo hello > test.txt
aws s3 cp test.txt s3://rawbbit_raw/test.txt --endpoint-url https://s3.yourdomain.com
aws s3 cp s3://rawbbit_raw/test.txt ./downloaded.txt --endpoint-url https://s3.yourdomain.com
cat downloaded.txt
rm -f test.txt downloaded.txt
unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY
```

Inspect the NATS stream after the stack has created its Docker network and the
collector has created the stream:

```bash
docker run --rm --network rawbbit-one_default natsio/nats-box:latest \
  nats --server nats://nats:4222 stream info EVENTS
```

Confirm the reported maximum age is `24h0m0s`.

## Logs

All services write to stdout/stderr. Docker rotates logs with:

```yaml
logging:
  driver: json-file
  options:
    max-size: "10m"
    max-file: "5"
```

Caddy access/runtime logs also stay in Docker logs by default. This keeps log
rotation consistent across the stack.

## Resource Limits

This quickstart does not set Docker CPU or RAM limits. Start without them, watch
real VM behavior, then add limits later only if a specific service becomes a
pressure point.

Watch disk first:

```bash
df -h
docker system df
docker compose logs --tail=100
```

The main storage controls are:

- VM disk size
- SeaweedFS `SEAWEED_VOLUME_MIN_FREE_SPACE_GB`
- NATS `max_file_store`
- Docker log rotation

## Security Notes

- Do not commit `.env`.
- Do not commit `seaweedfs/s3.json`.
- Replace all `change_me` values.
- Use non-admin S3 credentials for raw-writer.
- Keep NATS and SeaweedFS internal unless there is a specific reason to expose
  them through Caddy.
- Caddy certificate state is persisted under `/srv/rawbbit-one/caddy`.
