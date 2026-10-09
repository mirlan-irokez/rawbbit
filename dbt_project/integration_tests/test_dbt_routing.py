"""Real adapter, Parquet, and runner integration; runs only when invoked explicitly."""

import json
import os
from collections import Counter
from contextlib import contextmanager
import re
import selectors
import subprocess
import time
import uuid

import pytest


pytestmark = pytest.mark.integration


LABELS = {'model': ('rawbbit_events_load',), 'test': (
    'not_null_event_id', 'not_null_app_id', 'not_null_event_time')}
POISON = 'PRIVATE_DBT_PROGRESS_SENTINEL_74291'
PROGRESS = re.compile(
    r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z '
    r'\[dbt-progress job=([0-9a-f-]{36}) attempt=([0-9a-f-]{36}) '
    r'kind=(default|primary|fallback) db=([a-zA-Z_][a-zA-Z0-9_]*)\] (.+)$')
STEP = re.compile(r'^(START|OK|PASS|FAIL|ERROR|WARN|SKIP|PARTIAL) (model|test) (\w+)(?: |$)')
SAFE_BODY = re.compile(
    r'(?:START attempt|VERIFY attempt phase=server_settlement|'
    r'END attempt status=(?:completed|failed|unknown) outcome=(?:success|dbt_failed|'
    r'readiness_failed|process_error|timeout|interrupted|signal_exit|unknown)|'
    r'FAIL dbt category=execution_failed last_phase=(?:before_first_node|model|test)'
    r'(?: last_node=(?:rawbbit_events_load|not_null_event_id|not_null_app_id|not_null_event_time))?|'
    r'(?:START|OK|PASS|FAIL|ERROR|WARN|SKIP|PARTIAL) (?:model|test) '
    r'(?:rawbbit_events_load|not_null_event_id|not_null_app_id|not_null_event_time)'
    r'(?: elapsed_seconds=\d+\.\d{3})?(?: category=(?:test_failed|node_error))?)')
WINDOW = ['backfill', '2026-07-01T00:00:00Z', '2026-07-01T01:00:00Z']
COMPLETE_LINES = [f'] {status} {resource} {label}' for resource, labels in LABELS.items()
                  for label in labels for status in ('START', 'OK' if resource == 'model' else 'PASS')]


def _safe_output(result):
    # Do not include raw captured logs in assertion failures (even on leakage).
    leaked = any(value in result.stdout + result.stderr for value in (
        POISON, 'disposable_only', 'https://private.invalid', 'SELECT secret_value',
        'RAWBBIT_DBT_PROGRESS_FD', 'Traceback (most recent call last)'))
    assert not leaked, 'unsanitized dbt output reached a public stream'


def _docker(*args, timeout=60):
    result = subprocess.run(['docker', *map(str, args)], capture_output=True,
                            text=True, timeout=timeout)
    assert result.returncode == 0, 'disposable Docker operation failed; raw output withheld'
    return result


def _container_args(lab, *, name=None, extra_env=None):
    # Same production image/project bind mount as Lab.job(), but reusable for
    # Popen and a persistent PID 1. Never use a shared candidate image tag.
    args = ['--network', lab.network, '--mount', f'type=bind,src={lab.work},dst=/app']
    if name:
        args += ['--name', name]
    env = {'CLICKHOUSE_HOST': lab.ch, 'CLICKHOUSE_PORT': '8123',
           'CLICKHOUSE_DATABASE': 'default_custom', 'CLICKHOUSE_DBT_USER': 'dbt_integration',
           'CLICKHOUSE_DBT_PASSWORD': 'disposable_only', 'RAWBBIT_RAW_LOAD_MODE': 'dbt',
           'RAWBBIT_DBT_ROUTING_ENABLED': '0', 'RAWBBIT_DBT_MIRROR_PID1': '0',
           'RAWBBIT_DBT_ROUTES_FILE': '/app/runtime/dbt-routes.json',
           'RAWBBIT_DBT_LOCK_FILE': '/app/runtime/pipeline.lock', 'DBT_THREADS': '1'}
    env.update(extra_env or {})
    for key, value in env.items():
        args += ['-e', f'{key}={value}']
    return args


