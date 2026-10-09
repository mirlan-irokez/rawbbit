"""Stdlib runner/control tests: python -m unittest discover -s dbt_project/tests -p 'test_*.py'."""
import contextlib
import datetime as dt
import fcntl
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'runner'))
from config import ConfigError, snapshot
from clickhouse import ClickHouse, ControlError, EXPECTED_COLUMNS
from runner import Runner, windows
from storage import load, save
from progress import FD_ENV, encode


class FakeControl:
    def __init__(self, *args):
        self.missing = set()
        self.safe = True
        self.settled = []

    def ready(self, database):
        return database not in self.missing

    def settle(self, tag, database, directory):
        self.settled.append((tag, database))
        return self.safe


class Harness(Runner):
    def __init__(self, env):
        self.fake = FakeControl()
        super().__init__(env, control_factory=lambda *args: self.fake)
        self.calls = []
        self.results = []
        self.on_build = None

    def build(self, record, vars_value, request_dir):
        self.calls.append((dict(record), vars_value))
        if self.on_build:
            self.on_build(self)
        return self.results.pop(0) if self.results else 'success'


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.routes = self.root / 'dbt-routes.json'
        self.env = {'RAWBBIT_DBT_RUNTIME_DIR': str(self.root), 'RAWBBIT_RAW_LOAD_MODE': 'dbt',
                    'RAWBBIT_DBT_ROUTES_FILE': str(self.routes), 'CLICKHOUSE_DATABASE': 'main_custom',
                    'RAWBBIT_DBT_ROUTING_ENABLED': '1', 'CLICKHOUSE_DBT_USER': 'loader',
                    'CLICKHOUSE_DBT_PASSWORD': 'SECRET-NOT-IN-LEDGER'}
        self.write_routes([('a', 'team_a'), ('b', 'team_b')])

    def write_routes(self, routes):
        save(self.routes, {'version': 1, 'routes': [dict(app_id=app, dataset_id=database,
                                                      database=database, table='events')
                                                 for app, database in routes]})

    def run_job(self, harness=None, args=None):
        harness = harness or Harness(self.env)
        with contextlib.redirect_stdout(io.StringIO()):
            code = harness.run(args or ['hourly'])
        self.assertNotIn('SECRET-NOT-IN-LEDGER', harness.job_path.read_text())
        return harness, code

    def test_disabled_ignores_missing_or_bad_snapshot(self):
        self.env['RAWBBIT_DBT_ROUTING_ENABLED'] = '0'
        self.routes.unlink()
        job, code = self.run_job()
        self.assertEqual(code, 0)
        self.assertEqual(len(job.calls), 1)
        self.assertEqual(job.calls[0][1]['rawbbit_excluded_app_ids'], [])

    def test_success_default_then_routes(self):
        job, code = self.run_job()
        self.assertEqual(code, 0)
        self.assertEqual([call[0]['kind'] for call in job.calls], ['default', 'primary', 'primary'])
        self.assertEqual(job.calls[0][1]['rawbbit_excluded_app_ids'], ['a', 'b'])
        self.assertEqual(job.calls[1][1]['rawbbit_app_id'], 'a')
        self.assertEqual(job.calls[1][1]['rawbbit_excluded_app_ids'], [])

    def test_readiness_fallback_includes_only_same_app_and_window(self):
        harness = Harness(self.env)
        harness.fake.missing.add('team_a')
        job, code = self.run_job(harness)
        self.assertEqual(code, 2)
        self.assertEqual([call[0]['kind'] for call in job.calls], ['default', 'fallback', 'primary'])
        fallback = job.calls[1]
        self.assertEqual(fallback[0]['actual_database'], 'main_custom')
        self.assertEqual(fallback[0]['intended_database'], 'team_a')
        self.assertEqual(fallback[1]['rawbbit_app_id'], 'a')
        self.assertEqual(fallback[1]['rawbbit_excluded_app_ids'], [])
        self.assertEqual(fallback[1]['rawbbit_window_start'], job.calls[0][1]['rawbbit_window_start'])
        self.assertEqual(job.job['outcomes'][1]['status'], 'failed')

    def test_default_primary_fallback_failure_do_not_stop_healthy_routes(self):
        harness = Harness(self.env)
        harness.results = ['dbt_failed', 'dbt_failed', 'dbt_failed', 'success']
        job, code = self.run_job(harness)
        self.assertEqual(code, 1)
        self.assertEqual([call[0]['kind'] for call in job.calls], ['default', 'primary', 'fallback', 'primary'])
        self.assertEqual(len(job.fake.settled), 5)  # preflight plus every writer

    def test_partial_primary_failure_can_fallback_after_server_confirmation(self):
        harness = Harness(self.env)
        harness.results = ['success', 'dbt_failed', 'success', 'success']
        job, code = self.run_job(harness)
        self.assertEqual(code, 2)
        primary = load(self.root / 'attempts' / (job.job['outcomes'][1]['attempt_id'] + '.json'))
        self.assertTrue(primary['partial_write_possible'])

    def test_timeout_with_proven_completion_can_fallback(self):
        harness = Harness(self.env)
        harness.results = ['success', 'timeout', 'success', 'success']
        job, code = self.run_job(harness)
        self.assertEqual(code, 2)
        self.assertFalse(job.fence.exists())

    def test_unknown_server_work_fences_before_fallback_and_future_jobs(self):
        harness = Harness(self.env)
        harness.on_build = lambda job: setattr(job.fake, 'safe', False)
        job, code = self.run_job(harness)
        self.assertEqual(code, 4)
        self.assertEqual(len(job.calls), 1)
        self.assertTrue(job.fence.exists())
        next_job, code = self.run_job()
        self.assertEqual(code, 4)
        self.assertEqual(next_job.calls, [])

    def test_unowned_work_preflight_fences_without_writes(self):
        harness = Harness(self.env)
        harness.fake.safe = False
        job, code = self.run_job(harness)
        self.assertEqual(code, 4)
        self.assertEqual(job.calls, [])

    def test_signal_abnormal_exit_confirms_server_then_stops(self):
        harness = Harness(self.env)
        harness.results = ['signal_exit']
        job, code = self.run_job(harness)
        self.assertEqual(code, 4)
        self.assertEqual(len(job.fake.settled), 2)
        self.assertEqual(len(job.calls), 1)

    def test_signaled_parent_confirms_server_work(self):
        harness = Harness(self.env)
        harness.on_build = lambda job: job.signal()
        job, code = self.run_job(harness)
        self.assertEqual(code, 4)
        self.assertEqual(len(job.fake.settled), 2)

    def test_crash_nonterminal_intent_survives_and_fences(self):
        path = self.root / 'attempts' / 'interrupted.json'
        save(path, {'status': 'running', 'attempt_id': 'old'})
        job, code = self.run_job()
        self.assertEqual(code, 4)
        self.assertEqual(load(path)['status'], 'unknown')
        self.assertEqual(job.calls, [])
        self.assertTrue(job.fence.exists())

    def test_interrupted_job_with_terminal_writes_is_durably_reported(self):
        path = self.root / 'jobs' / 'old.json'
        save(path, {'job_id': 'old', 'status': 'running', 'windows': [['start', 'end']]})
        job, code = self.run_job()
        self.assertEqual(code, 0)
        self.assertEqual(load(path)['status'], 'unknown')
        self.assertEqual(load(path)['exit_code'], 4)

    def test_real_process_path_timeout_tracks_intent_and_confirms_server(self):
        class Child:
            pid = 123
            returncode = None
            def poll(self):
                return self.returncode
            def wait(self, timeout):
                self.returncode = -15
                return self.returncode
        child = Child()
        harness = Harness(self.env)
        self.addCleanup(harness.close_progress)
        harness.snap = snapshot(self.env)
        harness.control = harness.fake
        harness.deadline = 100
        harness.attempt_seconds = 1
        envs = []
        def factory(args, **kwargs):
            records = [load(path) for path in (self.root / 'attempts').glob('*.json')]
            self.assertEqual(records[0]['status'], 'running')
            envs.append(kwargs['env'])
            return child
        harness.process_factory = factory
        harness.build = lambda *args: Runner.build(harness, *args)
        with patch('runner.time.monotonic', side_effect=[0, 2]), patch('runner.os.killpg') as kill:
            result = harness.attempt('default', None, 'main_custom', 'main_custom', ('start', 'end'), [])
        self.assertEqual(result, 'timeout')
        kill.assert_called_once()
        self.assertEqual(len(harness.fake.settled), 1)
        self.assertEqual(envs[0]['DBT_LOG_LEVEL_FILE'], 'none')
        self.assertEqual(envs[0]['DBT_WRITE_JSON'], 'false')

    def test_cannot_stop_child_must_fence_even_when_server_looks_quiet(self):
        class Child:
            def poll(self):
                return None
        harness = Harness(self.env)
        self.addCleanup(harness.close_progress)
        harness.snap = snapshot(self.env)
        harness.control = harness.fake
        harness.active = Child()
        harness.build = lambda *args: (_ for _ in ()).throw(RuntimeError('failure'))
        harness.stop_child = lambda *args: (_ for _ in ()).throw(RuntimeError('stop failure'))
        result = harness.attempt('default', None, 'main_custom', 'main_custom', ('start', 'end'), [])
        self.assertEqual(result, 'unknown')
        self.assertTrue(harness.fence.exists())
        self.assertEqual(harness.fake.settled, [])

    def process_harness(self, source):
        harness = Harness(self.env)
        self.addCleanup(harness.close_progress)
        harness.snap = snapshot(self.env)
        harness.control = harness.fake
        harness.deadline = time.monotonic() + 5
        harness.attempt_seconds = 2
        harness.build = lambda *args: Runner.build(harness, *args)
        calls = []
        def factory(args, **kwargs):
            calls.append(kwargs)
            self.assertEqual(kwargs['stdout'], subprocess.DEVNULL)
            self.assertEqual(kwargs['stderr'], subprocess.DEVNULL)
            self.assertTrue(kwargs['start_new_session'])
            self.assertEqual(kwargs['env']['DBT_LOG_LEVEL_FILE'], 'none')
            self.assertEqual(kwargs['env']['DBT_WRITE_JSON'], 'false')
            return subprocess.Popen([sys.executable, '-c', source], **kwargs)
        harness.process_factory = factory
        return harness, calls

    def test_progress_setup_failures_run_original_child_once_strip_stale_fd(self):
        for target in ('runner.Channel', 'runner.Emitter'):
            with self.subTest(target=target):
                self.env[FD_ENV] = '1'
                harness, calls = self.process_harness('import sys;sys.exit(0)')
                with patch(target, side_effect=OSError('SECRET-NOT-IN-LEDGER')):
                    result = harness.attempt('default', None, 'main_custom', 'main_custom', ('start', 'end'), [])
                self.assertEqual(result, 'success')
                self.assertEqual(len(calls), 1)
                self.assertNotIn(FD_ENV, calls[0]['env'])
                self.assertNotIn('pass_fds', calls[0])
                self.assertEqual(len(harness.fake.settled), 1)

    def test_launched_process_never_replayed_progress_drain_failures_contained(self):
        harness, calls = self.process_harness('import time;time.sleep(.2)')
        with patch('progress.Channel.drain', side_effect=OSError):
            result = harness.attempt('default', None, 'main_custom', 'main_custom', ('start', 'end'), [])
        self.assertEqual(result, 'success')
        self.assertEqual(len(calls), 1)
        for fd in calls[0]['pass_fds']:
            with self.assertRaises(OSError):
                os.fstat(fd)

    def test_generic_failure_after_durable_outcome_retains_observed_node(self):
        safe_frame = {'v': 1, 'seq': 1, 'event': 'start', 'resource': 'model',
                      'node': 'rawbbit_events_load', 'status': 'START'}
        source = f'''import os,sys,time
os.write(int(os.environ[{FD_ENV!r}]), {encode(safe_frame)!r})
print('SECRET-NOT-IN-LEDGER password sql',file=sys.stderr,flush=True)
time.sleep(.2)
sys.exit(1)
'''
        harness, calls = self.process_harness(source)
        lines = []
        class Sink:
            def emit(_, line):
                if 'END attempt' in line or 'FAIL dbt' in line:
                    record = load(next((self.root / 'attempts').glob('*.json')))
                    self.assertEqual(record['status'], 'failed')
                lines.append(line)
            def close(_):
                pass
        with patch('runner.Emitter', return_value=Sink()):
            result = harness.attempt('default', None, 'main_custom', 'main_custom', ('start', 'end'), [])
        self.assertEqual(result, 'dbt_failed')
        self.assertEqual(len(calls), 1)
        text = '\n'.join(lines)
        self.assertIn('START model rawbbit_events_load', text)
        self.assertIn('category=execution_failed last_phase=model last_node=rawbbit_events_load', text)
        self.assertNotIn('SECRET', text)
        self.assertLess(text.index('VERIFY attempt'), text.index('END attempt'))
        record = load(next((self.root / 'attempts').glob('*.json')))
        self.assertNotIn('last_phase', record)
        self.assertNotIn('progress', record)

    def test_callback_silent_failure_is_before_first_node_not_parse_guess(self):
        harness, _ = self.process_harness('import sys;sys.exit(1)')
        lines = []
        with patch('runner.Emitter') as emitter:
            emitter.return_value.emit.side_effect = lines.append
            result = harness.attempt('default', None, 'main_custom', 'main_custom', ('start', 'end'), [])
        self.assertEqual(result, 'dbt_failed')
        self.assertIn('category=execution_failed last_phase=before_first_node', '\n'.join(lines))
        self.assertNotIn('parse', '\n'.join(lines))

    def test_flooded_progress_timeout_signal_and_closed_sink_do_not_change_settlement(self):
        source = f'''import os
fd=int(os.environ[{FD_ENV!r}])
while True:
 try:os.write(fd,b'x'*512)
 except (BlockingIOError,BrokenPipeError):pass
'''
        for interrupted in (False, True):
            harness, calls = self.process_harness(source)
            harness.attempt_seconds = .2
            harness.interrupted = interrupted
            started = time.monotonic()
            with patch('progress.os.write', side_effect=BrokenPipeError):
                result = harness.attempt('default', None, 'main_custom', 'main_custom', ('start', 'end'), [])
            self.assertEqual(result, 'interrupted' if interrupted else 'timeout')
            self.assertLess(time.monotonic() - started, 2)
            self.assertEqual(len(harness.fake.settled), 1)
            self.assertFalse(harness.fence.exists())
            for fd in calls[0]['pass_fds']:
                with self.assertRaises(OSError):
                    os.fstat(fd)

    def test_stdout_summary_and_ledger_keys_unchanged_with_progress(self):
        harness = Harness(self.env)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(harness.run(['hourly']), 0)
        summary = json.loads(output.getvalue())
        self.assertEqual(set(summary), {'job_id', 'status', 'exit_code', 'revision', 'outcomes'})
        for outcome in summary['outcomes']:
            self.assertEqual(set(outcome), {'attempt_id', 'kind', 'app_id', 'database',
                                           'window_start', 'window_end', 'status'})
        for path in self.root.rglob('*.json'):
            self.assertNotIn('SECRET-NOT-IN-LEDGER', path.read_text())

    def test_single_lazy_emitter_per_invocation_and_bounded_cleanup(self):
        with patch('runner.Emitter') as emitter:
            harness, code = self.run_job()
        self.assertEqual(code, 0)
        emitter.assert_called_once_with()
        emitter.return_value.close.assert_called_once_with()

    def test_gate_before_lock(self):
        (self.root / 'deploy.gate').touch()
        job, code = self.run_job()
        self.assertEqual(code, 4)
        self.assertEqual(job.job['status'], 'gated')
        self.assertFalse(job.lock.exists())

    def test_gate_after_lock(self):
        original = fcntl.flock
        def gate(fd, operation):
            original(fd, operation)
            (self.root / 'deploy.gate').touch()
        with patch('runner.fcntl.flock', side_effect=gate):
            job, code = self.run_job()
        self.assertEqual(code, 4)
        self.assertEqual(job.calls, [])

    def test_scheduled_lock_skip_is_observable_nonzero(self):
        with (self.root / 'pipeline.lock').open('a') as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            job, code = self.run_job()
        self.assertEqual(code, 4)
        self.assertEqual(job.job['status'], 'lock_skipped')

    def test_frozen_snapshot_across_backfill_chunks(self):
        harness = Harness(self.env)
        harness.on_build = lambda job: self.write_routes([('c', 'team_c')])
        job, code = self.run_job(harness, ['backfill', '2026-01-01T00:00:00Z', '2026-01-03T00:00:00Z'])
        self.assertEqual(code, 0)
        self.assertEqual(len(job.calls), 6)
        self.assertEqual({call[0]['revision'] for call in job.calls}, {job.snap.revision})
        self.assertEqual([call[0]['app_id'] for call in job.calls], [None, 'a', 'b', None, 'a', 'b'])

    def test_continue_backfill_after_failure(self):
        harness = Harness(self.env)
        harness.results = ['dbt_failed']
        job, code = self.run_job(harness, ['backfill', '2026-01-01T00:00:00Z', '2026-01-03T00:00:00Z'])
        self.assertEqual(code, 1)
        self.assertEqual(len(job.calls), 6)

    def test_last_route_removal_restores_default(self):
        self.write_routes([])
        job, code = self.run_job()
        self.assertEqual(code, 0)
        self.assertEqual(len(job.calls), 1)
        self.assertEqual(job.calls[0][1]['rawbbit_excluded_app_ids'], [])

    def test_invalid_enabled_configs_no_writes(self):
        cases = [None, '{', '{"version":2,"routes":[]}', '{"version":true,"routes":[]}',
                 '{"version":1,"version":1,"routes":[]}']
        for raw in cases:
            with self.subTest(raw=raw):
                self.routes.unlink(missing_ok=True)
                if raw is not None:
                    self.routes.write_text(raw)
                job, code = self.run_job()
                self.assertEqual(code, 3)
                self.assertEqual(job.calls, [])

    def test_conflicts_and_unsafe_fields(self):
        for routes in [[('a', 'x'), ('a', 'y')], [('a', 'x'), ('b', 'x')],
                       [('a', 'main_custom')], [('../a', 'x')], [('a*', 'x')],
                       [("a'", 'x')], [('a', 'x.y')]]:
            self.write_routes(routes)
            job, code = self.run_job()
            self.assertEqual(code, 3)
            self.assertEqual(job.calls, [])

    def test_invalid_snapshot_encoding_is_config_error_without_fence(self):
        self.routes.write_bytes(b'\xff\xfe')
        job, code = self.run_job()
        self.assertEqual(code, 3)
        self.assertEqual(job.calls, [])
        self.assertFalse(job.fence.exists())

    def test_routing_legacy_incompatible_and_disabled_mode_observable(self):
        self.env['RAWBBIT_RAW_LOAD_MODE'] = 'legacy'
        job, code = self.run_job()
        self.assertEqual(code, 3)
        self.env['RAWBBIT_DBT_ROUTING_ENABLED'] = '0'
        job, code = self.run_job()
        self.assertEqual(code, 4)
        self.assertEqual(job.job['status'], 'mode_disabled')

    def test_capabilities_and_validate_have_no_lock_ledger_or_credentials_requirement(self):
        script = Path(__file__).resolve().parents[1] / 'runner' / 'runner.py'
        before = sorted(self.root.iterdir())
        for args in [['deployment-contract'], ['capabilities'], ['--capabilities'], ['validate-config']]:
            result = subprocess.run([sys.executable, str(script)] + args,
                                    env=self.env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)['version'], 1)
        self.assertEqual(before, sorted(self.root.iterdir()))

    def test_hourly_daily_backfill_bounds(self):
        now = dt.datetime(2026, 1, 4, 5, 59, tzinfo=dt.timezone.utc)
        self.assertEqual(windows(['hourly'], {}, now), [('2026-01-04T02:00:00Z', '2026-01-04T05:00:00Z')])
        self.assertEqual(windows(['daily'], {}, now), [('2026-01-01T00:00:00Z', '2026-01-04T00:00:00Z')])
        for args in [['backfill', '2026-02-30T00:00:00Z', '2026-03-01T00:00:00Z'],
                     ['backfill', '2026-01-01T00:01:00Z', '2026-01-02T00:00:00Z']]:
            with self.assertRaises(ConfigError):
                windows(args, {})


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.control = ClickHouse({'CLICKHOUSE_DBT_USER': 'loader', 'CLICKHOUSE_DBT_PASSWORD': 'secret'},
                                  settle_seconds=0)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.requests = Path(self.temp.name)
        self.sql = []

    def fake(self, sql):
        self.sql.append(sql)
        return []

    def test_control_queries_do_not_self_match_and_kill_is_owned_tagged(self):
        calls = iter([[{'query_id': 'owned'}], [], [], [], []])
        def query(sql):
            self.sql.append(sql)
            return next(calls)
        self.control.query = query
        self.assertTrue(self.control.settle('attempt', 'team', self.requests))
        kill = [sql for sql in self.sql if sql.startswith('KILL')][0]
        self.assertIn("user = 'loader'", kill)
        self.assertIn("Settings['log_comment'] = 'attempt'", kill)
        self.assertIn(' SYNC', kill)
        self.assertTrue(any('{control_query_id:String}' in sql for sql in self.sql))

    def test_no_process_is_not_proof_pending_request_completed(self):
        save(self.requests / 'q.json', {'query_id': 'q', 'status': 'dispatched'})
        self.control.query = self.fake
        self.assertFalse(self.control.settle('attempt', 'team', self.requests))
        self.assertTrue(any('system.query_log' in sql for sql in self.sql))

    def test_terminal_log_evidence_allows_failed_http_request(self):
        save(self.requests / 'q.json', {'query_id': 'q', 'status': 'dispatched'})
        self.control.query = lambda sql: [{'query_id': 'q'}] if 'system.query_log' in sql else []
        self.assertTrue(self.control.settle('attempt', 'team', self.requests))

    def test_pending_mutation_or_unowned_query_never_killed_or_treated_safe(self):
        for table in ['system.mutations', 'control_query_id:String']:
            self.sql = []
            def query(sql):
                self.sql.append(sql)
                return [{'query_id': 'unowned'}] if table in sql else []
            self.control.query = query
            self.assertFalse(self.control.settle('attempt', 'team', self.requests))
            self.assertFalse(any(sql.startswith('KILL') for sql in self.sql))

    def test_denied_metadata_or_kill_is_uncertain(self):
        self.control.query = lambda sql: (_ for _ in ()).throw(ControlError('denied'))
        self.assertFalse(self.control.settle('attempt', 'team', self.requests))

    def test_schema_readiness_rejects_incompatible_and_missing(self):
        def query(sql):
            if 'system.tables' in sql:
                return [{'engine': 'MergeTree', 'partition_key': 'toYYYYMM(event_date)',
                         'sorting_key': 'app_id, environment, event_name, event_date, user_pseudo_id, event_time'}]
            if 'system.columns' in sql:
                return [{'name': name, 'type': value} for name, value in EXPECTED_COLUMNS.items()]
            return []
        self.control.query = query
        self.assertTrue(self.control.ready('team'))
        self.control.query = lambda sql: []
        self.assertFalse(self.control.ready('team'))


if __name__ == '__main__':
    unittest.main()
