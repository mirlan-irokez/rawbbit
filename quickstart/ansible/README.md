# Rawbbit Ansible Deployment

This deployment path prepares one or both Rawbbit Ubuntu VMs and starts the
existing Docker Compose quickstarts. Compose remains the application source of
truth; Ansible owns repeatable host configuration, private configuration
rendering, deployment, and verification.

## What it deploys

- `vm_one.yml`: ingestion, NATS JetStream, raw writer, SeaweedFS, Caddy, and
  authenticated Dozzle
- `vm_two.yml`: ClickHouse, dbt runner, Rawbbit MCP, Metabase, Postgres, Caddy,
  and authenticated Dozzle
- `site.yml`: VM one first, then VM two

Either VM can be deployed or rebuilt independently. VM two accepts an existing
S3-compatible raw-storage endpoint and does not require this playbook to deploy
VM one.

## Workstation requirements

- Ansible Core 2.17 or newer
- access to this repository checkout
- SSH-key access as the manually created `deploy` user
- the Ansible Vault password

Install the required collections:

```bash
cd quickstart/ansible
ansible-galaxy collection install -r requirements.yml
```

## Manual server bootstrap

The first identity bootstrap intentionally remains manual. On the workstation,
create an SSH key if the operator does not already have one:

```bash
ssh-keygen -t ed25519 -a 100 -f ~/.ssh/rawbbit
```

After the provider gives you initial root access, log in as root and run:

```bash
adduser --disabled-password --gecos "" deploy
usermod -aG sudo deploy

install -d -m 0700 -o deploy -g deploy /home/deploy/.ssh
nano /home/deploy/.ssh/authorized_keys
chown deploy:deploy /home/deploy/.ssh/authorized_keys
chmod 0600 /home/deploy/.ssh/authorized_keys

printf '%s\n' 'deploy ALL=(ALL:ALL) NOPASSWD:ALL' \
  > /etc/sudoers.d/90-rawbbit-deploy
chmod 0440 /etc/sudoers.d/90-rawbbit-deploy
visudo -cf /etc/sudoers.d/90-rawbbit-deploy
```

Paste the workstation key from `~/.ssh/rawbbit.pub` as one line in
`/home/deploy/.ssh/authorized_keys`. The passwordless sudo rule is required
because the playbooks use privilege escalation non-interactively.

Before closing the root session, test key login and non-interactive sudo from a
second workstation terminal:

```bash
ssh -i ~/.ssh/rawbbit deploy@VM_PUBLIC_IP 'sudo -n true'
```

The command must exit successfully without asking for a password. Repeat the
server-side bootstrap for each VM.

The complete bootstrap sequence is:

1. Rent or create an Ubuntu 22.04 or 24.04 server.
2. Log in using the provider's initial root access.
3. Create `deploy`, install the operator SSH public key, and configure
   passwordless sudo using the commands above.
4. Confirm key-based access and sudo from a second terminal.
5. Create the required public DNS records.

Keep the original session open during the first Ansible run. The playbooks
refuse to harden SSH when Ansible is using a password.

## Configure inventory and variables

```bash
cp inventory.example.yml inventory.yml
cp group_vars/all/main.yml.example group_vars/all/main.yml
cp group_vars/all/vault.yml.example group_vars/all/vault.yml
```

The `.example` files are tracked templates. Do not put operator-specific values
into them. The copied files are the live Ansible inputs and are excluded by
`.gitignore`:

- `inventory.yml` answers **where Ansible connects**. Set each server's public
  IP or resolvable hostname in `ansible_host`. Hosts under `rawbbit_one` are
  targeted by `vm_one.yml`; hosts under `rawbbit_two` are targeted by
  `vm_two.yml`. The shared `ansible_user: deploy` selects the manually created
  SSH account. `ansible.cfg` uses this file as the default inventory, while the
  documented commands also pass it explicitly with `-i inventory.yml`.
- `group_vars/all/main.yml` holds **non-secret desired configuration** shared
  with the inventory hosts. Set the administrative CIDR, public DNS names,
  deployment choices, resource profiles, VM-two S3 endpoint, and Dozzle user
  metadata/revisions here. Edit this file normally when infrastructure or
  non-secret settings change.
- `group_vars/all/vault.yml` holds **secret configuration** such as service
  passwords, API keys, salts, S3 credentials, and Dozzle login passwords.
  Replace the required `change_me` values, encrypt the file before the first
  run, and use `ansible-vault edit` for later changes. Ansible decrypts it in
  memory when `--ask-vault-pass` is used.

