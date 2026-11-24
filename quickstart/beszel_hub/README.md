# Beszel Hub Quickstart

This deploys the Beszel `0.21.2` hub on an owner-provided third VM. It is an
independent Compose project: the Rawbbit VM playbooks deploy only optional
Beszel agents and never manage this hub.

## Requirements and network

- A Linux VM with rootful Docker Engine and the Docker Compose v2 plugin.
- A dedicated operator account on this separate hub VM. The examples assume
  `deploy` can run Docker Compose; grant it Docker-group access or use `sudo`.
  Either Docker access path is root-equivalent, so do not grant it to
  untrusted users and avoid direct root login.
- A DNS `A` record for the hub name pointing to the VM. Add an `AAAA` record only
  when IPv6 routing and the firewall are ready.
- Inbound TCP `80` and `443` to Caddy. Restrict SSH to the operator's trusted
  address/VPN; do not expose Docker's API.
- Outbound HTTPS for Caddy's ACME certificate issuance/renewal and agent
  WebSocket connections. Agents initiate outbound HTTPS; no agent listener or
  additional inbound port is needed.

Open only the ports required on the provider firewall and host firewall. Keep
the Beszel hub's internal port `8090` unpublished. Caddy is the only service
that publishes ports.

## Configure and start

On the VM, copy this directory from the repository and set the two matching
hostname values:

```bash
cp .env.example .env
chmod 600 .env
editor .env
```

Run these commands as the trusted operator account with Docker access (the
commands do not require a root shell when using the Docker group). Docker group
membership is equivalent to root and permits access to every container and
host bind mount. The snippets assume that `deploy` has Docker-group access; if
using `sudo` instead, prefix each `docker compose` command accordingly.

For example:

```dotenv
BESZEL_DOMAIN=beszel.example.com
BESZEL_APP_URL=https://beszel.example.com
```

`BESZEL_DOMAIN` is passed to the Caddy container and substituted into the
Caddyfile. `BESZEL_APP_URL` becomes the hub's `APP_URL`. They must refer to the
same public HTTPS origin, with valid DNS before startup.

Create persistent directories and validate the Compose configuration:

```bash
mkdir -p beszel_data caddy/data caddy/config
docker compose --env-file .env config --quiet
docker compose --env-file .env up -d
docker compose ps
```

Caddy obtains and renews HTTPS certificates automatically. Startup ordering
does not prove that the hub is ready or that ACME succeeded. Check
`docker compose logs caddy beszel` and verify `https://beszel.example.com`
before configuring agents.

Create the first administrator interactively in the Beszel web UI and use a
unique strong password. Follow Beszel's Add System flow to obtain the hub public
key for `rawbbit_beszel_hub_key`. In **Settings > Tokens**, create the universal
agent token for `vault_rawbbit_beszel_token`. Enable the token and set it to
**Permanent** before copying it into Vault; keep it private and never put it in a
command line, Compose file, or non-secret Ansible variables.

## Configure the Rawbbit agents

On the Ansible workstation, set the hub URL and public key in
`quickstart/ansible/group_vars/all/main.yml`, then set the universal token in
`group_vars/all/vault.yml` and encrypt it. Opt in globally or per VM using the
variables and examples in the
[Ansible deployment guide](../ansible/README.md#optional-beszel-monitoring).
Deploy the hub and confirm HTTPS first, then run one VM playbook at a time.

After deployment, confirm both configured system names appear in the hub and
that host, container, and disk metrics arrive. Create a notification destination
in the Beszel UI and use its test-notification action before relying on alerts.
An initial SMART-device detection failure does not generate a SMART alert;
check agent logs and device/controller support instead.

## SMART device guidance and security

The agent image already includes `smartctl`, so the hosts need no extra
packages. On each bare-metal host, list the whole disks (`sda`, `nvme0n1`, and so on):

```bash
lsblk -d
```

Installing `smartmontools` on the host is optional. It only lets you run
`sudo smartctl --scan` there to double-check device names.

Configure only verified `/dev` paths in that VM's
`rawbbit_*_beszel_smart_devices` list. A single path maps to itself; an explicit
`/dev/source:/dev/target` mapping changes the path visible in the container.
Many cloud VMs expose virtual disks without SMART data. Support also varies by
controller, NVMe namespace, RAID layer, and device permissions. Beszel scans
devices in addition to the configured list, so mappings are not a strict
device allowlist.

The agent's `/var/run/docker.sock` mount is read-only as a filesystem mount, but
that does **not** restrict Docker API operations. When SMART is enabled,
`SYS_RAWIO` and `SYS_ADMIN` capabilities grant substantial host access. Use
trusted hosts and protect the `deploy` account accordingly. System names should
be distinct on a shared hub.

## Backup, restore, and removal

Back up the hub's `beszel_data` identity/database and Caddy's `caddy/data` and
`caddy/config` directories together. Use a consistent filesystem snapshot or
stop the Compose services during a file-level backup. Store the backup off-host
in an encrypted location with access limited to trusted operators. Include
`.env` and `Caddyfile` (or record their values in the same protected backup).
Restore the same directories, ownership, `.env`, and Caddyfile before starting
the hub; the database holds Beszel identity and system configuration, while
Caddy state includes ACME certificates and configuration state.

Periodically restore a copy to an isolated test VM or private staging
environment. Verify that the hub starts and the prior administrator and systems
are present, then stop and dispose of the test copy. Keep restored test systems
isolated from production agents and public DNS.

Disabling an Ansible agent stops only its `beszel-agent` Compose project and
retains `/srv/beszel-agent`, including its protected token file and data.
Explicitly revoke the universal token in Beszel when it should no longer be
trusted; because it is shared, update all remaining agents and rerun their
playbooks with the replacement token. Purge retained agent files only when
re-enable is no longer required. Disable and rerun the VM playbook, revoke the
token, then remove `/srv/beszel-agent` and any backups containing `agent.env`.
Ordinary file deletion is not a reliable secure erase on SSDs, snapshots, or
journaled filesystems; token revocation is the security boundary. Hub system
entries may need separate cleanup.