def _progress(result, lab, *, complete=False, scheduler=False):
    _safe_output(result)
    stdout = result.stdout.strip().splitlines()
    if scheduler:
        stdout = [line for line in stdout if line.startswith('{')]
    assert len(stdout) == 1, 'stdout must contain only the existing final JSON summary'
    summary = json.loads(stdout[0])
    assert set(summary) == {'job_id', 'status', 'exit_code', 'revision', 'outcomes'}
    assert str(uuid.UUID(summary['job_id'])) == summary['job_id']
    outcomes = {row['attempt_id']: row for row in summary['outcomes']}
    events = []
    seen_attempts = set()
    for line in result.stderr.splitlines():
        if scheduler and '[dbt-progress ' not in line:
            continue  # Supercronic's own fixed scheduler logs are not dbt frames.
        match = PROGRESS.fullmatch(line)
        assert match is not None, 'stderr contains a non-contract dbt progress line'
        job, attempt, kind, database, body = match.groups()
        assert job == summary['job_id']
        assert attempt in outcomes
        row = outcomes[attempt]
        assert (kind, database) == (row['kind'], row['database'])
        seen_attempts.add(attempt)
        approved_body = SAFE_BODY.fullmatch(body) is not None
        assert approved_body, 'progress body contains an unapproved field or free-form text'
        assert 'request_id=' not in body, 'node/request association must not be invented'
        step = STEP.match(body)
        if step:
            status, resource, label = step.groups()
            approved = label in LABELS[resource]
            assert approved, 'progress exposed an unapproved node label'
            events.append((attempt, status, resource, label))
    assert seen_attempts == set(outcomes), 'all attempts need trusted log correlation'
    for attempt, row in outcomes.items():
        assert set(row) == {'attempt_id', 'kind', 'app_id', 'database',
                            'window_start', 'window_end', 'status'}
        record = json.loads((lab.runtime / 'attempts' / (attempt + '.json')).read_text())
        assert set(record) == {'version', 'job_id', 'attempt_id', 'tag', 'revision', 'kind',
                               'app_id', 'intended_database', 'actual_database', 'table',
                               'window_start', 'window_end', 'status', 'partial_write_possible',
                               'error_class', 'finished_at'}
        assert (record['job_id'], record['status'], record['actual_database']) == (
            summary['job_id'], row['status'], row['database'])
        if complete and row['status'] == 'completed':
            observed = Counter((status, resource, label) for a, status, resource, label in events
                               if a == attempt)
            expected = Counter((status, resource, label) for resource, labels in LABELS.items()
                               for label in labels for status in (
                                   'START', 'OK' if resource == 'model' else 'PASS'))
            assert observed == expected, 'real selected nodes must start/finish exactly once'
    ledger = json.loads((lab.runtime / 'jobs' / (summary['job_id'] + '.json')).read_text())
    assert set(ledger) == {'version', 'job_id', 'status', 'created_at', 'outcomes', 'revision',
                           'snapshot', 'command', 'windows', 'exit_code', 'finished_at'}
    assert ledger['outcomes'] == summary['outcomes']
    for attempt in outcomes:
        for path in (lab.runtime / 'requests' / attempt).glob('*.json'):
            request = json.loads(path.read_text())
            assert set(request) <= {'query_id', 'status', 'transport_settings'}
    return summary, events


# Imported only by the real dbt child via a disposable PYTHONPATH. The shim
# forwards the candidate's callbacks, poisons fields never approved for output,
# and records ONLY approved event evidence. It never replaces dbt invocation.
CALLBACK_FIXTURE = '''
import json, os, sys, threading, time
from pathlib import Path
if Path(sys.argv[0]).name == 'dbt_child.py':
    from dbt.cli.main import dbtRunner
    original = dbtRunner.__init__
    root = Path('/app/progress_fixture')
    lock = threading.Lock()
    def initialize(self, manifest=None, callbacks=None):
        assert callbacks, 'candidate must install a real progress callback'
        def observe(event):
            name = event.info.name
            if name in ('NodeStart', 'NodeFinished'):
                node = event.data.node_info
                label = {'rawbbit_events_load': 'rawbbit_events_load',
                    'not_null_rawbbit_events_load_event_id': 'not_null_event_id',
                    'not_null_rawbbit_events_load_app_id': 'not_null_app_id',
                    'not_null_rawbbit_events_load_event_time': 'not_null_event_time'}.get(node.node_name)
                if label:
                    status = event.data.run_result.status if name == 'NodeFinished' else ''
                    with lock:
                        with (root / 'observed.jsonl').open('a') as out:
                            out.write(json.dumps({'event': name, 'resource': node.resource_type,
                                'label': label, 'status': status}) + '\\n')
                    event.info.msg = 'PRIVATE_DBT_PROGRESS_SENTINEL_74291 https://private.invalid SELECT secret_value'
                    node.node_path = '/private/PRIVATE_DBT_PROGRESS_SENTINEL_74291.sql'
                    node.meta.update({'private': 'PRIVATE_DBT_PROGRESS_SENTINEL_74291'})
                    if name == 'NodeFinished':
                        event.data.run_result.message = 'PRIVATE_DBT_PROGRESS_SENTINEL_74291'
                        event.data.run_result.adapter_response.update({
                            'private': 'PRIVATE_DBT_PROGRESS_SENTINEL_74291', 'code': 999999,
                            'url': 'https://private.invalid'})
            for callback in callbacks:
                callback(event)
            if (name == 'NodeStart' and event.data.node_info.node_name == 'rawbbit_events_load'
                    and os.environ.get('PROGRESS_TEST_PAUSE') == '1'):
                (root / 'paused').write_text(str(os.getpid()))
                deadline = time.monotonic() + 60
                while not (root / 'release').exists():
                    if time.monotonic() >= deadline:
                        raise RuntimeError('bounded test callback pause expired')
                    time.sleep(0.05)
        original(self, manifest=manifest, callbacks=[observe])
    dbtRunner.__init__ = initialize
'''


