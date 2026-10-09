"""Availability-first sequential ingestion with persistent ownership and fencing."""
import datetime as dt
import fcntl
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

from clickhouse import ClickHouse
from config import ConfigError, positive, snapshot
from storage import load, save
from progress import Channel, Emitter, FD_ENV, node_body, prefix

UTC = dt.timezone.utc
TERMINAL = {'completed', 'completed_with_fallback', 'failed', 'invalid_enabled_config',
            'unknown', 'gated', 'lock_skipped', 'lock_wait_expired', 'mode_disabled', 'fenced'}

CAPABILITIES = {'version': 1, 'drain_aware': True, 'route_snapshot_version': 1,
                'deploy_gate': '/app/runtime/deploy.gate',
                'pipeline_lock': '/app/runtime/pipeline.lock',
                'write_fence': '/app/runtime/write-fence.json',
                'lock_file': '/app/runtime/pipeline.lock',
                'gate_file': '/app/runtime/deploy.gate',
                'write_fence_file': '/app/runtime/write-fence.json',
                 'commands': ['hourly', 'daily', 'backfill', 'deployment-contract', 'capabilities', '--capabilities', 'validate-config', 'verify-control'],
                'exit_codes': {'completed': 0, 'failed': 1, 'completed_with_fallback': 2,
                               'invalid_enabled_config': 3, 'interrupted_unknown_or_deferred': 4}}


def iso(value):
    return value.strftime('%Y-%m-%dT%H:%M:%SZ')


def timestamp(value):
    try:
        parsed = dt.datetime.strptime(value, '%Y-%m-%dT%H:00:00Z').replace(tzinfo=UTC)
        if iso(parsed) != value:
            raise ValueError()
        return parsed
    except ValueError:
        raise ConfigError('timestamp must be hour-aligned UTC') from None


def windows(args, env, now=None):
    now = now or dt.datetime.now(UTC)
    if args == ['hourly']:
        end = now.replace(minute=0, second=0, microsecond=0)
        start = end - dt.timedelta(hours=positive(env, 'RAWBBIT_DBT_HOURLY_LOOKBACK_HOURS', 3, 168))
        return [(iso(start), iso(end))]
    if args == ['daily']:
        end = now.replace(hour=0, minute=0, second=0, microsecond=0)
        start = end - dt.timedelta(days=positive(env, 'RAWBBIT_DBT_DAILY_LOOKBACK_DAYS', 3, 7))
        return [(iso(start), iso(end))]
    if len(args) != 3 or args[0] != 'backfill':
        raise ConfigError('usage: dbt-job {hourly|daily|backfill START_UTC END_UTC}')
    start, end = timestamp(args[1]), timestamp(args[2])
    if start >= end:
        raise ConfigError('backfill end must be after start')
    chunk = dt.timedelta(hours=positive(env, 'RAWBBIT_DBT_BACKFILL_CHUNK_HOURS', 24, 168))
    if (end - start).total_seconds() > 366 * 86400:
        raise ConfigError('backfill exceeds 366-day bound')
    result = []
    while start < end:
        stop = min(start + chunk, end)
        result.append((iso(start), iso(stop)))
        start = stop
    return result


