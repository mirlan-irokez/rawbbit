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