@contextmanager
def _callback_fixture(lab):
    root = lab.work / 'progress_fixture'
    root.mkdir(mode=0o777)
    root.chmod(0o777)
    hook = root / 'sitecustomize.py'
    hook.write_text(CALLBACK_FIXTURE)
    try:
        yield root, {'PYTHONPATH': '/app/progress_fixture', 'PYTHONDONTWRITEBYTECODE': '1'}
    finally:
        # Only known files created by this test inside the disposable Lab.
        for path in root.iterdir():
            if path.is_file():
                path.unlink()
        root.rmdir()


def _observed(root):
    rows = [json.loads(line) for line in (root / 'observed.jsonl').read_text().splitlines()]
    assert rows, 'no real pinned-package NodeStart/NodeFinished evidence captured'
    return rows


def _observed_lifecycle_match(events, rows):
    statuses = {'success': 'OK', 'pass': 'PASS', 'fail': 'FAIL', 'error': 'ERROR',
                'warn': 'WARN', 'skipped': 'SKIP', 'partial success': 'PARTIAL'}
    expected = Counter(('START' if row['event'] == 'NodeStart' else statuses[row['status']],
                        row['resource'], row['label']) for row in rows)
    actual = Counter((status, resource, label) for _, status, resource, label in events)
    assert actual == expected, 'every start/finish/skip must have actual callback evidence'


def success(result):
    assert result.returncode == 0, (result.stdout + "\n" + result.stderr)[-4500:]


def test_adapter_replay_and_grant_boundary(lab):
    assert "INSERT" not in lab.sql("SHOW GRANTS FOR scoped_reader")
    denied = None
    try:
        denied = lab.sql("INSERT INTO team_a.events (event_id, app_id, environment, event_name, event_time, event_date, user_pseudo_id) VALUES ('x', 'route_a', 'prod', 'view', now(), today(), 'x')", user="scoped_reader", password="disposable_only")
    except AssertionError:
        pass
    else:
        pytest.fail(f"scoped reader unexpectedly wrote to team_a: {denied}")
    success(lab.dbt("parse"))
    success(lab.dbt("build"))
    expected = [("route_a", "a1", "view"), ("route_b", "b1", "view"), ("unrouted", "u1", "view")]
    assert lab.rows("default_custom") == expected
    success(lab.dbt("build"))
    assert lab.rows("default_custom") == expected  # per-table key, not MergeTree magic
    # Empty hour has zero matching Parquet files; not a fatal ClickHouse S3 error.
    previous_end = lab.work / "runtime" / "not-used"
    assert not previous_end.exists()


def test_routing_placement_and_fallback(lab):
    lab.route(lab.snapshot())
    result = lab.job()
    _safe_output(result)
    success(result)
    summary, _ = _progress(result, lab, complete=True)
    assert {row['kind'] for row in summary['outcomes']} == {'default', 'primary'}
    assert lab.rows("team_a") == [("route_a", "a1", "view")]
    assert lab.rows("team_b") == [("route_b", "b1", "view")]
    # Prior default copies from baseline remain! Verify current run's exact
    # exclusion with a new event instead of asserting old rows disappear.
    lab.put("route_a", "a2")
    lab.put("unrouted", "u2")
    success(lab.job())
    assert ("route_a", "a2", "view") not in lab.rows("default_custom")
    assert ("unrouted", "u2", "view") in lab.rows("default_custom")
    assert ("route_a", "a2", "view") in lab.rows("team_a")
    # Missing physical destination is not a global config error: only that
    # app falls back, and the other app continues at its own destination.
    broken = lab.snapshot()
    broken["routes"][0]["database"] = "missing_target"
    lab.route(broken)
    lab.put("route_a", "a3")
    lab.put("route_b", "b3")
    result = lab.job()
    _safe_output(result)
    assert result.returncode == 2, (result.stdout + result.stderr)[-4500:]
    summary, events = _progress(result, lab, complete=True)
    assert {row['kind'] for row in summary['outcomes']} == {'default', 'primary', 'fallback'}
    unavailable = next(row['attempt_id'] for row in summary['outcomes']
                       if row['database'] == 'missing_target')
    assert not any(event[0] == unavailable for event in events), 'readiness failure never launched dbt'
    assert ("route_a", "a3", "view") in lab.rows("default_custom")
    assert ("route_a", "a3", "view") not in lab.rows("team_a")
    assert ("route_b", "b3", "view") in lab.rows("team_b")
    assert ("route_b", "b3", "view") not in lab.rows("default_custom")