class Runner:
    def __init__(self, env, control_factory=ClickHouse, process_factory=subprocess.Popen):
        self.env = dict(env)
        self.root = Path(env.get('RAWBBIT_DBT_RUNTIME_DIR', '/app/runtime'))
        self.lock = Path(env.get('RAWBBIT_DBT_LOCK_FILE', str(self.root / 'pipeline.lock')))
        self.gate = self.root / 'deploy.gate'
        self.fence = self.root / 'write-fence.json'
        self.job_id = str(uuid.uuid4())
        self.job_path = self.root / 'jobs' / (self.job_id + '.json')
        self.job = {'version': 1, 'job_id': self.job_id, 'status': 'created',
                    'created_at': iso(dt.datetime.now(UTC)), 'outcomes': []}
        self.interrupted = False
        self.control_factory = control_factory
        self.process_factory = process_factory
        self.active = None
        self.emitter = None
        self.emitter_started = False
        self.last_phase = 'before_first_node'
        self.last_node = None

    def progress(self, record, body):
        try:
            if not self.emitter_started:
                self.emitter_started = True
                self.emitter = Emitter()
            if self.emitter is not None:
                self.emitter.emit(prefix(record) + body)
        except Exception:
            pass

    def drain_progress(self, channel, record):
        try:
            for frame in channel.drain():
                body = node_body(frame)
                if body:
                    self.last_phase, self.last_node = frame['resource'], frame['node']
                    self.progress(record, body)
        except Exception:
            pass

    def close_progress(self):
        try:
            if self.emitter is not None:
                self.emitter.close()
        except Exception:
            pass

    def record(self, status=None):
        if status:
            self.job['status'] = status
        save(self.job_path, self.job)

    def finish(self, status, code):
        self.job['exit_code'] = code
        self.job['finished_at'] = iso(dt.datetime.now(UTC))
        self.record(status)
        print(json.dumps({'job_id': self.job_id, 'status': status, 'exit_code': code,
                          'revision': self.job.get('revision'), 'outcomes': self.job['outcomes']}), flush=True)
        return code

    def signal(self, *_):
        self.interrupted = True

    def set_fence(self, attempt=None, reason='unknown_server_work'):
        # Conservative global fence: even a different app shares dbt scratch objects.
        save(self.fence, {'version': 1, 'job_id': self.job_id,
                          'attempt_id': attempt, 'reason': reason,
                          'operator_review_required': True})

    def recover(self):
        for path in (self.root / 'jobs').glob('*.json'):
            if path == self.job_path:
                continue
            try:
                previous = load(path)
                if previous['status'] == 'running':
                    previous.update(status='unknown', exit_code=4,
                                    error_class='interrupted_job_unloaded_windows_require_review')
                    save(path, previous)
            except Exception:
                self.set_fence(reason='unreadable_job_record')
        for path in (self.root / 'attempts').glob('*.json'):
            try:
                record = load(path)
                if record['status'] in ('intent', 'running'):
                    # Do not auto-cancel/replay a crashed predecessor: preserve its evidence.
                    record['status'] = 'unknown'
                    record['error_class'] = 'interrupted_unknown_partial_write'
                    self.set_fence(record.get('attempt_id'), 'interrupted_intent')
                    save(path, record)
            except Exception:
                self.set_fence(reason='unreadable_attempt_record')
        return self.fence.exists()

    def stop_child(self, child):
        if child.poll() is None:
            os.killpg(child.pid, signal.SIGTERM)
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait(timeout=5)

    def build(self, record, vars_value, request_dir):
        env = dict(self.env, CLICKHOUSE_DATABASE=record['actual_database'],
                   RAWBBIT_DBT_ATTEMPT_TAG=record['tag'],
                   RAWBBIT_DBT_REQUEST_DIR=str(request_dir),
                   DBT_LOG_PATH=str(self.root / 'logs' / record['attempt_id']),
                   DBT_TARGET_PATH=str(self.root / 'target' / record['attempt_id']),
                   DBT_LOG_LEVEL_FILE='none', DBT_WRITE_JSON='false')
        env.pop(FD_ENV, None)  # Never inherit an operator/stale descriptor.
        project = str(Path(__file__).resolve().parents[1])
        args = [sys.executable, str(Path(__file__).with_name('dbt_child.py')), 'build',
                '--project-dir', project, '--profiles-dir', project,
                '--selector', 'rawbbit_ingestion', '--fail-fast', '--no-partial-parse',
                '--vars', json.dumps(vars_value)]
        # dbt errors can embed S3/server credentials: never forward child output.
        # Unique dbt files are chmod-protected; ledger contains only fixed classifications.
        channel = None
        options = {}
        try:
            if self.emitter is None:
                raise ValueError('progress sink unavailable')
            channel = Channel()
            env[FD_ENV] = str(channel.write_fd)
            options['pass_fds'] = (channel.write_fd,)
        except Exception:
            if channel is not None:
                channel.close()
            channel = None
            env.pop(FD_ENV, None)
            options.clear()
        try:
            child = self.process_factory(args, env=env, stdout=subprocess.DEVNULL,
                                         stderr=subprocess.DEVNULL, start_new_session=True, **options)
            self.active = child
            if channel:
                channel.close_writer()
            deadline = min(self.deadline, time.monotonic() + self.attempt_seconds)
            while child.poll() is None:
                # Check control/deadlines BEFORE spending the bounded drain budget.
                if self.interrupted or time.monotonic() >= deadline:
                    self.stop_child(child)
                    if channel:
                        self.drain_progress(channel, record)
                    return 'interrupted' if self.interrupted else 'timeout'
                if channel:
                    self.drain_progress(channel, record)
                time.sleep(0.1)
            if channel:
                self.drain_progress(channel, record)  # Never wait for descendant EOF.
            self.active = None
            return 'success' if child.returncode == 0 else ('signal_exit' if child.returncode < 0 else 'dbt_failed')
        finally:
            if channel:
                channel.close()

    def attempt(self, kind, app, intended, actual, window, excluded, readiness=False):
        attempt_id = str(uuid.uuid4())
        record = {'version': 1, 'job_id': self.job_id, 'attempt_id': attempt_id,
                  'tag': 'rawbbit-dbt:' + attempt_id, 'revision': self.snap.revision,
                  'kind': kind, 'app_id': app, 'intended_database': intended,
                  'actual_database': actual, 'table': 'events', 'window_start': window[0],
                  'window_end': window[1], 'status': 'intent', 'partial_write_possible': False}
        path = self.root / 'attempts' / (attempt_id + '.json')
        request_dir = self.root / 'requests' / attempt_id
        request_dir.mkdir(parents=True)
        save(path, record)  # Intent must reach persistent storage BEFORE any adapter call.
        self.job['outcomes'].append({'attempt_id': attempt_id, 'kind': kind, 'app_id': app,
                                     'database': actual, 'window_start': window[0],
                                     'window_end': window[1], 'status': 'intent'})
        self.record()
        self.last_phase, self.last_node = 'before_first_node', None
        self.progress(record, 'START attempt')
        result = 'readiness_failed'
        dbt_failed = False
        if not readiness or self.control.ready(actual):
            record['status'] = 'running'
            record['partial_write_possible'] = True
            save(path, record)
            try:
                result = self.build(record, {'rawbbit_window_start': window[0],
                                            'rawbbit_window_end': window[1],
                                            'rawbbit_app_id': app,
                                            'rawbbit_excluded_app_ids': excluded}, request_dir)
            except Exception:
                if self.active is not None:
                    try:
                        self.stop_child(self.active)
                    except Exception:
                        pass
                result = 'process_error'
            dbt_failed = result == 'dbt_failed'
            # Always confirm server quiescence, even on exit 0/test failure.
            self.progress(record, 'VERIFY attempt phase=server_settlement')
            child_live = self.active is not None and self.active.poll() is None
            if child_live or not self.control.settle(record['tag'], actual, request_dir):
                self.set_fence(attempt_id)
                result = 'unknown'
        record['status'] = 'completed' if result == 'success' else ('unknown' if result == 'unknown' else 'failed')
        record['error_class'] = None if result == 'success' else result
        record['finished_at'] = iso(dt.datetime.now(UTC))
        save(path, record)
        self.job['outcomes'][-1]['status'] = record['status']
        self.record()
        if dbt_failed:
            body = 'FAIL dbt category=execution_failed last_phase=' + self.last_phase
            if self.last_node:
                body += ' last_node=' + self.last_node
            self.progress(record, body)
        self.progress(record, 'END attempt status=' + record['status'] + ' outcome=' + result)
        return result

    def run_locked(self, args):
        if self.gate.exists():
            return self.finish('gated', 4)
        if self.recover():
            return self.finish('fenced', 4)
        self.snap = snapshot(self.env)
        chunks = windows(args, self.env)
        if self.env.get('RAWBBIT_RAW_LOAD_MODE', 'legacy') == 'legacy':
            if args[0] == 'backfill':
                raise ConfigError('dbt backfill requires dbt mode')
            return self.finish('mode_disabled', 4)
        if self.env.get('RAWBBIT_RAW_LOAD_MODE') != 'dbt':
            raise ConfigError('RAWBBIT_RAW_LOAD_MODE must be dbt or legacy')
        self.attempt_seconds = positive(self.env, 'RAWBBIT_DBT_ATTEMPT_TIMEOUT_SECONDS', 900, 7200)
        budget = positive(self.env, 'RAWBBIT_DBT_JOB_TIMEOUT_SECONDS', 21600, 172800)
        http = positive(self.env, 'RAWBBIT_DBT_CONTROL_TIMEOUT_SECONDS', 10, 60)
        settle = positive(self.env, 'RAWBBIT_DBT_SETTLE_TIMEOUT_SECONDS', 30, 300)
        self.control = self.control_factory(self.env, http, settle)
        self.deadline = time.monotonic() + budget
        self.job.update(revision=self.snap.revision, snapshot=self.snap.document,
                        command=args[0], windows=chunks)
        self.record('running')
        # Read-only preflight catches unowned loader activity/mutations before
        # the first new write; a single fenced writer is safer than overlap.
        preflight_dir = self.root / 'requests' / ('preflight-' + self.job_id)
        preflight_dir.mkdir(parents=True)
        databases = [self.snap.default] + [row['database'] for row in self.snap.routes]
        if not self.control.settle('rawbbit-dbt:preflight-' + self.job_id, databases, preflight_dir):
            self.set_fence(reason='preexisting_loader_work')
            return self.finish('fenced', 4)
        failed, fallback = False, False
        excluded = [row['app_id'] for row in self.snap.routes]
        for window in chunks:
            if self.interrupted or time.monotonic() >= self.deadline:
                return self.finish('unknown', 4)
            result = self.attempt('default', None, self.snap.default, self.snap.default, window, excluded)
            if result == 'unknown' or self.interrupted or result == 'signal_exit':
                return self.finish('unknown', 4)
            failed |= result != 'success'
            for route in self.snap.routes:
                if self.interrupted or time.monotonic() >= self.deadline:
                    return self.finish('unknown', 4)
                result = self.attempt('primary', route['app_id'], route['database'], route['database'],
                                      window, [], readiness=True)
                if result == 'unknown' or self.interrupted or result == 'signal_exit':
                    return self.finish('unknown', 4)
                if result != 'success':
                    if time.monotonic() >= self.deadline:
                        return self.finish('unknown', 4)
                    result = self.attempt('fallback', route['app_id'], route['database'], self.snap.default,
                                          window, [])
                    if result == 'unknown' or self.interrupted or result == 'signal_exit':
                        return self.finish('unknown', 4)
                    fallback |= result == 'success'
                    failed |= result != 'success'
        return self.finish('failed' if failed else ('completed_with_fallback' if fallback else 'completed'),
                           1 if failed else (2 if fallback else 0))

    def run(self, args):
        try:
            return self._run(args)
        finally:
            self.close_progress()

    def _run(self, args):
        self.root.mkdir(parents=True, exist_ok=True)
        self.record()
        if self.gate.exists():
            return self.finish('gated', 4)
        self.lock.parent.mkdir(parents=True, exist_ok=True)
        wait = positive(self.env, 'RAWBBIT_DBT_LOCK_WAIT_SECONDS', 300, 7200)
        with self.lock.open('a') as lock:
            deadline = time.monotonic() + (wait if args and args[0] == 'backfill' else 0)
            while True:
                if self.gate.exists():
                    return self.finish('gated', 4)
                if self.interrupted:
                    return self.finish('unknown', 4)
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        return self.finish('lock_wait_expired' if args and args[0] == 'backfill' else 'lock_skipped', 4)
                    time.sleep(0.1)
            try:
                return self.run_locked(args)
            except ConfigError as exc:
                self.job['error_class'] = str(exc)  # Only controlled public validation messages.
                return self.finish('invalid_enabled_config', 3)
            except Exception:
                # Keep nonterminal intent and fence on unexpected ledger/control errors.
                self.set_fence(reason='runtime_error')
                return self.finish('unknown', 4)