All three copied files stay on the Ansible workstation. The playbooks use them
to render only the required application configuration on each server; they do
not copy the inventory or Vault file to a VM.

For `site.yml`, configure both VM sections. For `vm_one.yml` or `vm_two.yml`,
only the common values and that VM's section need real values; unused example
values for the other VM are not evaluated. Copy the templates once before the
first run, then keep and update the copied files for later reruns and recovery.

### VM-one raw-writer throughput settings

The VM-one non-secret variables control the raw-writer's pull and flush behavior.
These are the recommended live/example values:

```yaml
rawbbit_one_nats_fetch_batch: 1000
rawbbit_one_raw_flush_interval_seconds: 60
rawbbit_one_nats_max_ack_pending: 10000
```

`rawbbit_one_nats_fetch_batch` is the maximum number of messages requested by one
pull; it is not a minimum, so low-volume traffic does not wait for a full batch.
`rawbbit_one_raw_flush_interval_seconds` bounds normal in-memory buffering before
partial Parquet files are flushed. `rawbbit_one_nats_max_ack_pending` is applied
to the existing `EVENTS/raw-writer` durable JetStream consumer after Compose
starts. Ansible retries until the consumer exists, updates it only when needed,
and verifies the final value without deleting or recreating the consumer.
The role accepts positive integer-form values, requires a flush interval below
the fixed 300-second ACK wait, and requires Max Ack Pending to be at least the
fetch batch.

If a key is omitted, the role fallback is `200` for the fetch batch, `60` seconds
for the flush interval, and `1000` for Max Ack Pending. Once the role runs,
`rawbbit_one_nats_max_ack_pending` is Ansible-managed consumer state; set it
explicitly in an existing inventory if you need to preserve a manually chosen
value rather than adopting the fallback.

These settings do not require a raw-writer image rebuild. A larger Max Ack
Pending permits more in-flight messages and can increase memory use, so monitor
the consumer backlog, outstanding acknowledgements, redeliveries, and host
storage while a backlog drains.

The role uses a pinned `nats-box` image digest for these administrative commands;
override that role variable only as part of a deliberate CLI upgrade.

`rawbbit_admin_cidr` controls which source network UFW allows to reach SSH.
Use a narrow `/32` address when the operator has a stable public IP.

If the operator has only a dynamic public IP, leave the value empty:

```yaml
rawbbit_admin_cidr: ""
```

While still logged in as root, use the same broader fallback as the manual
quickstarts before enabling UFW manually:

```bash
ufw allow OpenSSH
```

Ansible applies that OpenSSH profile automatically when the CIDR is empty.
This permits SSH from any source, so keep SSH-key authentication enabled and
use a provider firewall, VPN, or other trusted access boundary when available.
Keep the first root session open until the Ansible run finishes and a new
`deploy` connection succeeds.

## Configure encrypted secrets

Replace every `change_me` value required by the playbook you plan to run. For
`site.yml`, replace values for both VMs. The Vault file holds:

- collector API keys and IP hash salt
- SeaweedFS writer, reader, and administrator credentials
- ClickHouse and Postgres passwords
- MCP bearer tokens
- Dozzle login passwords

Choose a different Dozzle password for each VM and store the plain values only
inside the Vault file:

```yaml
vault_rawbbit_one_dozzle_password: "replace_with_a_long_password"
vault_rawbbit_two_dozzle_password: "replace_with_another_long_password"
```

Do not run Dozzle's Docker generator manually. After installing Docker,
Ansible passes each Vault password to the pinned Dozzle image through protected
standard input and captures the generated `users.yml`. The password is not
placed in the remote command line or Ansible output.

After replacing the required `change_me` values, encrypt the file:

```bash
ansible-vault encrypt group_vars/all/vault.yml
```

Keep the Vault password in a password manager. Back up the encrypted Vault,
inventory, and non-secret variables in a private repository or encrypted
backup outside the Rawbbit servers. They are required to reconstruct private
configuration after a server loss and are intentionally ignored by this
public repository.

To change a Dozzle login later, edit the encrypted Vault:

```bash
ansible-vault edit group_vars/all/vault.yml
```

Then increment the corresponding non-secret revision in
`group_vars/all/main.yml`:

```yaml
rawbbit_one_dozzle_users_revision: 2
# or
rawbbit_two_dozzle_users_revision: 2
```

