"""Ephemeral Docker/ClickHouse/S3 setup; no fixture is allowed to reach prod."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
import uuid

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import requests


PROJECT = Path(__file__).resolve().parents[1]
SCHEMA = PROJECT.parent / "quickstart/vm_rawbbit_two/clickhouse/schema_analytics_events.sql"
IMAGE_CH = "clickhouse/clickhouse-server:24.8"
IMAGE_S3 = "chrislusf/seaweedfs:3.99"
START = "2026-07-01T00:00:00Z"
END = "2026-07-01T01:00:00Z"
CH_USER = "dbt_integration"
CH_PASS = "disposable_only"


def docker(*args: str, timeout: int = 60) -> str:
    done = subprocess.run(["docker", *map(str, args)], capture_output=True, text=True, timeout=timeout)
    if done.returncode:
        raise AssertionError(f"docker {args[:3]} failed: {done.stdout[-1000:]} {done.stderr[-1600:]}")
    return done.stdout.strip()


class Lab:
    def __init__(self, root: Path):
        self.root = root
        self.token = "dbt-it-" + uuid.uuid4().hex[:12]
        self.network = self.token
        self.ch = self.token + "-ch"
        self.s3 = self.token + "-s3"
        self.containers: list[str] = []
        self.dbt_image = self.token + "-candidate:local"
        self.work = root / "project"
        self.runtime = self.work / "runtime"
        self.routes_file = self.runtime / "dbt-routes.json"

    def sql(self, query: str, *, user: str = "default", password: str = "") -> str:
        return docker("exec", self.ch, "clickhouse-client", "--user", user,
                      "--password", password, "--query", query, timeout=45)

    def rows(self, db: str) -> list[tuple[str, str, str]]:
        text = self.sql(f"SELECT app_id, event_id, event_name FROM {db}.events ORDER BY app_id, event_id FORMAT TSV")
        return [tuple(line.split("\t")) for line in text.splitlines() if line]

    def snapshot(self) -> dict:
        return {"version": 1, "routes": [
            {"app_id": "route_a", "dataset_id": "a", "database": "team_a", "table": "events"},
            {"app_id": "route_b", "dataset_id": "b", "database": "team_b", "table": "events"},
        ]}

    def route(self, data: dict) -> None:
        self.routes_file.write_text(json.dumps(data))

    def dbt(self, command: str = "build", *, database: str = "default_custom", mode: str = "default",
            app: str | None = None, exclude: list[str] | None = None, timeout: int = 180) -> subprocess.CompletedProcess:
        """Invoke the pinned adapter against a fresh project copy, not host Python."""
        vars_ = {"rawbbit_window_start": START, "rawbbit_window_end": END}
        if app is not None:
            vars_["rawbbit_app_id"] = app
        if exclude is not None:
            vars_["rawbbit_excluded_app_ids"] = exclude
        args = ["docker", "run", "--rm", "--network", self.network,
                "--mount", f"type=bind,src={self.work},dst=/app",
                "-e", f"CLICKHOUSE_HOST={self.ch}", "-e", "CLICKHOUSE_PORT=8123",
                "-e", f"CLICKHOUSE_DATABASE={database}", "-e", f"CLICKHOUSE_DBT_USER={CH_USER}",
                "-e", f"CLICKHOUSE_DBT_PASSWORD={CH_PASS}", "-e", "DBT_PROFILES_DIR=/app",
                "-e", "DBT_TARGET_PATH=/app/runtime/target", "-e", "DBT_LOG_PATH=/app/runtime/logs",
                "--entrypoint", "dbt", self.dbt_image, command, "--project-dir", "/app",
                "--profiles-dir", "/app"]
        if command != "parse":
            args.extend(("--selector", "rawbbit_ingestion"))
        args.extend(("--vars", json.dumps(vars_)))
        return subprocess.run(args, text=True, capture_output=True, timeout=timeout)

    def job(self, *, enabled: str = "1", database: str = "default_custom", start: str = START,
            end: str = END, extra_env: dict[str, str] | None = None, timeout: int = 300) -> subprocess.CompletedProcess:
        args = ["docker", "run", "--rm", "--network", self.network,
                "--mount", f"type=bind,src={self.work},dst=/app",
                "-e", f"CLICKHOUSE_HOST={self.ch}", "-e", "CLICKHOUSE_PORT=8123",
                "-e", f"CLICKHOUSE_DATABASE={database}", "-e", f"CLICKHOUSE_DBT_USER={CH_USER}",
                "-e", f"CLICKHOUSE_DBT_PASSWORD={CH_PASS}", "-e", "RAWBBIT_RAW_LOAD_MODE=dbt",
                "-e", f"RAWBBIT_DBT_ROUTING_ENABLED={enabled}",
                "-e", "RAWBBIT_DBT_ROUTES_FILE=/app/runtime/dbt-routes.json",
                "-e", "RAWBBIT_DBT_LOCK_FILE=/app/runtime/pipeline.lock",
                "-e", "DBT_TARGET_PATH=/app/runtime/target", "-e", "DBT_LOG_PATH=/app/runtime/logs"]
        for key, value in (extra_env or {}).items():
            args += ["-e", f"{key}={value}"]
        args += ["--entrypoint", "/app/bin/dbt-job", self.dbt_image, "backfill", start, end]
        return subprocess.run(args, text=True, capture_output=True, timeout=timeout)

    def put(self, app: str, event: str, *, name: str = "view", hour: str = "00") -> None:
        # Explicit raw schema: any optional columns missing from the record are null.
        columns = ["event_id", "app_id", "environment", "event_name", "event_timestamp", "received_at",
                   "user_id", "user_pseudo_id", "session_id", "platform", "app_version", "os_version",
                   "device_model", "locale", "timezone", "event_params_json", "user_properties_json",
                   "traffic_source_json", "geo_json", "consent_json", "ingest_request_id", "ingest_user_agent",
                   "ingest_ip_hash", "nats_stream", "nats_sequence"]
        assert len(columns) == 25
        values = {column: None for column in columns}
        values.update(event_id=event, app_id=app, environment="prod", event_name=name,
                      event_timestamp="2026-07-01T00:15:00Z", received_at="2026-07-01T00:16:00Z",
                      user_pseudo_id="demo", nats_sequence=1)
        table = pa.table({key: pa.array([value], type=pa.int64() if key == "nats_sequence" else pa.string())
                          for key, value in values.items()})
        parquet = self.root / (uuid.uuid4().hex + ".parquet")
        pq.write_table(table, parquet)
        key = f"raw/app_id={app}/event_date=2026-07-01/hour={hour}/{event}.parquet"
        # SeaweedFS mini accepts anonymous S3 requests in this disposable lab.
        response = requests.put(f"http://127.0.0.1:{self.s3_port}/raw/{key}", data=parquet.read_bytes(), timeout=15)
        assert response.status_code in (200, 201, 204), (response.status_code, response.text[:300])


@pytest.fixture(scope="module")
def lab():
    if os.getenv("RAWBBIT_DBT_INTEGRATION") != "1":
        pytest.skip("set RAWBBIT_DBT_INTEGRATION=1 for disposable Docker integration tests")
    if not shutil.which("docker"):
        pytest.skip("Docker required")
    docker("info", "--format", "{{.ServerVersion}}", timeout=10)
    for image in (IMAGE_CH, IMAGE_S3):
        try:
            docker("image", "inspect", image, timeout=10)
        except AssertionError:
            docker("pull", image, timeout=240)
    with tempfile.TemporaryDirectory(prefix="rawbbit-dbt-it-") as temp:
        instance = Lab(Path(temp))
        instance.work.mkdir()
        for part in ("bin", "models", "macros", "tests", "runner"):
            shutil.copytree(PROJECT / part, instance.work / part)
        for name in ("dbt_project.yml", "profiles.yml", "selectors.yml", "requirements.txt",
                     "Dockerfile", ".dockerignore", "crontab"):
            shutil.copy2(PROJECT / name, instance.work / name)
        docker("build", "-q", "-f", str(instance.work / "Dockerfile"),
               "-t", instance.dbt_image, str(instance.work), timeout=300)
        versions = docker("run", "--rm", "--entrypoint", "dbt", instance.dbt_image, "--version", timeout=30)
        assert "installed: 1.9.10" in versions and "clickhouse: 1.9.8" in versions, versions
        assert "1.6.0" in docker("run", "--rm", "--entrypoint", "python", instance.dbt_image,
                                 "-c", "from importlib.metadata import version;print(version('clickhouse-connect'))")
        instance.runtime.mkdir(mode=0o777)
        (instance.runtime / "target").mkdir(mode=0o777)
        (instance.runtime / "logs").mkdir(mode=0o777)
        # Docker Desktop/Linux rootless bind mounts may use UID 1000 inside the runner.
        for folder in (instance.runtime, instance.runtime / "target", instance.runtime / "logs"):
            folder.chmod(0o777)
        config = Path(temp) / "s3.xml"
        config.write_text(f"""<clickhouse><named_collections><rawbbit_raw_s3>