def test_invalid_config_fails_closed(lab):
    before = [lab.rows(db) for db in ("default_custom", "team_a", "team_b")]
    lab.routes_file.unlink(missing_ok=True)
    result = lab.job()
    assert result.returncode == 3, (result.stdout + result.stderr)[-2500:]
    assert before == [lab.rows(db) for db in ("default_custom", "team_a", "team_b")]
    bad = lab.snapshot()
    bad["routes"][0]["database"] = "default_custom"
    lab.route(bad)
    result = lab.job()
    assert result.returncode == 3, (result.stdout + result.stderr)[-2500:]
    assert before == [lab.rows(db) for db in ("default_custom", "team_a", "team_b")]


def test_empty_disabled_and_last_route_removed(lab):
    lab.route({"version": 1, "routes": []})
    lab.put("route_a", "a4")
    success(lab.job())
    assert ("route_a", "a4", "view") in lab.rows("default_custom")
    lab.routes_file.unlink()
    lab.put("route_b", "b4")
    success(lab.job(enabled="0"))
    assert ("route_b", "b4", "view") in lab.rows("default_custom")


def test_pinned_callbacks_stream_before_real_child_exit(lab, record_property):
    identity = _docker('image', 'inspect', '--format', '{{.Id}}', lab.dbt_image).stdout.strip()
    versions = json.loads(_docker('run', '--rm', '--network', 'none', '--entrypoint', 'python',
        lab.dbt_image, '-c', "import json; from importlib.metadata import version; "
        "print(json.dumps({p:version(p) for p in "
        "('dbt-core','dbt-clickhouse','clickhouse-connect','dbt-common')}))").stdout)
    assert versions['dbt-core'] == '1.9.10'
    assert versions['dbt-clickhouse'] == '1.9.8'
    assert versions['clickhouse-connect'] == '1.6.0'
    record_property('candidate_image_id', identity)
    record_property('candidate_versions', json.dumps(versions, sort_keys=True))
    with _callback_fixture(lab) as (root, env):
        name = lab.token + '-stream-progress'
        args = ['docker', 'run', '--rm', *_container_args(lab, name=name,
                extra_env=dict(env, PROGRESS_TEST_PAUSE='1')), '--entrypoint',
                '/app/bin/dbt-job', lab.dbt_image, *WINDOW]
        child = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        captured = bytearray()
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(child.stderr, selectors.EVENT_READ)
                deadline = time.monotonic() + 45
                while time.monotonic() < deadline:
                    for key, _ in selector.select(timeout=0.1):
                        chunk = os.read(key.fileobj.fileno(), 8192)
                        if chunk:
                            captured.extend(chunk)
                        else:
                            selector.unregister(key.fileobj)
                    if (root / 'paused').exists() and b'] START model rawbbit_events_load\n' in captured:
                        break
                    assert child.poll() is None, 'runner exited before live model progress was observed'
                else:
                    pytest.fail('real child did not emit model progress while held at callback barrier')
            assert child.poll() is None
            # The barrier is inside NodeStart, AFTER invoking the production
            # callback. Verify that exact dbt PID is alive, not just docker/parent.
            pid = int((root / 'paused').read_text())
            _docker('exec', name, 'python', '-c', f'import os; os.kill({pid}, 0)')
            result = subprocess.CompletedProcess(args, None, '', captured.decode())
            _safe_output(result)
            assert b'] OK model rawbbit_events_load' not in captured
            (root / 'release').touch()
            stdout, stderr = child.communicate(timeout=180)
            result = subprocess.CompletedProcess(args, child.returncode, stdout.decode(),
                                                  captured.decode() + stderr.decode())
            _safe_output(result)
            assert result.returncode == 0, 'real paused dbt build did not retain success outcome'
            summary, events = _progress(result, lab, complete=True)
            rows = _observed(root)
            _observed_lifecycle_match(events, rows)
            assert Counter((row['resource'], row['label'], row['status']) for row in rows
                           if row['event'] == 'NodeFinished') == Counter([
                               ('model', 'rawbbit_events_load', 'success'),
                               *[('test', label, 'pass') for label in LABELS['test']]])
            record_property('safe_real_callback_fixture', json.dumps(rows, sort_keys=True))
            print('PINNED_DBT_PROGRESS_EVIDENCE ' + json.dumps({
                'candidate_image_id': identity, 'versions': versions,
                'approved_callbacks': rows}, sort_keys=True))
            # Normal file logging and artifact JSON remain disabled on runner jobs.
            for outcome in summary['outcomes']:
                for path in (lab.runtime / 'logs' / outcome['attempt_id']).rglob('*'):
                    assert not path.is_file(), 'runner restored raw dbt file logging'
        finally:
            (root / 'release').touch()
            if child.poll() is None:
                subprocess.run(['docker', 'rm', '-f', name], capture_output=True, timeout=20)
                try:
                    child.communicate(timeout=20)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.communicate(timeout=10)


