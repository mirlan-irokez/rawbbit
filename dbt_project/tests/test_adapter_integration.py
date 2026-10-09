"""Opt-in disposable ClickHouse 24.8 adapter test (never point at production).

Run inside the pinned runner image with source mounted at /app and
RAWBBIT_DBT_TEST_CLICKHOUSE=1 plus CLICKHOUSE_HOST/PORT/DBT_USER/DBT_PASSWORD.
Creates and drops only a random routing_runtime_test_* database. The test
identity needs setup privileges on that disposable instance, not production.
"""
import os
import contextlib
import io
from pathlib import Path
import sys
import subprocess
import tempfile
import time
import unittest
import urllib.parse
import urllib.request
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'runner'))
from clickhouse import ClickHouse, ControlError, EXPECTED_COLUMNS, literal
from runner import Runner
from storage import load


@unittest.skipUnless(os.environ.get('RAWBBIT_DBT_TEST_CLICKHOUSE') == '1', 'disposable integration opt-in required')
class AdapterIntegration(unittest.TestCase):
    def setup_sql(self, sql):
        control = ClickHouse(os.environ)
        request = urllib.request.Request(control.url + '/?' + urllib.parse.urlencode({'query_id': str(uuid.uuid4())}),
                                         data=sql.encode(), method='POST',
                                         headers={'X-ClickHouse-User': control.user,
                                                  'X-ClickHouse-Key': control.password})
        with control.opener.open(request, timeout=10) as response:
            response.read()

    def test_every_adapter_request_has_exact_server_query_id_and_settings(self):
        from dbt_child import install_tracking
        from dbt.adapters.clickhouse.credentials import ClickHouseCredentials
        from dbt.adapters.clickhouse.httpclient import ChHttpClient
        database = 'routing_runtime_test_' + uuid.uuid4().hex
        tag = 'rawbbit-dbt:' + str(uuid.uuid4())
        with tempfile.TemporaryDirectory() as temporary:
            requests = Path(temporary)
            os.environ['RAWBBIT_DBT_ATTEMPT_TAG'] = tag
            os.environ['RAWBBIT_DBT_REQUEST_DIR'] = temporary
            install_tracking()
            credentials = ClickHouseCredentials(
                driver='http', host=os.environ.get('CLICKHOUSE_HOST', 'localhost'),
                port=int(os.environ.get('CLICKHOUSE_PORT', '8123')),
                user=os.environ['CLICKHOUSE_DBT_USER'], password=os.environ['CLICKHOUSE_DBT_PASSWORD'],
                schema=database, check_exchange=False, use_lw_deletes=True,
                custom_settings={'log_comment': tag, 'log_queries': 1, 'log_query_settings': 1,
                                 'mutations_sync': 2, 'lightweight_deletes_sync': 2})
            client = ChHttpClient(credentials)
            control = ClickHouse(os.environ, settle_seconds=20)
            try:
                columns = ', '.join('`' + name + '` ' + value for name, value in EXPECTED_COLUMNS.items())
                client.command('CREATE TABLE events (' + columns + ') ENGINE MergeTree '
                               'PARTITION BY toYYYYMM(event_date) '
                               'ORDER BY (app_id, environment, event_name, event_date, user_pseudo_id, event_time)')
                self.assertTrue(control.ready(database))
                client.command("INSERT INTO events (event_id,app_id,event_time,event_date) VALUES "
                               "('e','runner_rawbbit','2026-01-01 00:00:00','2026-01-01')")
                client.columns_in_query('SELECT * FROM events')
                client.query('SELECT * FROM events')
                client.command("DELETE FROM events WHERE app_id = 'runner_rawbbit'")
                self.assertEqual(client.query('SELECT count() FROM events').result_set[0][0], 0)
                settings = client.query("SELECT getSetting('mutations_sync'), "
                                        "getSetting('lightweight_deletes_sync'), getSetting('log_comment')").result_set[0]
                self.assertEqual(tuple(map(str, settings)), ('2', '2', tag))
                self.assertTrue(control.settle(tag, database, requests))
                # Flush via independent, untagged test-setup HTTP request. It is
                # not production behavior or a permission requirement of runner.
                request = urllib.request.Request(control.url + '/?' + urllib.parse.urlencode({'query_id': str(uuid.uuid4())}),
                                                  data=b'SYSTEM FLUSH LOGS', method='POST',
                                                  headers={'X-ClickHouse-User': control.user,
                                                           'X-ClickHouse-Key': control.password})
                with control.opener.open(request, timeout=10) as response:
                    response.read()
                audit_records = [load(path) for path in requests.glob('*.json')]
                audit_ids = {row['query_id'] for row in audit_records}
                for record in audit_records:
                    self.assertEqual(record['transport_settings'],
                                     {'log_comment': tag, 'mutations_sync': '2', 'lightweight_deletes_sync': '2'})
                rows = control.query('SELECT query_id, query, Settings FROM system.query_log WHERE log_comment = '
                                     + literal(tag) + " AND type = 'QueryFinish'")
                server_ids = {row['query_id'] for row in rows}
                self.assertTrue(audit_ids <= server_ids, 'client IDs must exactly match server IDs, including metadata')
                writes = [row for row in rows if row['query'].lstrip().upper().startswith(('CREATE', 'INSERT', 'DELETE'))]
                self.assertGreaterEqual(len(writes), 4)  # ensure_database + table + insert + delete
                for row in writes:
                    self.assertEqual(row['Settings']['log_comment'], tag)
                    self.assertEqual(row['Settings']['mutations_sync'], '2')
                    # 24.8 logs changed settings only; lightweight_deletes_sync
                    # defaults to 2. Its actual HTTP URL value is asserted above.
                    self.assertEqual(row['Settings'].get('lightweight_deletes_sync', '2'), '2')
            finally:
                client.command('DROP DATABASE IF EXISTS `' + database + '`')
                client.close()

    def test_killed_writer_and_denied_terminal_proof_persist_real_fence(self):
        database = 'routing_runtime_test_' + uuid.uuid4().hex
        user = 'routing_runtime_test_' + uuid.uuid4().hex
        admin = ClickHouse(os.environ)
        self.setup_sql('CREATE DATABASE `' + database + '`')
        try:
            columns = ', '.join('`' + name + '` ' + value for name, value in EXPECTED_COLUMNS.items())
            self.setup_sql('CREATE TABLE `' + database + '`.events (' + columns + ') ENGINE MergeTree '
                           'PARTITION BY toYYYYMM(event_date) '
                           'ORDER BY (app_id, environment, event_name, event_date, user_pseudo_id, event_time)')
            self.setup_sql('CREATE USER `' + user + "` IDENTIFIED WITH plaintext_password BY 'test-only'")
            self.setup_sql('GRANT SELECT, INSERT ON `' + database + '`.events TO `' + user + '`')
            self.setup_sql('GRANT SELECT ON system.processes TO `' + user + '`')
            self.setup_sql('GRANT SELECT ON system.mutations TO `' + user + '`')
            with tempfile.TemporaryDirectory() as temporary:
                env = dict(os.environ, CLICKHOUSE_DBT_USER=user, CLICKHOUSE_DBT_PASSWORD='test-only',
                           CLICKHOUSE_DATABASE=database, RAWBBIT_DBT_RUNTIME_DIR=temporary,
                           RAWBBIT_DBT_ROUTING_ENABLED='0', RAWBBIT_RAW_LOAD_MODE='dbt',
                           RAWBBIT_DBT_SETTLE_TIMEOUT_SECONDS='1')
                denied = ClickHouse(env)
                with self.assertRaises(ControlError):
                    denied.query('SELECT count() FROM system.query_log')
                test = self

                class KilledWriter(Runner):
                    def build(self, record, variables, request_dir):
                        child_env = dict(env, RAWBBIT_DBT_ATTEMPT_TAG=record['tag'],
                                         RAWBBIT_DBT_REQUEST_DIR=str(request_dir))
                        script = """
import os,sys
sys.path.insert(0,'/app/runner')
from dbt_child import install_tracking
install_tracking()
from dbt.adapters.clickhouse.credentials import ClickHouseCredentials
from dbt.adapters.clickhouse.httpclient import ChHttpClient
c=ChHttpClient(ClickHouseCredentials(driver='http',host=os.environ['CLICKHOUSE_HOST'],port=8123,
user=os.environ['CLICKHOUSE_DBT_USER'],password=os.environ['CLICKHOUSE_DBT_PASSWORD'],
schema=os.environ['CLICKHOUSE_DATABASE'],check_exchange=False,
custom_settings={'log_comment':os.environ['RAWBBIT_DBT_ATTEMPT_TAG'],'log_queries':1,'log_query_settings':1,
'mutations_sync':2,'lightweight_deletes_sync':2}))
c.command("INSERT INTO events (event_id,app_id,event_time,event_date) SELECT toString(number), 'runner_rawbbit', toDateTime64('2026-01-01 00:00:00',3,'UTC'), toDate('2026-01-01') FROM numbers(20) WHERE sleepEachRow(0.1)=0")
"""
                        child = subprocess.Popen([sys.executable, '-c', script], env=child_env,
                                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                        try:
                            deadline = time.monotonic() + 10
                            while time.monotonic() < deadline:
                                rows = admin.query('SELECT query_id FROM system.processes WHERE user = '
                                                   + literal(user) + ' AND Settings[\'log_comment\'] = '
                                                   + literal(record['tag']) + " AND startsWith(query, 'INSERT')")
                                if rows:
                                    child.kill()
                                    child.wait(timeout=5)
                                    return 'signal_exit'
                                test.assertIsNone(child.poll(), 'writer must reach ClickHouse before being killed')
                                time.sleep(0.05)
                            test.fail('writer did not reach ClickHouse')
                        finally:
                            if child.poll() is None:
                                child.kill()
                                child.wait(timeout=5)

                job = KilledWriter(env)
                with contextlib.redirect_stdout(io.StringIO()):
                    code = job.run(['hourly'])
                self.assertEqual(code, 4)
                self.assertTrue(job.fence.exists())
                self.assertEqual(job.job['outcomes'][0]['status'], 'unknown')
                self.assertTrue(any(load(path)['status'] == 'dispatched'
                                    for path in (Path(temporary) / 'requests').rglob('*.json')))
                with contextlib.redirect_stdout(io.StringIO()):
                    next_job = Runner(env)
                    self.assertEqual(next_job.run(['hourly']), 4)
                self.assertEqual(next_job.job['status'], 'fenced')
        finally:
            self.setup_sql('DROP DATABASE IF EXISTS `' + database + '`')
            self.setup_sql('DROP USER IF EXISTS `' + user + '`')

    def test_pending_server_mutation_blocks_writes_without_a_live_client(self):
        database = 'routing_runtime_test_' + uuid.uuid4().hex
        self.setup_sql('CREATE DATABASE `' + database + '`')
        try:
            columns = ', '.join('`' + name + '` ' + value for name, value in EXPECTED_COLUMNS.items())
            self.setup_sql('CREATE TABLE `' + database + '`.events (' + columns + ') ENGINE MergeTree '
                           'PARTITION BY toYYYYMM(event_date) '
                           'ORDER BY (app_id, environment, event_name, event_date, user_pseudo_id, event_time)')
            self.setup_sql('INSERT INTO `' + database + "`.events (event_id,app_id,event_time,event_date) VALUES "
                           "('pending','runner_rawbbit','2026-01-01 00:00:00','2026-01-01')")
            self.setup_sql('SYSTEM STOP MERGES `' + database + '`.events')
            self.setup_sql('ALTER TABLE `' + database + "`.events DELETE WHERE event_id = 'pending' SETTINGS mutations_sync=0")
            control = ClickHouse(os.environ, settle_seconds=1)
            self.assertTrue(control.query('SELECT mutation_id FROM system.mutations WHERE database = '
                                          + literal(database) + ' AND NOT is_done'))
            with tempfile.TemporaryDirectory() as temporary:
                env = dict(os.environ, CLICKHOUSE_DATABASE=database,
                           RAWBBIT_DBT_RUNTIME_DIR=temporary, RAWBBIT_DBT_ROUTING_ENABLED='0',
                           RAWBBIT_RAW_LOAD_MODE='dbt', RAWBBIT_DBT_SETTLE_TIMEOUT_SECONDS='1')
                job = Runner(env)
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(job.run(['hourly']), 4)
                self.assertTrue(job.fence.exists())
                self.assertEqual(job.job['outcomes'], [])
        finally:
            self.setup_sql('SYSTEM START MERGES `' + database + '`.events')
            self.setup_sql('DROP DATABASE IF EXISTS `' + database + '`')


if __name__ == '__main__':
    unittest.main()
