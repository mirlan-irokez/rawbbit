"""Validate the entire public route snapshot before permitting writes."""
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

IDENTIFIER = re.compile(r'[A-Za-z_][A-Za-z0-9_]{0,127}\Z')
APP = re.compile(r'[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}\Z')


class ConfigError(Exception):
    pass


def identifier(value):
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise ConfigError('unsafe database or dataset identifier')
    return value


def app_id(value):
    if not isinstance(value, str) or not APP.fullmatch(value) or value in ('.', '..'):
        raise ConfigError('unsafe app partition identifier')
    return value


def positive(env, key, default, maximum=86400):
    value = env.get(key, str(default))
    if not re.fullmatch(r'[1-9][0-9]*', value) or int(value) > maximum:
        raise ConfigError('invalid bounded positive integer: ' + key)
    return int(value)


def no_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ConfigError('duplicate JSON field')
        result[key] = value
    return result


@dataclass(frozen=True)
class Snapshot:
    enabled: bool
    default: str
    routes: tuple
    revision: str
    document: dict


def snapshot(env):
    switch = env.get('RAWBBIT_DBT_ROUTING_ENABLED', '0')
    if switch not in ('0', '1'):
        raise ConfigError('RAWBBIT_DBT_ROUTING_ENABLED must be 0 or 1')
    enabled = switch == '1'
    default = identifier(env.get('CLICKHOUSE_DATABASE', 'analytics'))
    routes = []
    if enabled:
        if env.get('RAWBBIT_RAW_LOAD_MODE', 'legacy') != 'dbt':
            raise ConfigError('routing requires RAWBBIT_RAW_LOAD_MODE=dbt')
        try:
            path = Path(env.get('RAWBBIT_DBT_ROUTES_FILE', '/app/runtime/dbt-routes.json'))
            if path.stat().st_size > 1048576:
                raise ConfigError('route snapshot too large')
            value = json.loads(path.read_text(), object_pairs_hook=no_duplicate_keys)
        except (OSError, ValueError, UnicodeError):
            raise ConfigError('enabled route snapshot unreadable or malformed') from None
        if (not isinstance(value, dict) or set(value) != {'version', 'routes'}
                or type(value['version']) is not int or value['version'] != 1
                or not isinstance(value['routes'], list) or len(value['routes']) > 1000):
            raise ConfigError('unsupported route snapshot')
        seen_apps, seen_targets = set(), set()
        for row in value['routes']:
            if not isinstance(row, dict) or set(row) != {'app_id', 'dataset_id', 'database', 'table'}:
                raise ConfigError('route must contain only resolved public fields')
            app_id(row['app_id'])
            identifier(row['dataset_id'])
            identifier(row['database'])
            if row['table'] != 'events':
                raise ConfigError('only events table is supported')
            target = (row['database'], row['table'])
            if row['app_id'] in seen_apps or target in seen_targets or target == (default, 'events'):
                raise ConfigError('duplicate or conflicting route')
            seen_apps.add(row['app_id'])
            seen_targets.add(target)
            routes.append(dict(row))
        routes.sort(key=lambda row: row['app_id'])
    document = {'version': 1, 'enabled': enabled, 'default_database': default,
                'default_table': 'events', 'routes': routes}
    revision = hashlib.sha256(json.dumps(document, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return Snapshot(enabled, default, tuple(routes), revision, document)