@pytest.mark.parametrize('failure', ['model', 'test', 'compilation', 'startup', 'connection'])
def test_real_dbt_failures_are_safe_and_do_not_invent_lifecycle(lab, failure):
    model = lab.work / 'models/ingestion/rawbbit_events_load.sql'
    profile = lab.work / 'profiles.yml'
    macro = lab.work / 'macros/progress_test_not_null.sql'
    original_model, original_profile = model.read_text(), profile.read_text()
    try:
        if failure == 'model':
            # Preserve production alias/materialization configuration. Unknown
            # identifier is only in this disposable project, not production SQL.
            model.write_text(original_model.split('{% set hour_paths', 1)[0] +
                             f'\nselect {POISON} from {{{{ this }}}}\n')
        elif failure == 'test':
            # Exercise the actual selected built-in test identity and pinned
            # fail-fast engine with failing results, not synthetic dbt events.
            macro.write_text("{% test not_null(model, column_name) %}\n"
                'select {{ column_name }} from {{ model }}\n'
                "{% if column_name == 'event_id' %}\n"
                f"where '{POISON}' != ''\n"
                '{% else %} where {{ column_name }} is null {% endif %}\n'
                '{% endtest %}\n')
        elif failure == 'compilation':
            model.write_text(f"{{{{ exceptions.raise_compiler_error('{POISON}') }}}}\n" + original_model)
        elif failure == 'startup':
            profile.write_text(f'rawbbit: [\n{POISON}\n')
        else:
            # Refuse only the child's profile connection; parent control-plane
            # still connects to disposable ClickHouse and confirms quiescence.
            profile.write_text(original_profile.replace(
                'port: "{{ env_var(\'CLICKHOUSE_PORT\', \'8123\') | int }}"', 'port: 1')
                .replace('connect_timeout: 10', 'connect_timeout: 1'))
            assert 'port: 1' in profile.read_text()
        with _callback_fixture(lab) as (root, env):
            result = lab.job(enabled='0', extra_env=dict(env, DBT_THREADS='1'))
            _safe_output(result)
            assert result.returncode == 1, 'dbt failure changed the established job exit contract'
            summary, events = _progress(result, lab)
            assert summary['status'] == 'failed'
            assert len(summary['outcomes']) == 1
            attempt = summary['outcomes'][0]['attempt_id']
            record = json.loads((lab.runtime / 'attempts' / (attempt + '.json')).read_text())
            assert record['error_class'] == 'dbt_failed'
            diagnostics = [PROGRESS.fullmatch(line).group(5) for line in result.stderr.splitlines()
                           if 'category=execution_failed' in line]
            assert len(diagnostics) == 1, 'failure needs one honest fixed terminal diagnosis'
            assert 'last_phase=' in diagnostics[0]
            assert 'failing_rows=' not in result.stderr
            assert 'failure_count=' not in result.stderr, 'count semantics are not approved for this candidate'
            assert 'code=' not in result.stderr, 'structured backend codes are not approved'
            assert not (lab.runtime / 'write-fence.json').exists()
            if failure in ('model', 'test'):
                rows = _observed(root)
                _observed_lifecycle_match(events, rows)
                finished = [row for row in rows if row['event'] == 'NodeFinished']
                if failure == 'model':
                    assert any(row['resource'] == 'model' and row['status'] == 'error' for row in finished)
                    assert any(status == 'ERROR' and resource == 'model' for _, status, resource, _ in events)
                    assert not any(row['resource'] == 'test' and row['event'] == 'NodeStart' for row in rows)
                    assert not any(resource == 'test' and status == 'START' for _, status, resource, _ in events)
                else:
                    assert any(row['label'] == 'not_null_event_id' and row['status'] == 'fail'
                               for row in finished)
                    assert any(status == 'FAIL' and label == 'not_null_event_id'
                               for _, status, _, label in events)
                    assert any(status == 'OK' and resource == 'model' for _, status, resource, _ in events)
            else:
                assert not events, 'pre-node failures cannot manufacture observed node phases'
                assert 'last_phase=before_first_node' in diagnostics[0]
                assert not (root / 'observed.jsonl').exists()
    finally:
        model.write_text(original_model)
        profile.write_text(original_profile)
        macro.unlink(missing_ok=True)


