# Metabase in the DataQuery stack

DataQuery’s current responsibility ends at durable raw event landing: events are collected, buffered through NATS JetStream, and written as partitioned Parquet files to object storage.
Metabase is not part of that ingestion runtime. It is an optional self-hosted analytics layer you can place downstream once DataQuery data is available in a warehouse or other query surface.

Use this guide if you want to run an open source Metabase instance on your own VM for exploring DataQuery data, building internal dashboards, and giving yourself a lightweight BI interface without depending on a managed SaaS setup.

In other words:
```text
DataQuery raw layer -> warehouse / query layer -> Metabase
```
This guide covers a simple production-minded single-VM deployment of Metabase OSS with PostgreSQL and Caddy.

## 1) VM setup

- **OS:** Ubuntu 24.04
- **Recommended minimum:**
  - 2 vCPU
  - 4 GB RAM
  - 40+ GB SSD
- **Better if usage is non-trivial:**
  - 4 vCPU
  - 8 GB RAM

Add SSH key during VM creation.

On your computer, if you don’t already have a key:

```bash
ssh-keygen -t ed25519 -C "your-email@example.com"
```

This gives:

- private key: `~/.ssh/id_ed25519`
- public key: `~/.ssh/id_ed25519.pub`

Upload the public key to the VM provider.

Connect by SSH:

```bash
ssh root@YOUR_VM_IP
```

If provider gives a non-root user, use that instead.

---

## 2) Initial server hardening

Create a normal admin user:

```bash
adduser deploy
usermod -aG sudo deploy
mkdir -p /home/deploy/.ssh
cp /root/.ssh/authorized_keys /home/deploy/.ssh/
chown -R deploy:deploy /home/deploy/.ssh
chmod 700 /home/deploy/.ssh
chmod 600 /home/deploy/.ssh/authorized_keys
```

Then test:

```bash
ssh deploy@YOUR_VM_IP
```

Basic packages update:

```bash
sudo apt update && sudo apt upgrade -y
```

Install basic networking, firewall, SSH protection, HTTPS trust certs, and key-management tools:

```bash
sudo apt install -y curl ufw fail2ban ca-certificates gnupg
```

SSH security:

Edit:

```bash
sudo nano /etc/ssh/sshd_config
```

Recommended minimum:

```text
PasswordAuthentication no
PubkeyAuthentication yes
```

Then:

```bash
sudo systemctl restart ssh
```

> Important: do this only after confirming your non-root SSH login works.

---

## 3) Firewall

Open only what you need:

```bash
sudo ufw allow 22/tcp
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw enable
sudo ufw status
```

Keep these closed publicly:

- `5432` Postgres
- `3000` Metabase

Those should be reachable only inside Docker network, not from internet.

---

## 4) Install Docker and Compose

Remove a few possible old Docker packages:

```bash
sudo apt remove -y docker docker-engine docker.io containerd runc || true
```

Add apt folder:

```bash
sudo install -m 0755 -d /etc/apt/keyrings
```

Download Docker’s GPG key and save it in apt format:

```bash
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
```

Make the key file readable by all users/processes that need it:

```bash
sudo chmod a+r /etc/apt/keyrings/docker.gpg
```

Add Docker’s official Ubuntu package repository for the CPU architecture and current Ubuntu release, and verify it using this Docker signing key:

```bash
echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
  https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo $VERSION_CODENAME) stable" | \
  sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
```

Refresh apt package list and install Docker packages:

```bash
sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo usermod -aG docker $USER
newgrp docker
```

Test:

```bash
docker --version
docker compose version
```

Enable Docker on boot:

```bash
sudo systemctl enable docker
sudo systemctl start docker
```

---

## 5) Folder layout on VM

As user `deploy`:

```bash
mkdir -p ~/metabase-stack
cd ~/metabase-stack
mkdir -p caddy data backups
touch .env
```

---

## 6) Domain and DNS

Create an A record:

```text
metabase.yourdomain.com -> YOUR_VM_IP
```

Wait for DNS to propagate.

---

## 7) Production-ready `docker-compose.yml`

Create `~/metabase-stack/docker-compose.yml`:

```yaml
services:
  postgres:
    image: postgres:18
    container_name: metabase-postgres
    restart: unless-stopped
    environment:
      POSTGRES_DB: ${POSTGRES_DB}
      POSTGRES_USER: ${POSTGRES_USER}
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
    volumes:
      - metabase_postgres_data:/var/lib/postgresql
    networks:
      - internal
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U ${POSTGRES_USER} -d ${POSTGRES_DB}"]
      interval: 10s
      timeout: 5s
      retries: 10
    security_opt:
      - no-new-privileges:true

  metabase:
    image: metabase/metabase:latest
    container_name: metabase
    restart: unless-stopped
    depends_on:
      postgres:
        condition: service_healthy
    environment:
      MB_DB_TYPE: postgres
      MB_DB_DBNAME: ${POSTGRES_DB}
      MB_DB_PORT: 5432
      MB_DB_HOST: postgres
      MB_DB_USER: ${POSTGRES_USER}
      MB_DB_PASS: ${POSTGRES_PASSWORD}
      JAVA_TIMEZONE: Europe/Helsinki
      MB_SITE_URL: https://${METABASE_DOMAIN}
    networks:
      - internal
      - web
    security_opt:
      - no-new-privileges:true

  caddy:
    image: caddy:2
    container_name: metabase-caddy
    restart: unless-stopped
    depends_on:
      - metabase
    ports:
      - "80:80"
      - "443:443"
    volumes:
      - ./caddy/Caddyfile:/etc/caddy/Caddyfile:ro
      - caddy_data:/data
      - caddy_config:/config
    networks:
      - web
    security_opt:
      - no-new-privileges:true

volumes:
  metabase_postgres_data:
  caddy_data:
  caddy_config:

networks:
  internal:
    internal: true
  web:
```

---

## 8) `.env` file

Create `~/metabase-stack/.env`:

```dotenv
POSTGRES_DB=metabase
POSTGRES_USER=metabase
POSTGRES_PASSWORD=CHANGE_THIS_TO_A_LONG_RANDOM_PASSWORD
METABASE_DOMAIN=metabase.yourdomain.com
```

Use a strong random password.

---

## 9) Caddy setup

Create `~/metabase-stack/caddy/Caddyfile`:

```caddyfile
metabase.yourdomain.com {
    encode gzip zstd

    reverse_proxy metabase:3000

    header {
        Strict-Transport-Security "max-age=31536000; includeSubDomains; preload"
        X-Content-Type-Options "nosniff"
        X-Frame-Options "SAMEORIGIN"
        Referrer-Policy "strict-origin-when-cross-origin"
    }
}
```

Replace with your real domain.

Caddy will automatically get HTTPS certificates if:

- DNS points correctly
- ports `80/443` are open

---

## 10) Start the stack

```bash
cd ~/metabase-stack
docker compose up -d
docker compose ps
docker compose logs -f
```

Visit:

```text
https://metabase.yourdomain.com
```

---

To move Metabase to another VM in the future, dump the PostgreSQL application database and restore it on the new VM.

## 11) How to dump the current PostgreSQL

You run the dump command from your computer or any machine that can connect to the current Postgres and has `pg_dump` installed.

Install PostgreSQL client locally if needed.

Mac:

```bash
brew install libpq
brew link --force libpq
```

Ubuntu:

```bash
sudo apt install -y postgresql-client
```

Create the dump:

```bash
pg_dump -Fc \
  -h YOUR_DB_HOST \
  -p 5432 \
  -U YOUR_DB_USER \
  -d YOUR_DB_NAME \
  -f metabase.dump
```

It will ask password unless you use `PGPASSWORD`.

This dump contains Metabase application data such as:

- users
- questions
- dashboards
- settings

---

## 12) Copy dump to the VM

From your laptop:

```bash
scp metabase.dump deploy@YOUR_VM_IP:/home/deploy/metabase-stack/
```

So now the file is on the VM.

---

## 13) Restore the dump into Docker Postgres

First start only Postgres:

```bash
cd ~/metabase-stack
docker compose up -d postgres
```

Copy dump into the Postgres container:

```bash
docker cp metabase.dump metabase-postgres:/tmp/metabase.dump
```

Restore:

```bash
docker exec -it metabase-postgres bash -lc 'export PGPASSWORD="$POSTGRES_PASSWORD"; pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists --no-owner --no-acl /tmp/metabase.dump'
```

Then start Metabase + Caddy:

```bash
docker compose up -d metabase caddy
```

---

## 14) Minimum backup setup

Manual backup command:

```bash
docker exec -t metabase-postgres bash -lc 'export PGPASSWORD="$POSTGRES_PASSWORD"; pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > ~/metabase-stack/backups/metabase-$(date +%F-%H%M).dump
```

Keep backups off the VM too.

Best practice:

- local VM backup
- plus remote object storage backup

Because if the VM disk dies, local backups die with it.

---

## 15) Security minimum checklist

Do this at minimum:

- SSH keys only
- disable SSH password auth
- UFW open only `22/80/443`
- keep `3000` and `5432` private
- strong Postgres password
- run HTTPS through Caddy
- enable automatic security updates if you want:

```bash
sudo apt install -y unattended-upgrades
sudo dpkg-reconfigure -plow unattended-upgrades
```

Optional:

- fail2ban
- provider snapshots
- monitoring/uptime check

---

## Metabase-specific notes

Metabase’s application DB is configured through environment variables and is read at startup. For production, Metabase recommends PostgreSQL for the application database.

Docs:

- https://www.metabase.com/docs/latest/installation-and-operation/configuring-application-database
- https://www.metabase.com/docs/latest/cloud/migrate/cloud-to-self-hosted
