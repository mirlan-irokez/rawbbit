"""Render the actual routing macros/model without connecting to a warehouse."""
import datetime
from pathlib import Path
import re
import types
import unittest

try:
    from jinja2 import Environment
except ImportError:
    Environment = None

ROOT = Path(__file__).resolve().parents[1]


class Returned(Exception):
    def __init__(self, value):
        self.value = value


def returned(value):
    raise Returned(value)


def compiler_error(message):
    raise ValueError(message)


@unittest.skipUnless(Environment, 'Jinja2 supplied by dbt or repo venv is required')
class ModelRouting(unittest.TestCase):
    def macro(self, name, variables, globals_value=None):
        source = (ROOT / 'macros' / 'rawbbit_window.sql').read_text()
        block = re.search(r'{% macro ' + name + r'\([^%]*%}.*?{% endmacro %}', source, re.S).group()
        environment = Environment(extensions=['jinja2.ext.do'])
        environment.globals.update(var=lambda key, default=None: variables.get(key, default),
                                   modules=types.SimpleNamespace(re=re, datetime=datetime),
                                   exceptions=types.SimpleNamespace(raise_compiler_error=compiler_error),
                                   return_value=returned)
        environment.globals['return'] = returned
        environment.globals.update(globals_value or {})
        macro = getattr(environment.from_string(block).module, name)
        try:
            return macro()
        except Returned as value:
            return value.value

    def selection(self, variables):
        return self.macro('rawbbit_app_selection', variables)

    def paths(self, variables):
        return self.macro('rawbbit_hour_paths', variables,
                          {'rawbbit_app_selection': lambda: self.selection(variables),
                           'rawbbit_window': lambda: {'hour_count': 2,
                                                      'start_dt': datetime.datetime(2026, 1, 1)}})

    def predicate(self, variables):
        return self.macro('rawbbit_app_predicate', variables,
                          {'rawbbit_app_selection': lambda: self.selection(variables)}).strip()

    def test_default_exact_include_fallback_and_exclude_modes(self):
        include = {'rawbbit_app_id': 'runner_rawbbit', 'rawbbit_excluded_app_ids': []}
        self.assertEqual(self.paths(include), ['app_id=runner_rawbbit/event_date=2026-01-01/hour=00/*.parquet',
                                               'app_id=runner_rawbbit/event_date=2026-01-01/hour=01/*.parquet'])
        self.assertEqual(self.predicate(include), "and app_id = 'runner_rawbbit'")
        excluded = {'rawbbit_excluded_app_ids': ['runner_rawbbit', 'other']}
        self.assertEqual(self.paths(excluded)[0], 'app_id=*/event_date=2026-01-01/hour=00/*.parquet')
        self.assertEqual(self.predicate(excluded), "and app_id not in ('runner_rawbbit', 'other')")
        self.assertEqual(self.predicate({}), '')

    def test_reject_unsafe_paths_and_mixed_modes(self):
        for app in ['../x', 'a/b', "a'", 'a*', 'a?', 'a{b}', 'a\n', '', 3, '..']:
            with self.subTest(app=app), self.assertRaises(ValueError):
                self.selection({'rawbbit_app_id': app})
        with self.assertRaises(ValueError):
            self.selection({'rawbbit_app_id': 'a', 'rawbbit_excluded_app_ids': ['b']})
        with self.assertRaises(ValueError):
            self.selection({'rawbbit_excluded_app_ids': 'a'})

    def test_same_model_keeps_incremental_and_replay_key(self):
        source = (ROOT / 'models' / 'ingestion' / 'rawbbit_events_load.sql').read_text()
        for value in ["incremental_strategy='delete_insert'", "unique_key=['app_id', 'event_id']",
                      'full_refresh=false', "'s3_throw_on_zero_files_match': 0",
                      'partition by app_id, event_id', '{{ rawbbit_app_predicate() }}']:
            self.assertIn(value, source)
        self.assertEqual(len(list((ROOT / 'models').rglob('*.sql'))), 1)

    def test_model_render_uses_real_path_and_predicate_modes(self):
        for variables in [{}, {'rawbbit_excluded_app_ids': ['runner_rawbbit']}, {'rawbbit_app_id': 'runner_rawbbit'}]:
            environment = Environment()
            environment.globals.update(config=lambda **kwargs: '',
                                       rawbbit_hour_paths=lambda: self.paths(variables),
                                       rawbbit_parquet_structure=lambda: 'event_id Nullable(String), app_id Nullable(String)',
                                       rawbbit_app_predicate=lambda: self.predicate(variables))
            sql = environment.from_string((ROOT / 'models' / 'ingestion' / 'rawbbit_events_load.sql').read_text()).render()
            self.assertIn("filename = '" + self.paths(variables)[0] + "'", sql)
            self.assertIn(self.predicate(variables), sql)


if __name__ == '__main__':
    unittest.main()
