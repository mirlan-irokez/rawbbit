"""Host transaction tests; no Docker daemon, network, credentials or databases."""
import argparse
import copy
import fcntl
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

HELPER = Path(__file__).resolve().parents[1] / 'deploy-dbt-runner.py'
spec = importlib.util.spec_from_file_location('deploy_dbt_runner', HELPER)
deploy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deploy)


class HostDeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name).resolve()
        self.install, self.stage, self.runtime = root / 'install', root / 'stage', root / 'runtime'
        for path in (self.install, self.stage, self.runtime):
            path.mkdir()
        self.files = ['docker-compose.yml', 'docker-compose.dozzle.yml', 'docker-compose.datasets.yml']
        for name in self.files:
            (self.stage / name).write_text('candidate ' + name)
            (self.install / name).write_text('active ' + name)
        (self.stage / '.env').write_text('RAWBBIT_RAW_LOAD_MODE=dbt\nCANDIDATE=1\n')
        (self.install / '.env').write_text('RAWBBIT_RAW_LOAD_MODE=dbt\nACTIVE=1\n')
        self.document = {'version': 1, 'routes': [{'app_id': 'runner_rawbbit', 'dataset_id': 'runner_rawbbit',
                                                'database': 'runner_data', 'table': 'events'}]}
        (self.stage / 'dbt-routes.json').write_text(json.dumps(self.document))
        (self.runtime / 'dbt-routes.json').write_text('old snapshot')
        self.args = argparse.Namespace(install_dir=str(self.install), stage_dir=str(self.stage),
                                       runtime_dir=str(self.runtime), compose_file=self.files,
                                       drain_timeout=1, recover_gate=False, bootstrap_stopped=False,
                                       pre_routing_rollback=False, remove_dataset_overlay=False)
        self.env = {'RAWBBIT_DBT_ROUTING_ENABLED': '1', 'RAWBBIT_DBT_ROUTES_FILE': '/app/runtime/dbt-routes.json',
                    'RAWBBIT_RAW_LOAD_MODE': 'dbt', 'CLICKHOUSE_DATABASE': 'custom_default',
                    'CLICKHOUSE_DBT_USER': 'rawbbit_dbt'}
        self.config = {'services': {
            'dbt-runner': {'image': 'test:candidate', 'environment': self.env,
                           'volumes': [{'type': 'bind', 'source': str(self.runtime), 'target': '/app/runtime'}]},
            'clickhouse': {'ports': [{'target': 8123, 'host_ip': '127.0.0.1', 'published': '8123'}],
                           'environment': {'CLICKHOUSE_USER': 'admin', 'CLICKHOUSE_PASSWORD': 'not-a-secret'}}}}
        self.commands = []
        self.containers = []
        self.gated = True
        self.verified_lock = False
        self.patch_run = patch.object(deploy, 'run', side_effect=self.fake_run)
        self.patch_run.start()
        self.addCleanup(self.patch_run.stop)

    def fake_run(self, argv, timeout=120):
        self.commands.append(argv)
        if argv[-3:] == ['config', '--format', 'json']:
            return json.dumps(self.config)
        if argv[1:3] == ['image', 'inspect']:
            return 'sha256:' + 'a' * 64
        if '--capabilities' in argv:
            if not self.gated or ('exec' in argv and 'old-ungated' in argv):
                raise deploy.DeploymentError('ungated')
            return json.dumps(dict(deploy.CONTRACT, commands=['--capabilities', 'validate-config']))
        if 'validate-config' in argv:
            return json.dumps({'status': 'valid', 'revision': 'candidate-revision'})
        if 'verify-control' in argv:
            return json.dumps({'status': 'control_access_verified'})
        if 'up' in argv:
            self.assertTrue((self.runtime / 'deploy.gate').exists())
            with (self.runtime / 'pipeline.lock').open('a') as stream:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.verified_lock = True
            return ''
        if argv[1] == 'inspect':
            files = [str(self.install / name) for name in self.files]
            if self.args.pre_routing_rollback:
                files += [str(self.install / 'docker-compose.dbt-paused.yml')]
            return json.dumps([{'State': {'Running': True}, 'Image': 'sha256:' + 'a' * 64,
                'Mounts': [{'Destination': '/app/runtime', 'Source': str(self.runtime), 'RW': True}], 'Config': {
                'Image': 'test:candidate', 'Env': [k + '=' + v for k, v in self.env.items()],
                'Entrypoint': ['/bin/sleep', 'infinity'] if self.args.pre_routing_rollback else None,
                'Labels': {'com.docker.compose.project.config_files': ','.join(files)}}}])
        if 'ps' in argv and 'dbt-runner' in argv:
            return 'new-runner'
        if argv[-2:] == ['cat', '/app/runtime/dbt-routes.json']:
            return (self.runtime / 'dbt-routes.json').read_text()
        return ''

    def deployment(self):
        result = deploy.Deployment(self.args)
        result.containers = lambda: self.containers
        result.assert_quiescence = lambda: None
        return result

    def old_container(self, container_id, running, oneoff=False):
        return {'Id': container_id, 'State': {'Running': running}, 'Config': {'Labels': {
            'com.docker.compose.project.config_files': ','.join(str(self.install / name) for name in self.files),
            'com.docker.compose.oneoff': str(oneoff)}}}

    def assert_not_published(self):
        self.assertIn('ACTIVE=1', (self.install / '.env').read_text())
        self.assertEqual((self.runtime / 'dbt-routes.json').read_text(), 'old snapshot')
        self.assertTrue((self.runtime / 'deploy.gate').exists())
        self.assertFalse(any('up' in cmd for cmd in self.commands))

    def test_single_lock_transaction_preserves_all_overlays_and_targets_only_runner(self):
        lock = self.runtime / 'pipeline.lock'
        lock.touch()
        inode = lock.stat().st_ino
        self.deployment().execute()
        self.assertEqual(lock.stat().st_ino, inode)
        self.assertTrue(self.verified_lock)
        self.assertFalse((self.runtime / 'deploy.gate').exists())
        for command in self.commands:
            if 'up' in command:
                self.assertEqual(command[-1], 'dbt-runner')
                self.assertIn('--no-deps', command)
                for name in self.files:
                    self.assertIn(str(self.install / name), command)
        fence = json.loads((self.runtime / 'deploy-fence.json').read_text())
        self.assertEqual(fence['route_revision'], 'candidate-revision')
        self.assertEqual(fence['compose_files'], sorted(str(self.install / name) for name in self.files))

    def active_job(self, duration):
        ready = self.runtime / 'job-ready'
        process = subprocess.Popen([sys.executable, '-c',
            'import fcntl,pathlib,sys,time; p=pathlib.Path(sys.argv[1]); '
            'f=p.open("a"); fcntl.flock(f,fcntl.LOCK_EX); '
            'pathlib.Path(sys.argv[2]).touch(); time.sleep(float(sys.argv[3]))',
            str(self.runtime / 'pipeline.lock'), str(ready), str(duration)])
        self.addCleanup(lambda: process.wait(timeout=10))
        deadline = time.monotonic() + 5
        while not ready.exists():
            if time.monotonic() > deadline:
                self.fail('job lock not acquired')
            time.sleep(0.01)
        return process

    def test_timeout_retains_gate_and_old_artifacts_without_killing_active_job(self):
        process = self.active_job(0.6)
        self.args.drain_timeout = 0.1
        with self.assertRaisesRegex(deploy.DeploymentError, 'Drain timed out'):
            self.deployment().execute()
        self.assertIsNone(process.poll())
        self.assert_not_published()

    def test_success_waits_for_active_job_to_complete(self):
        process = self.active_job(0.2)
        self.deployment().execute()
        self.assertEqual(process.wait(timeout=2), 0)
        self.assertTrue(self.verified_lock)

    def test_waiting_backfill_defers_on_gate_before_deployment_lock_is_released(self):
        self.active_job(0.3)
        result = self.runtime / 'queued-result'
        queued = subprocess.Popen([sys.executable, '-c',
            'import pathlib,time,sys; root=pathlib.Path(sys.argv[1]); '
            'deadline=time.monotonic()+5\n'
            'while time.monotonic()<deadline:\n'
            ' if (root/"deploy.gate").exists():\n'
            '  (root/"queued-result").write_text("gated"); sys.exit(0)\n'
            ' time.sleep(.01)\n'
            'sys.exit(1)', str(self.runtime)])
        self.addCleanup(lambda: queued.wait(timeout=10))
        self.deployment().execute()
        self.assertEqual(queued.wait(timeout=2), 0)
        self.assertEqual(result.read_text(), 'gated')

    def test_first_upgrade_refuses_running_ungated_runner_even_with_bootstrap_flag(self):
        self.args.bootstrap_stopped = True
        self.containers = [self.old_container('old-ungated', True)]
        with self.assertRaisesRegex(deploy.DeploymentError, 'Ungated runner is running'):
            self.deployment().execute()
        self.assert_not_published()

    def test_queued_one_off_container_must_defer_or_finish_before_cutover(self):
        self.containers = [self.old_container('one-off', True, oneoff=True)]
        with self.assertRaisesRegex(deploy.DeploymentError, 'one-off runner remains'):
            self.deployment().execute()
        self.assert_not_published()

    def test_gated_existing_runner_can_be_drained(self):
        self.containers = [self.old_container('gate-compatible', True)]
        self.deployment().execute()
        self.assertTrue(self.verified_lock)

    def test_removing_dataset_overlay_preserves_dozzle_and_runner_targeting(self):
        self.containers = [self.old_container('gate-compatible', True)]
        self.files.remove('docker-compose.datasets.yml')
        self.args.remove_dataset_overlay = True
        self.deployment().execute()
        up = next(cmd for cmd in self.commands if 'up' in cmd)
        self.assertIn(str(self.install / 'docker-compose.dozzle.yml'), up)
        self.assertNotIn(str(self.install / 'docker-compose.datasets.yml'), up)
        self.assertEqual(up[-1], 'dbt-runner')

    def test_accidentally_omitting_active_dozzle_overlay_fails_closed(self):
        self.containers = [self.old_container('gate-compatible', True)]
        self.files.remove('docker-compose.dozzle.yml')
        with self.assertRaisesRegex(deploy.DeploymentError, 'Active overlays would be omitted'):
            self.deployment().execute()
        self.assert_not_published()

    def test_dataset_overlay_removal_requires_explicit_metadata_decision(self):
        self.containers = [self.old_container('gate-compatible', True)]
        self.files.remove('docker-compose.datasets.yml')
        with self.assertRaisesRegex(deploy.DeploymentError, 'Active overlays would be omitted'):
            self.deployment().execute()
        self.assert_not_published()

    def test_routing_only_environment_change_does_not_force_mcp_restart(self):
        runner = self.deployment()
        runner.config['services']['mcp-server'] = {'environment': {'CLICKHOUSE_DATABASE': 'custom_default',
                                                                 'MCP_DATASETS_CONFIG_PATH': '/app/datasets.json'}}
        actual_env = ['CLICKHOUSE_DATABASE=custom_default', 'MCP_DATASETS_CONFIG_PATH=/app/datasets.json']
        def inspect(argv, timeout=120):
            if argv[1] == 'ps':
                return 'mcp'
            return json.dumps([{'Config': {'Env': actual_env}}])
        with patch.object(deploy, 'run', side_effect=inspect):
            self.assertFalse(runner.mcp_environment_changed())
            self.env['RAWBBIT_DBT_ROUTING_ENABLED'] = '0'
            self.assertFalse(runner.mcp_environment_changed())
            actual_env[0] = 'CLICKHOUSE_DATABASE=other'
            self.assertTrue(runner.mcp_environment_changed())

    def test_changed_running_warehouse_is_refused_before_publication(self):
        runner = self.deployment()
        runner.config['services']['clickhouse']['image'] = 'clickhouse:24.8'
        def changed(argv, timeout=120):
            if argv[1] == 'ps':
                return 'warehouse'
            return json.dumps([{'Config': {'Image': 'clickhouse:24.8',
                                          'Env': ['CLICKHOUSE_USER=admin', 'CLICKHOUSE_PASSWORD=different']}}])
        with patch.object(deploy, 'run', side_effect=changed):
            with self.assertRaisesRegex(deploy.DeploymentError, 'separately drain warehouse'):
                runner.assert_warehouse_unchanged()
        self.assertIn('ACTIVE=1', (self.install / '.env').read_text())

    def test_read_only_cancellation_fence_refuses_unknown_server_work(self):
        runner = self.deployment()
        # Restore the real fence method; the normal unit fixture avoids network.
        runner.assert_quiescence = deploy.Deployment.assert_quiescence.__get__(runner)
        from unittest.mock import MagicMock
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b'1\n'
        opener = MagicMock()
        opener.open.return_value = response
        with patch.object(deploy.urllib.request, 'build_opener', return_value=opener):
            with self.assertRaisesRegex(deploy.DeploymentError, 'unfinished mutations'):
                runner.assert_quiescence()
        request = opener.open.call_args.args[0]
        self.assertIn(b'system.processes', request.data)
        self.assertIn(b'system.mutations', request.data)
        self.assertNotIn(b'KILL', request.data)

    def test_stopped_old_runner_requires_explicit_bootstrap_and_cancellation_fence(self):
        self.containers = [self.old_container('old-ungated', False)]
        with self.assertRaisesRegex(deploy.DeploymentError, 'bootstrap-stopped'):
            self.deployment().execute()
        self.assert_not_published()
        self.args.bootstrap_stopped = self.args.recover_gate = True
        runner = self.deployment()
        def unfinished():
            raise deploy.DeploymentError('unfinished mutations')
        runner.assert_quiescence = unfinished
        with self.assertRaisesRegex(deploy.DeploymentError, 'unfinished mutations'):
            runner.execute()
        self.assert_not_published()

    def test_existing_gate_needs_explicit_recovery(self):
        (self.runtime / 'deploy.gate').touch()
        with self.assertRaisesRegex(deploy.DeploymentError, 'inspected recovery'):
            self.deployment().execute()
        self.assert_not_published()

    def test_durable_write_fence_is_not_cleared_by_gate_recovery(self):
        (self.runtime / 'write-fence.json').write_text('{}')
        self.args.recover_gate = True
        with self.assertRaisesRegex(deploy.DeploymentError, 'operator reconciliation'):
            self.deployment().execute()
        self.assert_not_published()
        self.assertTrue((self.runtime / 'write-fence.json').exists())

    def test_disabled_routing_needs_no_snapshot_and_last_route_removal_is_default_only(self):
        self.env['RAWBBIT_DBT_ROUTING_ENABLED'] = '0'
        (self.stage / 'dbt-routes.json').unlink()
        (self.runtime / 'dbt-routes.json').unlink()
        self.deployment().execute()
        self.assertFalse((self.runtime / 'dbt-routes.json').exists())
        self.assertFalse(json.loads((self.runtime / 'deploy-fence.json').read_text())['routing_enabled'])

    def test_enabled_zero_routes_still_requires_valid_snapshot(self):
        (self.stage / 'dbt-routes.json').unlink()
        with self.assertRaisesRegex(deploy.DeploymentError, 'snapshot missing/unreadable'):
            self.deployment()
        self.assertFalse((self.runtime / 'deploy.gate').exists())
        (self.stage / 'dbt-routes.json').write_text('{"version":1,"routes":[]}')
        self.deployment().execute()

    def test_invalid_enabled_snapshot_never_installs_gate_or_publishes(self):
        (self.stage / 'dbt-routes.json').write_text('{"version":2,"routes":[]}')
        with self.assertRaises(deploy.DeploymentError):
            self.deployment()
        self.assertFalse((self.runtime / 'deploy.gate').exists())
        self.assertIn('ACTIVE=1', (self.install / '.env').read_text())

    def test_pre_routing_rollback_is_explicit_inert_and_retains_gate(self):
        self.args.pre_routing_rollback = True
        self.env['RAWBBIT_DBT_ROUTING_ENABLED'] = '0'
        self.gated = False
        self.deployment().execute()
        self.assertTrue((self.runtime / 'deploy.gate').exists())
        self.assertIn('/bin/sleep', (self.install / 'docker-compose.dbt-paused.yml').read_text())
        self.assertTrue(json.loads((self.runtime / 'deploy-fence.json').read_text())['ingestion_paused'])

    def test_failure_during_verification_keeps_gate(self):
        runner = self.deployment()
        real_run = self.fake_run
        def failed(argv, timeout=120):
            if 'parse' in argv:
                raise deploy.DeploymentError('parse failed')
            return real_run(argv, timeout)
        with patch.object(deploy, 'run', side_effect=failed):
            with self.assertRaisesRegex(deploy.DeploymentError, 'parse failed'):
                runner.execute()
        self.assertTrue((self.runtime / 'deploy.gate').exists())

    def test_legacy_loader_entrypoint_must_be_disabled_before_cutover(self):
        (self.install / '.env').write_text('RAWBBIT_RAW_LOAD_MODE=legacy\n')
        (self.install / 'clickhouse').mkdir()
        (self.install / 'clickhouse/load_events_hourly.sh').write_text('old launcher')
        with self.assertRaisesRegex(deploy.DeploymentError, 'Disable/rename'):
            self.deployment().execute()
        self.assertFalse(any('up' in cmd for cmd in self.commands))

    def test_unverified_loader_observation_grants_keep_gate_after_recreation(self):
        original = self.fake_run

        def unverified(argv, timeout=120):
            if 'verify-control' in argv:
                return json.dumps({'status': 'control_access_unverified'})
            return original(argv, timeout)

        with patch.object(deploy, 'run', side_effect=unverified):
            with self.assertRaisesRegex(deploy.DeploymentError, 'observation grants'):
                self.deployment().execute()
        self.assertTrue(self.verified_lock)
        self.assertTrue((self.runtime / 'deploy.gate').exists())

    def test_unchanged_legacy_redeploy_keeps_same_shared_lock(self):
        data = 'RAWBBIT_RAW_LOAD_MODE=legacy\nRAWBBIT_DBT_LOCK_FILE=' + str(self.runtime / 'pipeline.lock') + '\n'
        (self.install / '.env').write_text(data)
        (self.stage / '.env').write_text(data)
        self.env.update(RAWBBIT_RAW_LOAD_MODE='legacy', RAWBBIT_DBT_ROUTING_ENABLED='0')
        (self.install / 'clickhouse').mkdir()
        (self.install / 'clickhouse/load_events_hourly.sh').write_text('unchanged loader')
        self.deployment().execute()
        self.assertTrue(self.verified_lock)

    def test_staged_mutation_aborts_before_publication(self):
        runner = self.deployment()
        (self.stage / '.env').write_text('changed staging content')
        with self.assertRaisesRegex(deploy.DeploymentError, 'Staged artifact changed'):
            runner.execute()
        self.assert_not_published()


