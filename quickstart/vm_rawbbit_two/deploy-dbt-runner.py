#!/usr/bin/env python3
"""Fail-closed host cutover. All supported ingestion enters through dbt-job.

No signals are sent to jobs. An ungated running runner is NEVER stopped here.
The same pipeline.lock inode is held from drain through publication/verification.
Docker-exec of raw dbt, bypassing dbt-job, is not a supported maintenance workflow.
"""

import argparse
import base64
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import urllib.request


CONTRACT = {"version": 1, "drain_aware": True, "pipeline_lock": "/app/runtime/pipeline.lock",
            "deploy_gate": "/app/runtime/deploy.gate", "route_snapshot_version": 1}
IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}\Z")
APP_ID = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}\Z")
COMPOSE_FILES = {"docker-compose.yml", "docker-compose.dozzle.yml",
                 "docker-compose.datasets.yml"}


class DeploymentError(Exception):
    pass


def run(argv, timeout=120):
    # Never print Docker config, credentials, or subprocess error bodies.
    try:
        return subprocess.run(argv, check=True, capture_output=True, text=True,
                              timeout=timeout).stdout.strip()
    except (subprocess.SubprocessError, OSError) as exc:
        raise DeploymentError("Docker/host command failed; gate retained if installed") from exc


def atomic_write(path, data, mode=0o600):
    fd, name = tempfile.mkstemp(prefix=".deploy-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
            os.fchmod(stream.fileno(), mode)
        os.replace(name, path)
        sync_directory(path.parent)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@contextlib.contextmanager
def locked(path, timeout):
    # Append, never truncate or rename this inode (container and host share it).
    with path.open("a") as stream:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise DeploymentError("Drain timed out; active work was NOT killed; gate retained")
                time.sleep(min(0.1, max(0, deadline - time.monotonic())))
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def validate_routes(data, database):
    try:
        def unique_keys(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate JSON field")
                result[key] = value
            return result
        if len(data) > 1048576:
            raise ValueError("snapshot too large")
        value = json.loads(data, object_pairs_hook=unique_keys)
        if not isinstance(value, dict) or set(value) != {"version", "routes"}:
            raise ValueError("invalid snapshot fields")
        if type(value["version"]) is not int or value["version"] != 1:
            raise ValueError("unsupported snapshot version")
        if not isinstance(value["routes"], list) or len(value["routes"]) > 1000:
            raise ValueError("routes must be a list")
        apps, targets = set(), set()
        for route in value["routes"]:
            if not isinstance(route, dict) or set(route) != {"app_id", "dataset_id", "database", "table"}:
                raise ValueError("invalid route fields")
            if not all(isinstance(v, str) for v in route.values()):
                raise ValueError("route fields must be strings")
            if not APP_ID.fullmatch(route["app_id"]):
                raise ValueError("unsafe app ID")
            if not re.fullmatch(r"[a-z][a-z0-9_]{0,39}", route["dataset_id"]):
                raise ValueError("unsafe dataset ID")
            if not IDENTIFIER.fullmatch(route["database"]) or route["table"] != "events":
                raise ValueError("unsafe or unsupported target")
            target = (route["database"], route["table"])
            if target == (database, "events") or route["app_id"] in apps or target in targets:
                raise ValueError("duplicate/colliding route")
            apps.add(route["app_id"])
            targets.add(target)
        return value
    except (ValueError, TypeError, KeyError) as exc:
        raise DeploymentError("Invalid enabled routing snapshot; no publication or writes") from exc


def supports_gate(argv):
    try:
        value = json.loads(run(argv, timeout=30))
        return (isinstance(value, dict) and all(value.get(k) == v for k, v in CONTRACT.items()) and
                 'validate-config' in value.get('commands', []))
    except (DeploymentError, ValueError):
        return False


class Deployment:
    def __init__(self, args):
        self.args = args
        self.install = Path(args.install_dir).resolve()
        self.stage = Path(args.stage_dir).resolve()
        self.runtime = Path(args.runtime_dir).resolve()
        self.files = args.compose_file
        if (not self.files or self.files[0] != "docker-compose.yml" or
                len(set(self.files)) != len(self.files) or not set(self.files) <= COMPOSE_FILES):
            raise DeploymentError("Explicit base and complete desired overlay list required")
        self.artifacts = {name: (self.stage / name).read_bytes() for name in [".env"] + self.files}
        self.config = json.loads(run(self.compose(staged=True) + ["config", "--format", "json"]))
        self.env = self.config["services"]["dbt-runner"]["environment"]
        self.database = self.env.get("CLICKHOUSE_DATABASE", "analytics")
        if not isinstance(self.database, str) or not IDENTIFIER.fullmatch(self.database):
            raise DeploymentError("Invalid resolved default database")
        enabled = str(self.env.get("RAWBBIT_DBT_ROUTING_ENABLED", "0"))
        if enabled not in {"0", "1"}:
            raise DeploymentError("Routing switch must be 0 or 1")
        self.enabled = enabled == "1"
        if self.env.get('RAWBBIT_RAW_LOAD_MODE') not in {'dbt', 'legacy'}:
            raise DeploymentError('Raw load mode must be dbt or legacy')
        if self.enabled and self.env.get("RAWBBIT_RAW_LOAD_MODE") != "dbt":
            raise DeploymentError("Enabled routing is incompatible with legacy loading")
        if self.env.get("RAWBBIT_DBT_ROUTES_FILE") != "/app/runtime/dbt-routes.json":
            raise DeploymentError("Runner must use the shared runtime route snapshot")
        mounts = self.config["services"]["dbt-runner"].get("volumes", [])
        if not any(v.get("target") == "/app/runtime" and v.get("type") == "bind" and
                   Path(v["source"]).resolve() == self.runtime and not v.get("read_only", False)
                   for v in mounts):
            raise DeploymentError("Runner and host must share the exact writable runtime mount")
        self.routes = None
        if self.enabled:
            try:
                self.routes = (self.stage / "dbt-routes.json").read_bytes()
            except OSError as exc:
                raise DeploymentError('Enabled route snapshot missing/unreadable; no publication or writes') from exc
            validate_routes(self.routes, self.database)
        if args.pre_routing_rollback and self.enabled:
            raise DeploymentError("Pre-routing rollback requires routing disabled")
        self.previous_loader_users = set()
        self.assert_warehouse_unchanged()

    def assert_warehouse_unchanged(self):
        ids = run(['docker', 'ps', '-q', '--filter', 'label=com.docker.compose.project=rawbbit-two',
                   '--filter', 'label=com.docker.compose.service=clickhouse']).split()
        if not ids:
            return  # First install may start ClickHouse from the candidate.
        if len(ids) != 1:
            raise DeploymentError('Ambiguous running warehouse; separate maintenance required')
        actual = json.loads(run(['docker', 'inspect', ids[0]]))[0]
        expected = self.config['services']['clickhouse']
        actual_env = dict(item.split('=', 1) for item in actual['Config']['Env'])
        if (actual['Config']['Image'] != expected['image'] or
                any(actual_env.get(k) != str(v) for k, v in expected['environment'].items())):
            raise DeploymentError('Running ClickHouse image/environment differs; separately drain warehouse first')
        actual_mounts = {m['Destination']: str(Path(m['Source']).resolve()) for m in actual['Mounts']}
        for mount in expected.get('volumes', []):
            if mount.get('type') == 'bind' and actual_mounts.get(mount['target']) != str(Path(mount['source']).resolve()):
                raise DeploymentError('Running ClickHouse bind configuration differs; separately drain warehouse first')

    def compose(self, staged=False):
        directory = self.stage if staged else self.install
        argv = ["docker", "compose", "--project-directory", str(self.install),
                "--env-file", str(directory / ".env")]
        for name in self.files:
            argv += ["-f", str(directory / name)]
        if not staged and self.args.pre_routing_rollback:
            argv += ["-f", str(self.install / "docker-compose.dbt-paused.yml")]
        return argv

    def containers(self):
        ids = run(["docker", "ps", "-a", "-q", "--filter", "label=com.docker.compose.project=rawbbit-two",
                   "--filter", "label=com.docker.compose.service=dbt-runner"]).split()
        return json.loads(run(["docker", "inspect", *ids])) if ids else []

    def mcp_environment_changed(self):
        ids = run(['docker', 'ps', '-q', '--filter', 'label=com.docker.compose.project=rawbbit-two',
                   '--filter', 'label=com.docker.compose.service=mcp-server']).split()
        if len(ids) != 1:
            return True
        actual = json.loads(run(['docker', 'inspect', ids[0]]))[0]
        env = dict(item.split('=', 1) for item in actual['Config']['Env'])
        expected = self.config['services']['mcp-server']['environment']
        return any(env.get(key) != str(value) for key, value in expected.items())

    def assert_overlays_preserved(self, containers):
        for container in containers:
            recorded = container.get('Config', {}).get('Labels', {}).get('com.docker.compose.project.config_files', '')
            if not recorded:
                raise DeploymentError('Existing runner has no verifiable overlay inventory; operator review required')
            names = {Path(name).name for name in recorded.split(',')}
            if names - COMPOSE_FILES - {'docker-compose.dbt-paused.yml'}:
                raise DeploymentError('An unsupported active overlay needs separate reviewed migration; no overlays omitted')
            missing = names & COMPOSE_FILES - set(self.files)
            if missing == {'docker-compose.datasets.yml'} and self.args.remove_dataset_overlay:
                continue  # Explicit metadata-driven dataset endpoint removal.
            if missing:
                raise DeploymentError('Active overlays would be omitted; supply the complete overlay set')

    def validate_candidate(self, image):
        argv = ["docker", "run", "--rm", "--network", "none", "--entrypoint", "/app/bin/dbt-job"]
        for key in ("RAWBBIT_DBT_ROUTING_ENABLED", "RAWBBIT_DBT_ROUTES_FILE",
                    "RAWBBIT_RAW_LOAD_MODE", "CLICKHOUSE_DATABASE"):
            argv += ['-e', key + '=' + str(self.env[key])]
        if self.enabled:
            argv += ['--mount', 'type=bind,source=' + str(self.stage / 'dbt-routes.json') +
                     ',target=/app/runtime/dbt-routes.json,readonly']
        value = json.loads(run(argv + [image, 'validate-config'], timeout=30))
        if value.get('status') != 'valid':
            raise DeploymentError('Candidate rejected staged configuration; no publication')
        return value['revision']

    def assert_quiescence(self):
        # Also fence outstanding async mutations: lock release alone is not proof
        # that ClickHouse has finished a previous delete_insert request.
        clickhouse = self.config["services"]["clickhouse"]
        ports = [p for p in clickhouse["ports"] if int(p["target"]) == 8123]
        if len(ports) != 1 or ports[0].get("host_ip") != "127.0.0.1":
            raise DeploymentError("A localhost-only ClickHouse HTTP port is required for the cutover fence")
        env = clickhouse["environment"]
        users = [self.env.get("CLICKHOUSE_DBT_USER", "rawbbit_dbt"),
                 env.get("CLICKHOUSE_LOADER_USER", "rawbbit_loader")]
        users += sorted(self.previous_loader_users)
        if not all(IDENTIFIER.fullmatch(u) for u in users):
            raise DeploymentError("Unsafe loader identity")
        sql = ("SELECT (SELECT count() FROM system.processes WHERE user IN (" +
               ",".join("'" + u + "'" for u in users) + ")) + "
               "(SELECT count() FROM system.mutations WHERE NOT is_done) FORMAT TabSeparated")
        token = base64.b64encode((env["CLICKHOUSE_USER"] + ":" + env["CLICKHOUSE_PASSWORD"]).encode()).decode()
        request = urllib.request.Request("http://127.0.0.1:" + str(ports[0]["published"]),
                                         data=sql.encode(), headers={"Authorization": "Basic " + token})
        try:
            # Never route privileged localhost fence requests through a proxy.
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(request, timeout=10) as response:
                pending = int(response.read(64).strip())
        except Exception as exc:
            raise DeploymentError("Read-only ClickHouse cancellation fence failed; gate retained") from exc
        if pending:
            raise DeploymentError("ClickHouse still has loader queries or unfinished mutations; gate retained")

    def assert_legacy_disabled(self):
        active = self.install / ".env"
        if active.exists() and re.search(r"^RAWBBIT_RAW_LOAD_MODE=['\"]?legacy", active.read_text(), re.M):
            if (not self.enabled and self.env.get('RAWBBIT_RAW_LOAD_MODE') == 'legacy'
                    and active.read_bytes() == self.artifacts['.env']):
                # An unchanged legacy loader already skips on the same flock.
                # Permit repeat deployments, not a mode/credential cutover with
                # an entrant that could have parsed stale settings before lock.
                match = re.search(r'^RAWBBIT_DBT_LOCK_FILE=(.*)$', active.read_text(), re.M)
                path = match.group(1).strip().strip('\'"') if match else '/srv/rawbbit-two/dbt/pipeline.lock'
                if Path(path).resolve() != self.runtime / 'pipeline.lock':
                    raise DeploymentError('Legacy loader does not share the deployment pipeline lock')
                return
            if (self.install / "clickhouse/load_events_hourly.sh").exists():
                raise DeploymentError("Disable/rename the ungated host loader before cutover; no automatic stop")
            # A launch which parsed the old mode before the rename is still unsafe.
            for entry in Path("/proc").glob("[0-9]*/cmdline"):
                try:
                    if b"load_events_hourly.sh" in entry.read_bytes():
                        raise DeploymentError("An old host loader process remains; wait for completion")
                except (FileNotFoundError, ProcessLookupError):
                    continue

    def execute(self):
        self.runtime.mkdir(parents=True, exist_ok=True)
        gate = self.runtime / "deploy.gate"
        with locked(self.runtime / "deployment.lock", self.args.drain_timeout):
            if gate.exists() and not self.args.recover_gate:
                raise DeploymentError("An existing deploy.gate needs explicit inspected recovery")
            if not gate.exists():
                atomic_write(gate, b"dbt deployment in progress; do not remove without verification\n")
            self.assert_legacy_disabled()
            containers = self.containers()
            for container in containers:
                if container["State"]["Running"] and not supports_gate(
                        ["docker", "exec", container["Id"], "/app/bin/dbt-job", "--capabilities"]):
                    raise DeploymentError("Ungated runner is running; operator shared-lock drain/stop required")
            if containers and not any(c["State"]["Running"] for c in containers) and not self.args.bootstrap_stopped:
                raise DeploymentError("Stopped existing runner requires explicit bootstrap-stopped fence")
            self.assert_overlays_preserved(containers)
            # Pull and inspect the immutable candidate without mounting live runtime.
            run(self.compose(staged=True) + ["pull", "--policy", "missing", "dbt-runner"], timeout=600)
            image = self.config["services"]["dbt-runner"]["image"]
            image_id = run(['docker', 'image', 'inspect', '--format', '{{.Id}}', image])
            if not re.fullmatch(r'sha256:[0-9a-f]{64}', image_id):
                raise DeploymentError('Candidate image ID could not be frozen')
            candidate_gated = supports_gate(["docker", "run", "--rm", "--network", "none",
                                            "--entrypoint", "/app/bin/dbt-job", image_id, "--capabilities"])
            if not candidate_gated and not self.args.pre_routing_rollback:
                raise DeploymentError("Candidate image lacks the deployment gate/lock contract")
            revision = self.validate_candidate(image_id) if candidate_gated and not self.args.pre_routing_rollback else None
            with locked(self.runtime / "pipeline.lock", self.args.drain_timeout):
                self.assert_legacy_disabled()
                self.assert_warehouse_unchanged()
                # Reinspect under lock: do not trust an earlier stopped observation.
                current = self.containers()
                self.assert_overlays_preserved(current)
                for container in current:
                    if (container['State']['Running'] and
                            container.get('Config', {}).get('Labels', {}).get('com.docker.compose.oneoff', '').lower() == 'true'):
                        raise DeploymentError('A one-off runner remains; let queued/manual work defer or finish before retry')
                    if container["State"]["Running"] and not supports_gate(
                            ["docker", "exec", container["Id"], "/app/bin/dbt-job", "--capabilities"]):
                        raise DeploymentError("Ungated entrant appeared during drain; gate retained")
                    old_env = dict(v.split('=', 1) for v in container.get('Config', {}).get('Env', []))
                    self.previous_loader_users.add(old_env.get('CLICKHOUSE_DBT_USER', 'rawbbit_dbt'))
                self.assert_quiescence()
                if (self.runtime / 'write-fence.json').exists():
                    raise DeploymentError('Runtime write-fence requires operator reconciliation; gate retained')
                # Refuse mutable staging inputs (Ansible uses a unique stage dir).
                for name, data in self.artifacts.items():
                    if (self.stage / name).read_bytes() != data:
                        raise DeploymentError("Staged artifact changed during drain")
                if self.routes is not None and (self.stage / "dbt-routes.json").read_bytes() != self.routes:
                    raise DeploymentError("Staged route changed during drain")
                if self.routes is not None:
                    atomic_write(self.runtime / "dbt-routes.json", self.routes, 0o644)
                # Disabled routing retains any prior snapshot as recovery evidence;
                # a new disabled install requires no route file at all.
                for name, data in self.artifacts.items():
                    atomic_write(self.install / name, data, 0o600 if name == ".env" else 0o644)
                if self.args.pre_routing_rollback:
                    atomic_write(self.install / "docker-compose.dbt-paused.yml",
                                 b"services:\n  dbt-runner:\n    entrypoint: [/bin/sleep, infinity]\n", 0o644)
                run(self.compose() + ["up", "-d", "--no-deps", "--no-build", "--pull", "missing",
                                      "--force-recreate", "--wait", "--wait-timeout", "120", "dbt-runner"], timeout=180)
                ids = run(self.compose() + ["ps", "-q", "dbt-runner"]).split()
                if len(ids) != 1:
                    raise DeploymentError("Expected exactly one activated runner")
                actual = json.loads(run(["docker", "inspect", ids[0]]))[0]
                actual_env = dict(v.split("=", 1) for v in actual["Config"]["Env"])
                if (not actual["State"]["Running"] or actual.get('Image') != image_id or actual["Config"]["Image"] != image or
                        actual_env.get("RAWBBIT_DBT_ROUTING_ENABLED") != str(int(self.enabled)) or
                        actual_env.get("CLICKHOUSE_DATABASE") != self.database):
                    raise DeploymentError("Runner image/environment verification failed")
                if not any(m['Destination'] == '/app/runtime' and Path(m['Source']).resolve() == self.runtime
                           and m.get('RW') for m in actual.get('Mounts', [])):
                    raise DeploymentError('Activated runner no longer shares the host runtime/lock')
                expected_files = {str(self.install / f) for f in self.files}
                if self.args.pre_routing_rollback:
                    expected_files.add(str(self.install / "docker-compose.dbt-paused.yml"))
                labels = actual["Config"].get("Labels", {})
                if set(labels.get("com.docker.compose.project.config_files", "").split(",")) != expected_files:
                    raise DeploymentError("Activated runner overlay set does not match cutover")
                if not self.args.pre_routing_rollback:
                    if not supports_gate(["docker", "exec", ids[0], "/app/bin/dbt-job", "--capabilities"]):
                        raise DeploymentError("Activated runner lost gate compatibility")
                    if self.routes is not None:
                        mounted = run(["docker", "exec", ids[0], "cat", "/app/runtime/dbt-routes.json"])
                        if json.loads(mounted) != json.loads(self.routes):
                            raise DeploymentError("Mounted route snapshot verification failed")
                    actual_snapshot = json.loads(run(["docker", "exec", ids[0], "/app/bin/dbt-job", 'validate-config']))
                    if actual_snapshot.get('status') != 'valid' or actual_snapshot.get('revision') != revision:
                        raise DeploymentError('Activated route revision verification failed')
                    if self.env.get('RAWBBIT_RAW_LOAD_MODE') == 'dbt':
                        verified = json.loads(run(['docker', 'exec', ids[0], '/app/bin/dbt-job', 'verify-control']))
                        if verified.get('status') != 'control_access_verified':
                            raise DeploymentError('Loader observation grants unverified; gate retained')
                    # Default-only checks; route physical readiness is NOT a global
                    # deployment gate and no grants are mutated here.
                    for command in ("debug", "parse"):
                        run(self.compose() + ["exec", "-T", "dbt-runner", "dbt", command,
                                              "--project-dir", "/app", "--profiles-dir", "/app"], timeout=120)
                elif actual["Config"].get("Entrypoint") != ["/bin/sleep", "infinity"]:
                    raise DeploymentError("Pre-routing rollback must remain inert")
                record = {"version": 1, "image": image, "image_id": image_id, "compose_files": sorted(expected_files),
                          "routing_enabled": self.enabled, "ingestion_paused": self.args.pre_routing_rollback,
                          "route_revision": revision,
                          "routes_sha256": hashlib.sha256(self.routes).hexdigest() if self.routes else None,
                          "verified_at": time.time()}
                atomic_write(self.runtime / "deploy-fence.json", json.dumps(record).encode(), 0o644)
                if not self.args.pre_routing_rollback:
                    gate.unlink()
                    sync_directory(self.runtime)
        print("Runner cutover verified; " + ("ingestion paused, gate retained" if self.args.pre_routing_rollback else "gate released"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--install-dir", required=True)
    parser.add_argument("--stage-dir", required=True)
    parser.add_argument("--runtime-dir", default="/srv/rawbbit-two/dbt")
    parser.add_argument("--compose-file", action="append", required=True)
    parser.add_argument("--drain-timeout", type=int, default=600)
    parser.add_argument("--bootstrap-stopped", action="store_true")
    parser.add_argument("--recover-gate", action="store_true")
    parser.add_argument("--pre-routing-rollback", action="store_true")
    parser.add_argument('--remove-dataset-overlay', action='store_true')
    inspection = parser.add_mutually_exclusive_group()
    inspection.add_argument("--validate-only", action="store_true")
    inspection.add_argument('--inspect-mcp-env', action='store_true')
    inspection.add_argument('--check-quiescence', action='store_true',
                            help='Read-only server check for an operator-held bootstrap lock; does not stop jobs')
    args = parser.parse_args()
    try:
        if args.drain_timeout <= 0:
            raise DeploymentError("Drain timeout must be positive")
        if not args.validate_only and not args.inspect_mcp_env and sys.platform != 'linux':
            raise DeploymentError('Cutover requires the Linux Docker host and a shared bind-mount flock')
        deployment = Deployment(args)
        if args.inspect_mcp_env:
            print(json.dumps({'changed': deployment.mcp_environment_changed()}))
        elif args.check_quiescence:
            for container in deployment.containers():
                old_env = dict(v.split('=', 1) for v in container.get('Config', {}).get('Env', []))
                deployment.previous_loader_users.add(old_env.get('CLICKHOUSE_DBT_USER', 'rawbbit_dbt'))
            deployment.assert_quiescence()
            print('Server quiescence verified; caller must retain the shared pipeline lock')
        elif not args.validate_only:
            deployment.execute()
        return 0
    except (DeploymentError, OSError, ValueError, KeyError) as exc:
        # Do not stringify arbitrary parser/OS errors: they may include config.
        print(str(exc) if isinstance(exc, DeploymentError) else "Invalid staged deployment or host state", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