def test_persistent_manual_mirror_and_supercronic_forward_safe_progress(lab):
    crontab = (lab.work / 'crontab').read_bytes()
    with _callback_fixture(lab) as (root, env):
        name = lab.token + '-manual-progress'
        try:
            _docker('run', '-d', '--log-driver', 'json-file',
                    *_container_args(lab, name=name, extra_env=env), '--entrypoint', 'python',
                    lab.dbt_image, '-c', 'import signal; signal.pause()')
            plain = _docker('exec', name, '/app/bin/dbt-job', *WINDOW, timeout=180)
            plain_summary, _ = _progress(plain, lab, complete=True)
            logs = _docker('logs', name)
            assert plain_summary['job_id'] not in logs.stdout + logs.stderr, (
                'docker exec must not implicitly mirror manual output')
            mirrored = _docker('exec', '-e', 'RAWBBIT_DBT_MIRROR_PID1=1', name,
                               '/app/bin/dbt-job', *WINDOW, timeout=180)
            summary, _ = _progress(mirrored, lab, complete=True)
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                logs = _docker('logs', name)
                _safe_output(logs)
                if summary['job_id'] in logs.stdout and all(line in logs.stderr for line in COMPLETE_LINES):
                    break
                time.sleep(0.1)
            else:
                pytest.fail('existing manual mirror flag did not forward safe progress to Docker logs')
            mirrored_summary, _ = _progress(logs, lab, complete=True)
            assert mirrored_summary['job_id'] == summary['job_id']
        finally:
            subprocess.run(['docker', 'rm', '-f', name], capture_output=True, timeout=20)

        # Test-only six-field schedule runs once. Use the image's real
        # Supercronic and production -passthrough-logs flag, not a mocked pipe;
        # never mount over or change the production crontab.
        schedule = root / 'test.crontab'
        schedule.write_text('* * * * * * if [ ! -e /app/progress_fixture/scheduled.once ]; then '
            'touch /app/progress_fixture/scheduled.once; /app/bin/dbt-job ' + ' '.join(WINDOW) +
            '; touch /app/progress_fixture/scheduled.done; fi\n')
        name = lab.token + '-scheduled-progress'
        try:
            _docker('run', '-d', '--log-driver', 'json-file',
                    *_container_args(lab, name=name, extra_env=env), '--entrypoint',
                    '/usr/local/bin/supercronic', lab.dbt_image, '-passthrough-logs',
                    '/app/progress_fixture/test.crontab')
            deadline = time.monotonic() + 180
            while time.monotonic() < deadline:
                logs = _docker('logs', name)
                _safe_output(logs)
                if (root / 'scheduled.done').exists() and any(
                        line.startswith('{') for line in logs.stdout.splitlines()) and all(
                        line in logs.stderr for line in COMPLETE_LINES):
                    break
                running = _docker('inspect', '--format', '{{.State.Running}}', name).stdout.strip()
                assert running == 'true', 'disposable Supercronic exited before forwarding job output'
                time.sleep(0.1)
            else:
                pytest.fail('Supercronic did not forward the scheduled job within its bounded test window')
            summary, _ = _progress(logs, lab, complete=True, scheduler=True)
            assert summary['exit_code'] == 0
            assert (lab.work / 'crontab').read_bytes() == crontab
        finally:
            subprocess.run(['docker', 'rm', '-f', name], capture_output=True, timeout=20)


def test_real_adapter_query_ownership_and_unknown_writer_fence(lab):
    # Administrative privileges exist only in this disposable lab, to create
    # randomly named databases/users used by the safety contract tests. The
    # normal routed loader above is tested with its separate narrow grants.
    lab.sql("CREATE USER contract_test IDENTIFIED WITH sha256_password BY 'disposable_only'")
    lab.sql("GRANT SELECT, INSERT, CREATE DATABASE, CREATE TABLE, DROP DATABASE, DROP TABLE, "
            "ALTER DELETE, ALTER UPDATE, SYSTEM FLUSH LOGS, SYSTEM MERGES, CREATE USER, DROP USER, KILL QUERY "
            "ON *.* TO contract_test WITH GRANT OPTION")
    try:
        result = subprocess.run([
            "docker", "run", "--rm", "--network", lab.network,
            "--mount", f"type=bind,src={lab.work},dst=/app",
            "-e", f"CLICKHOUSE_HOST={lab.ch}", "-e", "CLICKHOUSE_PORT=8123",
            "-e", "CLICKHOUSE_DBT_USER=contract_test",
            "-e", "CLICKHOUSE_DBT_PASSWORD=disposable_only",
            "-e", "RAWBBIT_DBT_TEST_CLICKHOUSE=1",
            "--entrypoint", "python", lab.dbt_image,
            "-m", "unittest", "discover", "-s", "/app/tests",
            "-p", "test_adapter_integration.py", "-v",
        ], capture_output=True, text=True, timeout=180)
        success(result)
    finally:
        lab.sql("DROP USER contract_test")