class RouteValidationTests(unittest.TestCase):
    def setUp(self):
        self.value = {'version': 1, 'routes': [{'app_id': 'runner.rawbbit', 'dataset_id': 'runner_rawbbit',
                                             'database': 'runner_data', 'table': 'events'}]}

    def test_valid_and_empty_routes(self):
        deploy.validate_routes(json.dumps(self.value).encode(), 'custom_default')
        deploy.validate_routes(b'{"version":1,"routes":[]}', 'custom_default')

    def test_duplicate_unsafe_and_custom_default_collisions(self):
        cases = []
        for field, value in [('app_id', '../runner'), ('table', 'other'), ('database', 'custom_default'),
                             ('database', 'bad-name'), ('dataset_id', '../metadata')]:
            bad = copy.deepcopy(self.value)
            bad['routes'][0][field] = value
            cases.append(bad)
        duplicate = copy.deepcopy(self.value)
        duplicate['routes'].append(copy.deepcopy(duplicate['routes'][0]))
        cases.append(duplicate)
        conflicting = copy.deepcopy(duplicate)
        conflicting['routes'][1]['app_id'] = 'other'
        cases.append(conflicting)
        for case in cases:
            with self.subTest(case=case), self.assertRaises(deploy.DeploymentError):
                deploy.validate_routes(json.dumps(case).encode(), 'custom_default')

    def test_duplicate_json_fields_and_boolean_version(self):
        for data in (b'{"version":1,"version":1,"routes":[]}', b'{"version":true,"routes":[]}'):
            with self.assertRaises(deploy.DeploymentError):
                deploy.validate_routes(data, 'analytics')


if __name__ == '__main__':
    unittest.main()
