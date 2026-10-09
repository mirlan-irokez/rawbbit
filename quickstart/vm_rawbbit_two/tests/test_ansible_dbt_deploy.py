"""Public-safe role contracts and optional localhost-only Ansible rendering."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

import yaml

REPO = Path(__file__).resolve().parents[2]
ROLE = REPO / 'ansible/roles/rawbbit_two'


def tasks(path):
    for task in yaml.safe_load(path.read_text()):
        yield task
        for key in ('block', 'rescue', 'always'):
            for child in task.get(key, []):
                yield child


class AnsibleDeploymentContracts(unittest.TestCase):
    def test_local_image_is_always_rebuilt_before_candidate_activation(self):
        main = list(tasks(ROLE / 'tasks/main.yml'))
        build_index = next(i for i, task in enumerate(main)
                           if 'community.docker.docker_image_build' in task)
        copy_index = next(i for i, task in enumerate(main)
                          if task['name'] == 'Copy dbt-runner build context for the local image')
        validate_index = next(i for i, task in enumerate(main)
                              if task['name'].startswith('Validate staged routing'))
        activation_index = next(i for i, task in enumerate(main)
                                if task.get('ansible.builtin.include_tasks') == 'activate-dbt-runner.yml')
        build_task = main[build_index]
        build = build_task['community.docker.docker_image_build']
        self.assertEqual(build['rebuild'], 'always')
        self.assertEqual(build['name'], '{{ rawbbit_two_dbt_runner_image }}')
        self.assertEqual(build['path'], '{{ rawbbit_two_dbt_build_context }}')
        self.assertEqual(build['args'], {
            'DBT_UID': '{{ rawbbit_two_deploy_uid.stdout }}',
            'DBT_GID': '{{ rawbbit_two_deploy_gid.stdout }}',
        })
        self.assertFalse(build_task['become'])
        for task in (main[copy_index], build_task):
            self.assertEqual(task['when'], 'rawbbit_two_dbt_runner_image == "rawbbit-dbt-runner:local"')
        self.assertEqual(main[copy_index]['ansible.builtin.copy']['src'], '{{ playbook_dir }}/../../dbt_project/')
        self.assertEqual(main[copy_index]['ansible.builtin.copy']['dest'], '{{ rawbbit_two_dbt_build_context }}/')
        self.assertFalse(build.get('nocache', False))
        self.assertFalse(build.get('pull', False))
        self.assertFalse(build_task.get('ignore_errors', False))
        self.assertLess(copy_index, build_index)
        self.assertLess(build_index, validate_index)
        self.assertLess(validate_index, activation_index)

    def test_every_stack_action_is_scoped_and_cannot_recreate_runner(self):
        for path in (ROLE / 'tasks').glob('*.yml'):
            for task in tasks(path):
                compose = task.get('community.docker.docker_compose_v2')
                if compose:
                    with self.subTest(path=path, task=task['name']):
                        self.assertTrue(compose.get('services'))
                        self.assertNotIn('dbt-runner', compose['services'])
                        if compose.get('state') == 'present':
                            self.assertFalse(compose['dependencies'])
                            if 'clickhouse' in compose['services']:
                                self.assertEqual(compose['services'], ['clickhouse'])
                                self.assertEqual(compose['recreate'], 'never')

    def test_environment_and_compose_are_staged_until_host_transaction(self):
        main = list(tasks(ROLE / 'tasks/main.yml'))
        env = next(task for task in main if task.get('ansible.builtin.template', {}).get('src') == 'env.j2')
        self.assertIn('rawbbit_two_stage.path', env['ansible.builtin.template']['dest'])
        copy = next(task for task in main if task['name'] == 'Copy required VM-two runtime artifacts')
        self.assertFalse(any(item['path'].startswith('docker-compose') for item in copy['loop']))
        self.assertIn('clickhouse/', copy['ansible.builtin.copy']['force'])
        validate_index = next(i for i, task in enumerate(main) if task['name'].startswith('Validate staged routing'))
        provision_index = next(i for i, task in enumerate(main) if task['name'].startswith('Reconcile managed datasets'))
        runner_index = next(i for i, task in enumerate(main) if task.get('ansible.builtin.include_tasks') == 'activate-dbt-runner.yml')
        activation_index = next(i for i, task in enumerate(main) if task['name'].startswith('Activate and deploy'))
        self.assertLess(validate_index, provision_index)
        self.assertLess(runner_index, activation_index)

    def test_no_route_overlay_or_protected_registry_mounted_into_runner(self):
        compose = yaml.safe_load((REPO / 'vm_rawbbit_two/docker-compose.yml').read_text())
        runner = compose['services']['dbt-runner']
        self.assertEqual(runner['volumes'], ['/srv/rawbbit-two/dbt:/app/runtime'])
        self.assertEqual(runner['environment']['RAWBBIT_DBT_ROUTES_FILE'], '/app/runtime/dbt-routes.json')


@unittest.skipUnless(shutil.which('ansible-playbook'), 'Ansible CLI not installed')
class LocalhostRouteRendering(unittest.TestCase):
    def run_validation(self, routes, datasets, expect_success=True, expected=None, enabled=True):
        defaults = yaml.safe_load((ROLE / 'defaults/main.yml').read_text())
        defaults.update(rawbbit_two_dbt_routing_enabled=enabled, rawbbit_two_dbt_routes=routes,
                        rawbbit_two_datasets=datasets, rawbbit_two_raw_load_mode='dbt',
                        rawbbit_two_clickhouse_database='custom_default')
        play = [{'hosts': 'localhost', 'gather_facts': False, 'vars': defaults,
                 'tasks': [{'ansible.builtin.include_tasks': str(ROLE / 'tasks/validate-dbt-routes.yml')}]}]
        if expected is not None:
            play[0]['tasks'].append({'ansible.builtin.assert': {
                'that': ['rawbbit_two_resolved_dbt_routes == expected_routes']},
                'vars': {'expected_routes': expected}})
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'validate.yml'
            path.write_text(yaml.safe_dump(play))
            result = subprocess.run(['ansible-playbook', '-i', 'localhost,', '-c', 'local', str(path)],
                                    text=True, capture_output=True, timeout=60)
        self.assertEqual(result.returncode == 0, expect_success, result.stdout + result.stderr)

    def test_disabled_mcp_metadata_is_resolved_and_disabled_route_is_omitted(self):
        route = {'app_id': 'runner_rawbbit', 'dataset_id': 'runner_rawbbit'}
        datasets = [{'id': 'runner_rawbbit', 'enabled': False, 'database': 'runner_data', 'table': 'events'}]
        self.run_validation([route, dict(route, app_id='disabled_app', enabled=False)], datasets,
                            expected=[dict(route, database='runner_data', table='events')])

    def test_missing_metadata_duplicates_and_malformed_disabled_routes_fail(self):
        route = {'app_id': 'runner_rawbbit', 'dataset_id': 'runner_rawbbit'}
        datasets = [{'id': 'runner_rawbbit', 'enabled': False, 'database': 'runner_data', 'table': 'events'}]
        for routes, metadata in (([route], []), ([route, route], datasets),
                                 ([dict(route, app_id='../unsafe', enabled=False)], datasets),
                                 ([dict(route, enabled='false')], datasets)):
            with self.subTest(routes=routes):
                self.run_validation(routes, metadata, expect_success=False)

    def test_enabled_empty_route_list_is_valid(self):
        self.run_validation([], [], expected=[])

    def test_disabled_routing_does_not_resolve_retained_obsolete_references(self):
        self.run_validation([{'app_id': 'example_app', 'dataset_id': 'removed_dataset'}], [],
                            expected=[], enabled=False)


if __name__ == '__main__':
    unittest.main()