<url>http://{instance.s3}:8333/raw/raw/</url>
</rawbbit_raw_s3></named_collections></clickhouse>""")
        docker("network", "create", instance.network)
        try:
            docker("run", "-d", "--name", instance.s3, "--network", instance.network,
                   "-p", "127.0.0.1::8333", "--entrypoint", "weed", IMAGE_S3,
                   "server", "-s3", "-dir=/data", "-ip=127.0.0.1", "-ip.bind=0.0.0.0")
            instance.containers.append(instance.s3)
            instance.s3_port = docker("port", instance.s3, "8333/tcp").rsplit(":", 1)[1]
            docker("run", "-d", "--name", instance.ch, "--network", instance.network,
                   "--mount", f"type=bind,src={config},dst=/etc/clickhouse-server/config.d/raw-s3.xml,readonly",
                   IMAGE_CH)
            instance.containers.append(instance.ch)
            for _ in range(50):
                try:
                    instance.sql("SELECT 1")
                    if requests.get(f"http://127.0.0.1:{instance.s3_port}/", timeout=2).status_code == 200:
                        break
                except (AssertionError, requests.RequestException):
                    pass
                time.sleep(1)
            else:
                raise AssertionError("disposable ClickHouse or SeaweedFS failed readiness")
            response = requests.put(f"http://127.0.0.1:{instance.s3_port}/raw", timeout=10)
            assert response.status_code == 200, (response.status_code, response.text[:200])
            # The canonical CREATE statement has analytics hardcoded; substitute only the
            # database identifier, preserving every type and engine clause verbatim.
            schema = SCHEMA.read_text()
            for db in ("default_custom", "team_a", "team_b"):
                instance.sql(schema.replace("analytics", db), user="default")
            instance.sql(f"CREATE USER {CH_USER} IDENTIFIED WITH sha256_password BY '{CH_PASS}'")
            for db in ("default_custom", "team_a", "team_b"):
                instance.sql(f"GRANT SELECT, SHOW, INSERT, CREATE TABLE, CREATE VIEW, DROP TABLE, DROP VIEW ON {db}.* TO {CH_USER}")
                instance.sql(f"GRANT ALTER DELETE ON {db}.* TO {CH_USER}")
                instance.sql(f"GRANT ALTER UPDATE(_row_exists) ON {db}.events TO {CH_USER}")
            instance.sql(f"GRANT CREATE TEMPORARY TABLE, S3 ON *.* TO {CH_USER}")
            instance.sql(f"GRANT NAMED COLLECTION ON rawbbit_raw_s3 TO {CH_USER}")
            # The safety control plane is independently privileged: unavailable
            # observation is an unknown outcome, never evidence of quiescence.
            for table in ("processes", "mutations", "query_log", "tables", "columns"):
                instance.sql(f"GRANT SELECT ON system.{table} TO {CH_USER}")
            instance.sql("CREATE USER scoped_reader IDENTIFIED WITH sha256_password BY 'disposable_only'")
            instance.sql("GRANT SELECT, SHOW ON team_a.* TO scoped_reader")
            instance.put("route_a", "a1")
            instance.put("route_b", "b1")
            instance.put("unrouted", "u1")
            yield instance
        finally:
            if os.getenv("RAWBBIT_INTEGRATION_KEEP") != "1":
                for name in reversed(instance.containers):
                    subprocess.run(["docker", "rm", "-f", name], capture_output=True)
                subprocess.run(["docker", "network", "rm", instance.network], capture_output=True)
                subprocess.run(["docker", "image", "rm", instance.dbt_image], capture_output=True)