@pytest.mark.parametrize("partial_insert", [False, True])
def test_delete_before_insert_failure_falls_back_and_reconciles_per_table(lab, partial_insert):
    lab.route(lab.snapshot())
    success(lab.job())
    script = lab.work / 'fault_injection.py'
    # Inject a failure at the adapter's delete/insert boundary, not in production
    # code. Actual server DELETE/INSERT requests still pass through the exact
    # ownership, acknowledgement, and mutation-completion checks used by dbt.
    script.write_text('''
import os, sys
sys.path.insert(0, '/app/runner')
from runner import Runner
from dbt_child import install_tracking
from dbt.adapters.clickhouse.credentials import ClickHouseCredentials
from dbt.adapters.clickhouse.httpclient import ChHttpClient
install_tracking()
class Fault(Runner):
    def build(self, record, variables, request_dir):
        if record['kind'] != 'primary' or record['app_id'] != 'route_a':
            return super().build(record, variables, request_dir)
        os.environ['RAWBBIT_DBT_REQUEST_DIR'] = str(request_dir)
        os.environ['RAWBBIT_DBT_ATTEMPT_TAG'] = record['tag']
        c = ChHttpClient(ClickHouseCredentials(driver='http',
            host=os.environ['CLICKHOUSE_HOST'], port=8123,
            user=os.environ['CLICKHOUSE_DBT_USER'], password=os.environ['CLICKHOUSE_DBT_PASSWORD'],
            schema=record['actual_database'], check_exchange=False, use_lw_deletes=True,
            custom_settings={'log_comment':record['tag'], 'log_queries':1, 'log_query_settings':1,
                             'mutations_sync':2, 'lightweight_deletes_sync':2}))
        try:
            c.command("DELETE FROM events WHERE app_id = 'route_a'")
            if os.environ['PARTIAL_INSERT'] == '1':
                c.command("INSERT INTO events (event_id,app_id,event_name,event_time,event_date) "
                          "VALUES ('a1','route_a','partial','2026-07-01 00:15:00','2026-07-01')")
            return 'dbt_failed'
        finally:
            c.close()
raise SystemExit(Fault(os.environ).run(['backfill','2026-07-01T00:00:00Z','2026-07-01T01:00:00Z']))
''')
    result = subprocess.run([
        'docker', 'run', '--rm', '--network', lab.network,
        '--mount', f'type=bind,src={lab.work},dst=/app',
        '-e', f'CLICKHOUSE_HOST={lab.ch}', '-e', 'CLICKHOUSE_PORT=8123',
        '-e', 'CLICKHOUSE_DATABASE=default_custom', '-e', 'CLICKHOUSE_DBT_USER=dbt_integration',
        '-e', 'CLICKHOUSE_DBT_PASSWORD=disposable_only', '-e', 'RAWBBIT_RAW_LOAD_MODE=dbt',
        '-e', 'RAWBBIT_DBT_ROUTING_ENABLED=1', '-e', f'PARTIAL_INSERT={int(partial_insert)}',
        '--entrypoint', 'python', lab.dbt_image, '/app/fault_injection.py',
    ], capture_output=True, text=True, timeout=180)
    assert result.returncode == 2, (result.stdout + result.stderr)[-4500:]
    assert ('route_a', 'a1', 'view') in lab.rows('default_custom')
    if partial_insert:
        assert ('route_a', 'a1', 'partial') in lab.rows('team_a')
    else:
        assert not any(row[0] == 'route_a' for row in lab.rows('team_a'))
    assert any(row[0] == 'route_b' for row in lab.rows('team_b'))
    # A bounded successful replay repairs the intended table; copies in default
    # remain, requiring an explicit reviewed cross-database reconciliation.
    success(lab.job())
    success(lab.job())
    assert ('route_a', 'a1', 'view') in lab.rows('team_a')
    assert ('route_a', 'a1', 'view') in lab.rows('default_custom')
    for database in ('default_custom', 'team_a', 'team_b'):
        rows = lab.rows(database)
        keys = [row[:2] for row in rows]
        assert len(keys) == len(set(keys))