Rerun that VM's playbook. The revision makes password rotation explicit and
prevents bcrypt's random salt from rewriting `users.yml` during every normal
Ansible run. Increment it as well when changing the Dozzle username, email, or
display name.

## Deploy

Both VMs, in dependency order:

```bash
ansible-playbook -i inventory.yml site.yml --ask-vault-pass
```

Only VM one:

```bash
ansible-playbook -i inventory.yml vm_one.yml --ask-vault-pass
```

Only VM two:

```bash
ansible-playbook -i inventory.yml vm_two.yml --ask-vault-pass
```

For a genuinely fresh VM two, this is the standard command after the SSH/deploy
account setup, [inventory/configuration and Vault setup](#configure-inventory-and-variables),
and [supported image selection](#vm-two-dbt-app-id-routing). Fresh means no
existing runner container, retained deployment gate/write fence, restored
ClickHouse/runtime state, or prior ingestion needing upgrade/recovery. Leave
`rawbbit_two_dbt_bootstrap_stopped` and `rawbbit_two_dbt_recover_gate` false;
the old-runner launcher shim and drain/stop procedure do not apply.

In dbt mode, even a fresh install can fail closed because required observation
grants are not provisioned automatically. An authorized administrator must
review effective access and apply the documented narrow grants as needed.
This is not a guarantee of unattended completion in one run. A failed attempt
with retained gate/container state needs inspection and recovery, not blind
bootstrap flags; see [fresh VM-two installation](../vm_rawbbit_two/README.md#fresh-vm-two-installation).

The playbooks are intended to be safe to rerun. They validate Compose before
changing containers and preserve `/srv/rawbbit-one` and `/srv/rawbbit-two`.

## Host security behavior

The common roles:

- install host utilities and Docker from Docker's official Ubuntu repository
- configure UTC and UFW
- allow SSH from `rawbbit_admin_cidr`, or through the broader OpenSSH UFW
  profile when the CIDR is empty
- allow public HTTP/HTTPS
- add `deploy` to the root-equivalent Docker group

SSH hardening manages both `/etc/ssh/sshd_config` and:

```text
/etc/ssh/sshd_config.d/00-rawbbit-hardening.conf
```

The effective policy is:

```text
PasswordAuthentication no
KbdInteractiveAuthentication no
PubkeyAuthentication yes
PermitRootLogin prohibit-password
```

Ansible runs `sshd -t` and checks `sshd -T` before reloading SSH. If validation
fails, it restores the original files and does not reload the service.

## Dozzle

Dozzle is part of both automated deployments. Each VM has its own hostname,
authenticated `users.yml`, persisted `/data`, and MCP endpoint. Shell access
and container actions remain disabled through the `none` user role.

The role generates `users.yml` with the pinned Dozzle image only when the file
is missing or its `rawbbit_*_dozzle_users_revision` changes. Secret-bearing
generation and file-installation tasks use `no_log: true`.

The Docker socket is security-sensitive. A read-only filesystem mount does not
restrict Docker API calls, so Dozzle must remain authenticated and must not be
exposed through a raw container port.

## VM-two independence

VM two reads these variables rather than assuming a managed VM one:

```yaml
rawbbit_two_s3_endpoint: https://s3.example.com
rawbbit_two_s3_bucket: rawbbit_raw
rawbbit_two_s3_prefix: raw
```

The playbook verifies the endpoint before deploying analytics services. Reader
credentials come from Vault.

<!-- dataset-mcp:start -->
## Optional multi-dataset MCP on VM two

An empty registry keeps the existing VM-two deployment unchanged. Dataset
support is opt-in and requires a published, versioned MCP image newer than
`0.0.2`; do not enable it with `0.0.2`, `latest`, or an unpublished tag. The
Ansible role uses the VM-two Compose overlay only when datasets are configured.
After a compatible release is published, set `rawbbit_two_mcp_image` in the
private `main.yml` to that published version; leave the example pin unchanged
until then.
`rawbbit_two_datasets` defaults to an empty list. Use the tracked
`group_vars/all/main.yml.example` and `group_vars/all/vault.yml.example` for
field names: the main example shows neutral `secret_ref: team_a`, and the Vault
example has placeholder entries for `team_a` and `team_b`. Copy their shapes to
the ignored live files, replace placeholders, and keep credential values in
encrypted Vault.

Put non-secret dataset IDs, paths, modes, table names, exposure choices, and
secret references in the private non-secret variables file. Put the referenced
ClickHouse passwords and scoped bearer-token values only in encrypted
`group_vars/all/vault.yml`; never add real credentials to `.example` files or
logs. Neutral sample IDs are `team_a` and `team_b`. All authenticated users of
main `/mcp` see the same explicitly exposed dataset set. Scoped paths such as
`/datasets/team_a/mcp` and `/datasets/team_b/mcp` each use their own bearer
domain and restricted ClickHouse identity; one scoped token cannot authenticate
main `/mcp` or another dataset endpoint.
Dataset databases must differ from the main `analytics` database. The
privileged preflight checks the configured main ClickHouse user (`rawbbit_mcp`)
for direct or transitive `SELECT`/`ALL` grants to every disabled or unexposed
dataset, plus previously registered database/table targets retained in the
ownership journal after removal from the registry. A conflict stops provisioning
without modifying the main user's existing grants; narrow them manually and
rerun the preflight. This check also runs for all-disabled registries and for an
empty registry when a prior ownership journal exists; a fresh empty install
needs no dataset preflight.

For managed ClickHouse password rotation, change the Vault `mcp_password` and
increment that dataset's `password_revision` in the non-secret registry in the
same deployment. Keep the runtime password in sync; an unchanged revision means
the provisioner deliberately does not rotate the owned ClickHouse user. Scoped
bearer-token rotation does not use `password_revision`.

Each dataset selects `managed` or `reference` provisioning. Managed mode creates
missing databases and optionally the compatible Rawbbit events table, then
reconciles only objects recorded as owned. Reference mode verifies an
externally managed compatible table and restricted login without changing
ClickHouse schema or access entities. Ansible stages separate version-1
`datasets-runtime.json` and `datasets-provision.json` contracts: the runtime
file is mode `0600`, owned by `deploy`, and is the only dataset file mounted
read-only into MCP; the provisioning file is root-owned, mode `0600`, host-only,
and contains privileged connection data plus non-secret `main_database` and
`main_user` values. The provisioner uses them to reject database overlap and
check unexposed-dataset access. The MCP container never receives ClickHouse
administrator or BI/direct-user credentials.

When the registry has entries, or a previous ownership journal remains,
Ansible stages protected files, waits for ClickHouse health, then preflights and
runs the root-owned `/srv/rawbbit-two/clickhouse/provision_datasets.py` with
`/usr/bin/python3` and file-path arguments only. Managed ClickHouse objects are
provisioned only for enabled managed datasets; the root-owned journal retains
database/table isolation targets for all registrations, including references
and disabled entries. An all-disabled or removed registry is checked before the
empty runtime is activated, without deleting database objects or the retained
targets. For enabled datasets, Ansible atomically activates the verified
runtime file and immediately recreates MCP with the opt-in overlay when runtime
credentials, rendered environment/auth, or the image changes, before waiting on
unrelated services, so a removed token/route does not remain active through a
long Compose wait. A fresh empty install with no journal skips dataset files and
preflight entirely.
Never pass secret values on the command line. A provisioning failure leaves the previous live MCP
configuration untouched; on first install keep MCP stopped if provisioning
fails. The separate standalone `mcp-server` Compose overlay has no host-side
provisioner.

Back up `/srv/rawbbit-two/clickhouse/datasets-ownership.json` with ClickHouse
state and the protected configuration. The journal is root-owned, mode `0600`,
outside ClickHouse data and MCP mounts. Its retained database/table isolation
targets are not ownership claims over reference-mode objects and are not
discarded automatically when a registry entry is removed. Restore a lost journal from backup or
complete an explicit reviewed ownership check before provisioning again; do
not silently adopt existing same-named objects. Provisioning and registration
do not ingest events. With dbt routing disabled, dbt continues to load all apps
into the configured default `CLICKHOUSE_DATABASE.events` table (normally
`analytics.events`). Optional app-ID routing can direct selected apps to
existing events tables; dataset MCP enablement controls endpoint exposure, not
ingestion. See [VM-two dbt app-ID routing](#vm-two-dbt-app-id-routing).
An interrupted create can be retried only when its object is absent or its
recorded ClickHouse ID matches. For ambiguous identities or interrupted
non-creation mutations, stop MCP and follow the VM-two guide's recovery steps;
do not hand-edit the journal to bypass preflight.

Validate the opt-in Compose render from the VM-two deployment directory without
printing resolved configuration, then run the existing playbook:

```bash
docker compose -f docker-compose.yml -f docker-compose.datasets.yml config --quiet
ansible-playbook -i inventory.yml vm_two.yml --ask-vault-pass
```

Before rollout, the repository-side checks remain:

```bash
ansible-playbook -i inventory.example.yml vm_two.yml --syntax-check
ansible-lint .
```

Disabling/removing a dataset and rerunning the playbook withdraws MCP access
after MCP recreation; it does not delete the ClickHouse database, table, or
data, or revoke the provisioner-managed MCP ClickHouse user, role, or grants. It
also does not revoke separately managed direct ClickHouse credentials. Revoke
database identities separately through an operator-reviewed offboarding
procedure when needed. For rollback, restore the prior runtime configuration
and published image; preserve ClickHouse data and the ownership journal. Verify
rotated/removed MCP tokens are rejected.
Use the VM-two guide's [separate database-identity offboarding checklist](../vm_rawbbit_two/README.md#offboard-database-identities-separately)
for grant inspection, managed MCP-user revocation, and safe preservation of data
and journal targets.
<!-- dataset-mcp:end -->

## VM-two dbt app-ID routing

VM two can opt into availability-first routing without adding MCP endpoints or
changing the MCP image. In the ignored `group_vars/all/main.yml`, set the
non-secret routing variables; routing stays disabled unless explicitly enabled:

```yaml
rawbbit_two_dbt_routing_enabled: false
rawbbit_two_dbt_routes: []

# Example only; use an app_id observed in raw Parquet and a dataset ID already
# present in rawbbit_two_datasets.
# rawbbit_two_dbt_routing_enabled: true
# rawbbit_two_dbt_routes:
#   - app_id: example_app
#     dataset_id: example_dataset
```

Each `dataset_id` must resolve to an entry in `rawbbit_two_datasets`. Ansible
resolves the ClickHouse database from that metadata and routes to the `events`
table. The metadata reference must remain present even if that dataset's
`enabled` flag is false; that flag controls the existing dataset
provisioning/MCP behavior, while dbt routing resolves metadata independently of
it. Removing metadata still referenced by a dbt route is a configuration error.
Routing does not use MCP credentials, require a new MCP endpoint, create a
ClickHouse destination, or change grants. Ensure each physical target is an
existing compatible table and does not collide with the resolved default
`CLICKHOUSE_DATABASE.events` target.
A missing, inaccessible, or incompatible physical destination causes fallback
for that app and window; invalid route configuration fails before writes.

The runner uses `RAWBBIT_DBT_ROUTING_ENABLED=0|1` and reads its version-1 route
snapshot at `RAWBBIT_DBT_ROUTES_FILE=/app/runtime/dbt-routes.json`. Ansible
renders the snapshot from the variables and mounts it through the persistent
dbt runtime directory. The runner must use `RAWBBIT_RAW_LOAD_MODE=dbt`; enabled
routing is not compatible with the legacy shell loader. With routing disabled
or unconfigured, existing all-app loading into the default events table is
unchanged. See the dbt project README and VM-two quickstart for exact routing
order, exit-code meanings, event visibility, replay, and operator recovery.

Review effective ClickHouse grants for the existing `rawbbit_dbt` identity on
the default and route tables before enabling any route. The required
`delete_insert` privileges depend on the pinned dbt-clickhouse adapter and
deployed ClickHouse version; verify them and apply only table-scoped grants for
the operations the adapter actually uses. Separately, the safety control plane
requires explicit `SELECT` on `system.processes`, `system.mutations`,
`system.query_log`, `system.tables`, and `system.columns`, even with routing
disabled. The first-boot baseline script does not add these observation grants;
this applies to fresh and persistent installations. Ansible does not apply them
automatically. An authorized administrator must review effective access and
apply the existing narrow grants as needed. The deployment runs
`dbt-job verify-control` before gate release and fails closed if access is
missing. See [dbt observation access](../vm_rawbbit_two/README.md#dbt-observation-access)
for the exact narrowly scoped SQL grants and
tagged-query cancellation requirements.

### Safe first upgrade and route changes

A pre-routing dbt-runner does not understand the drain gate, and the current
helper refuses to proceed while that ungated runner is running. Setting a gate
does not protect an old image. Prevent new scheduled starts and manual backfill
submissions, let every active load finish naturally, drain already-queued
manual backfills, and follow the VM-two guide's launcher-shim and read-only
server-quiescence procedure. The helper's `--bootstrap-stopped` flag is only
valid after the operator has proved the old runner stopped; it does not stop
the runner. Do not send SIGTERM, cancel a container, or use a Compose
stop/recreate to interrupt active dbt or ClickHouse work. If quiescence and the
shared-lock state cannot be proven, abort without changing the live runner,
route snapshot, or image. Hold the same host lock across the old-runner stop,
replacement, and verification.

For a routing-capable runner, deployment establishes the gate before changing
runner-observed environment, route file, image, or Compose configuration. Every
scheduled/manual entry checks it both before and while holding the shared lock.
Deployment then acquires the same host-visible
`/srv/rawbbit-two/dbt/pipeline.lock` used by ingestion with a bounded wait and
holds it across publication and runner-only recreation. A timeout must abort
without terminating active work or replacing the live config/image. Keep the
gate closed if deployment state is uncertain; release it only after verifying
the runner image, active route revision, and active Compose overlays. Every
Compose operation must preserve all currently active overlays (including
Dozzle, dataset/MCP, and any dbt-routing overlays); route changes recreate only
`dbt-runner`, not ClickHouse, MCP, or Metabase. The unchanged hourly and daily jobs share one
model and lock.

The helper's `--check-quiescence` option is a read-only server check for the
operator-held bootstrap lock; it never stops jobs. Follow the VM-two guide's
explicit old-runner launcher shim, server-quiescence check, and same-inode lock
procedure for the first upgrade. `rawbbit_two_dbt_drain_timeout_seconds` defaults
to `600` seconds for deployment and shared-lock acquisition; timeout retains
the gate and does not terminate active loads.

The role default remains
`ghcr.io/mirlan-irokez/rawbbit-dbt-runner:0.1.1`; it predates the gate and
routing contract and is rejected as a routing-capable candidate. The tracked
`group_vars/all/main.yml.example` selects `rawbbit-dbt-runner:local` instead;
copying that example does not change the role default. Select either this local
build or a tested, compatible released image explicitly. For a remote image
selection, do not use `0.1.1`, an untested/unpublished registry tag, or registry
`latest` for routing.

With `rawbbit_two_dbt_runner_image: rawbbit-dbt-runner:local` in the ignored
`group_vars/all/main.yml`, each normal VM-two Ansible run that reaches the local
image build copies the checked-in `dbt_project/` build context and invokes a
build, even when the tag already exists. The build can reuse Docker layers; the
task may report `changed` on every such run, and unchanged inputs can leave the
image ID unchanged. This selection does not switch to GHCR, push an image, or
disable the build cache. Copying source alone does not update a running
container: the build retags the local candidate before the pipeline lock is
acquired, while the live runner stays on its current image until the existing
gate, lock, quiescence checks, and safe cutover succeed. A failed build stops
before runner activation. The recursive copy is not a deletion-sync guarantee;
removed source files may remain in the VM build context. A local build requires
no registry upload. For the one-time old-runner bootstrap and typed recovery
command, follow the [VM-two deployment guide](../vm_rawbbit_two/README.md#safe-first-upgrade-and-route-changes).

After changing routing variables, validate the Ansible playbook and Compose
render. Run the ordinary deployment command below only after selecting a tested
local or released candidate and meeting the maintenance or gate-and-lock
preconditions:

```bash
ansible-playbook -i inventory.yml vm_two.yml --ask-vault-pass
```

The deployment does not automatically retry, delete, or move fallback rows.
Back up the persistent dbt runtime state and retain outcome/revision records
long enough for bounded manual replay before raw Parquet expires. See the VM-two
quickstart for route outcomes and replay checks.

## Recovery

Ansible rebuilds the operating environment; it does not replace data backups.
To replace one failed VM:

1. Provision a clean supported Ubuntu server.
2. Manually establish the `deploy` SSH identity.
3. Change that host's IP in `inventory.yml`.
4. Run only its playbook.
5. Stop affected services and restore application data.
6. Start the Compose stack and rerun verification.
7. Change DNS if the public IP changed.

Backend-specific data restore automation is intentionally deferred until a
backup backend and retention policy are selected.

## Validation before committing changes

```bash
ansible-playbook -i inventory.example.yml vm_one.yml --syntax-check
ansible-playbook -i inventory.example.yml vm_two.yml --syntax-check
ansible-lint .
```

The syntax checks do not connect to the example hosts.