def main():
    os.umask(0o077)
    if sys.argv[1:] in (['--capabilities'], ['capabilities'], ['deployment-contract']):
        capabilities = dict(CAPABILITIES)
        root = Path(os.environ.get('RAWBBIT_DBT_RUNTIME_DIR', '/app/runtime'))
        lock = os.environ.get('RAWBBIT_DBT_LOCK_FILE', str(root / 'pipeline.lock'))
        capabilities.update(lock_file=lock, pipeline_lock=lock,
                            gate_file=str(root / 'deploy.gate'), deploy_gate=str(root / 'deploy.gate'),
                            write_fence_file=str(root / 'write-fence.json'), write_fence=str(root / 'write-fence.json'))
        print(json.dumps(capabilities, sort_keys=True))
        return 0
    if sys.argv[1:] == ['validate-config']:
        try:
            snap = snapshot(os.environ)
            mode = os.environ.get('RAWBBIT_RAW_LOAD_MODE', 'legacy')
            if mode not in ('legacy', 'dbt'):
                raise ConfigError('invalid raw load mode')
            print(json.dumps({'version': 1, 'status': 'valid', 'revision': snap.revision,
                              'snapshot': snap.document}, sort_keys=True))
            return 0
        except ConfigError:
            print('{"version":1,"status":"invalid_enabled_config"}')
            return 3
    if sys.argv[1:] == ['verify-control']:
        try:
            control = ClickHouse(os.environ)
            for table in ('processes', 'mutations', 'query_log', 'tables', 'columns'):
                control.query('SELECT * FROM system.' + table + ' LIMIT 0')
            print('{"version":1,"status":"control_access_verified"}')
            return 0
        except Exception:
            print('{"version":1,"status":"control_access_unverified"}')
            return 4
    runner = Runner(os.environ)
    signal.signal(signal.SIGTERM, runner.signal)
    signal.signal(signal.SIGINT, runner.signal)
    try:
        return runner.run(sys.argv[1:])
    except ConfigError:
        return runner.finish('invalid_enabled_config', 3)
    except Exception:
        # If persistent volume is unavailable, emit only fixed public text.
        print('rawbbit-dbt: runtime unavailable; operator review required', file=sys.stderr)
        return 4


if __name__ == '__main__':
    raise SystemExit(main())