def test_client_timeout_requires_server_completion_before_fallback(lab):
    lab.route(lab.snapshot())
    script = lab.work / 'timeout_fault_injection.py'
    script.write_text('''
import os, subprocess, sys, time
sys.path.insert(0, '/app/runner')
from runner import Runner
from clickhouse import literal
class Timeout(Runner):
    def build(self, record, variables, request_dir):
        if record['kind'] != 'primary' or record['app_id'] != 'route_a':
            return super().build(record, variables, request_dir)
        env = dict(self.env, CLICKHOUSE_DATABASE=record['actual_database'],
                   RAWBBIT_DBT_ATTEMPT_TAG=record['tag'], RAWBBIT_DBT_REQUEST_DIR=str(request_dir))
        command = """
import os, sys
sys.path.insert(0, '/app/runner')
from dbt_child import install_tracking
from dbt.adapters.clickhouse.credentials import ClickHouseCredentials
from dbt.adapters.clickhouse.httpclient import ChHttpClient
install_tracking()
c = ChHttpClient(ClickHouseCredentials(driver='http',host=os.environ['CLICKHOUSE_HOST'],port=8123,
    user=os.environ['CLICKHOUSE_DBT_USER'],password=os.environ['CLICKHOUSE_DBT_PASSWORD'],
    schema=os.environ['CLICKHOUSE_DATABASE'],check_exchange=False,
    custom_settings={'log_comment':os.environ['RAWBBIT_DBT_ATTEMPT_TAG'],'log_queries':1,'log_query_settings':1,
                     'mutations_sync':2,'lightweight_deletes_sync':2}))
c.command("INSERT INTO events (event_id,app_id,event_time,event_date) SELECT toString(number), 'route_a', "
          "toDateTime64('2026-07-01 00:00:00',3,'UTC'), toDate('2026-07-01') "
          "FROM numbers(20) WHERE sleepEachRow(0.1)=0")
"""
        self.active = subprocess.Popen([sys.executable, '-c', command], env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        deadline = time.monotonic() + 15
        try:
            while time.monotonic() < deadline:
                rows = self.control.query("SELECT query_id FROM system.processes WHERE Settings['log_comment'] = "
                    + literal(record['tag']) + " AND startsWith(query, 'INSERT')")
                if rows:
                    self.stop_child(self.active)
                    return 'timeout'
                assert self.active.poll() is None, 'writer exited before reaching server'
                time.sleep(0.05)
            raise AssertionError('writer did not reach server')
        finally:
            self.stop_child(self.active)
raise SystemExit(Timeout(os.environ).run(['backfill','2026-07-01T00:00:00Z','2026-07-01T01:00:00Z']))
''')
    result = subprocess.run([
        'docker', 'run', '--rm', '--network', lab.network,
        '--mount', f'type=bind,src={lab.work},dst=/app',
        '-e', f'CLICKHOUSE_HOST={lab.ch}', '-e', 'CLICKHOUSE_PORT=8123',
        '-e', 'CLICKHOUSE_DATABASE=default_custom', '-e', 'CLICKHOUSE_DBT_USER=dbt_integration',
        '-e', 'CLICKHOUSE_DBT_PASSWORD=disposable_only', '-e', 'RAWBBIT_RAW_LOAD_MODE=dbt',
        '-e', 'RAWBBIT_DBT_ROUTING_ENABLED=1',
        '--entrypoint', 'python', lab.dbt_image, '/app/timeout_fault_injection.py',
    ], capture_output=True, text=True, timeout=180)
    assert result.returncode == 2, (result.stdout + result.stderr)[-4500:]
    assert not (lab.runtime / 'write-fence.json').exists()
    summary = json.loads(result.stdout.strip().splitlines()[-1])
    primary = next(row for row in summary['outcomes'] if row['kind'] == 'primary' and row['app_id'] == 'route_a')
    record = json.loads((lab.runtime / 'attempts' / (primary['attempt_id'] + '.json')).read_text())
    assert record['error_class'] == 'timeout'
    pending = [json.loads(path.read_text())['query_id'] for path in
               (lab.runtime / 'requests' / primary['attempt_id']).glob('*.json')
               if json.loads(path.read_text())['status'] == 'dispatched']
    assert pending, 'test must terminate an unacknowledged server request'
    ids = ','.join("'" + value + "'" for value in pending)
    assert lab.sql(f"SELECT count() FROM system.processes WHERE query_id IN ({ids})") == '0'
    lab.sql('SYSTEM FLUSH LOGS')
    terminal = lab.sql(f"SELECT DISTINCT query_id FROM system.query_log WHERE query_id IN ({ids}) "
                       "AND type IN ('QueryFinish','ExceptionBeforeStart','ExceptionWhileProcessing')")
    assert set(pending) <= set(terminal.splitlines())
    assert ('route_a', 'a1', 'view') in lab.rows('default_custom')
    assert any(row[0] == 'route_b' for row in lab.rows('team_b'))
