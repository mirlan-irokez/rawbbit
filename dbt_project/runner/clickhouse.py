"""Bounded HTTP control plane using the loader identity, never MCP credentials."""
import json
import ssl
import time
import urllib.parse
import urllib.request
import uuid

from config import ConfigError
from storage import load


class ControlError(Exception):
    pass


def literal(value):
    return "'" + value.replace('\\', '\\\\').replace("'", "\\'") + "'"


STRING_COLUMNS = ('event_id', 'app_id', 'environment', 'event_name', 'user_id',
                  'user_pseudo_id', 'session_id', 'platform', 'app_version', 'os_version',
                  'device_model', 'locale', 'timezone', 'event_params_json', 'user_properties_json',
                  'traffic_source_json', 'geo_json', 'consent_json', 'ingest_request_id',
                  'ingest_user_agent', 'ingest_ip_hash', 'nats_stream')
EXPECTED_COLUMNS = {name: ('String' if name in ('app_id', 'environment', 'event_name',
                                              'user_pseudo_id') else 'Nullable(String)')
                    for name in STRING_COLUMNS}
EXPECTED_COLUMNS.update(event_time="DateTime64(3, 'UTC')", event_date='Date',
                        received_time="Nullable(DateTime64(3, 'UTC'))", nats_sequence='Nullable(Int64)')


class ClickHouse:
    def __init__(self, env, timeout=10, settle_seconds=30):
        secure = env.get('CLICKHOUSE_SECURE', '0')
        verify = env.get('CLICKHOUSE_VERIFY', '1')
        if secure not in ('0', '1') or verify not in ('0', '1'):
            raise ConfigError('invalid ClickHouse TLS switches')
        host = env.get('CLICKHOUSE_HOST', 'clickhouse')
        port = env.get('CLICKHOUSE_PORT', '8123')
        if not host or any(c in host for c in '/@?#\\') or not port.isdigit() or not 0 < int(port) < 65536:
            raise ConfigError('invalid ClickHouse HTTP endpoint')
        self.url = ('https' if secure == '1' else 'http') + '://' + host + ':' + port
        self.user = env.get('CLICKHOUSE_DBT_USER', '')
        self.password = env.get('CLICKHOUSE_DBT_PASSWORD', '')
        if not self.user or not self.password:
            raise ConfigError('loader credentials required')
        self.timeout = timeout
        self.settle_seconds = settle_seconds
        context = ssl.create_default_context()
        if verify == '0':
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        # Do not send credentials to proxy environment or follow HTTP redirects.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                                  urllib.request.HTTPSHandler(context=context), NoRedirect())

    def query(self, sql):
        query_id = str(uuid.uuid4())
        params = urllib.parse.urlencode({'query_id': query_id,
                                        'param_control_query_id': query_id,
                                        'log_comment': 'rawbbit-dbt-control',
                                        'max_execution_time': self.timeout,
                                        'wait_end_of_query': 1})
        request = urllib.request.Request(self.url + '/?' + params,
                                         data=(sql + ' FORMAT JSON').encode(), method='POST',
                                         headers={'X-ClickHouse-User': self.user,
                                                  'X-ClickHouse-Key': self.password})
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                return json.loads(response.read(4 * 1024 * 1024))['data']
        except Exception:
            raise ControlError('ClickHouse control request failed') from None

    def ready(self, database):
        try:
            table = self.query('SELECT engine, partition_key, sorting_key FROM system.tables WHERE database = '
                               + literal(database) + " AND name = 'events'")
            if (len(table) != 1 or table[0]['engine'] != 'MergeTree'
                    or table[0]['partition_key'] != 'toYYYYMM(event_date)'
                    or table[0]['sorting_key'] != 'app_id, environment, event_name, event_date, user_pseudo_id, event_time'):
                return False
            columns = self.query('SELECT name, type FROM system.columns WHERE database = '
                                 + literal(database) + " AND table = 'events'")
            actual = {row['name']: row['type'].replace('LowCardinality(', '', 1)[:-1]
                      if row['type'].startswith('LowCardinality(') else row['type'] for row in columns}
            if actual != EXPECTED_COLUMNS:
                return False
            # Metadata visibility alone does not establish SELECT permission.
            self.query('SELECT event_id FROM `' + database + '`.events LIMIT 0')
            return True
        except ControlError:
            return False

    def settle(self, tag, database, request_dir):
        """Never equate client timeout, child exit or KILL response with stopped writes.

        Check ALL loader/tagged processes (metadata included). Mutations have no
        reliable ownership field; never kill them. Any pending mutation in this
        database's events/adapter scratch namespace keeps the fence closed.
        Requests without an acknowledged response additionally require a terminal
        query_log event, even when the process list is empty. Missing log visibility
        or dropped logs fail closed, requiring operator review.
        """
        deadline = time.monotonic() + self.settle_seconds
        try:
            pending = []
            for path in request_dir.glob('*.json'):
                record = load(path)
                if record['status'] != 'confirmed':
                    pending.append(record['query_id'])
            while True:
                owned = 'user = ' + literal(self.user) + " AND Settings['log_comment'] = " + literal(tag)
                active = self.query('SELECT query_id FROM system.processes WHERE ' + owned)
                if active:
                    # No broad-user or database-only cancellation, ever.
                    self.query('KILL QUERY WHERE ' + owned + ' SYNC')
                    active = self.query('SELECT query_id FROM system.processes WHERE ' + owned)
                # A dedicated loader identity is required. Never kill unowned
                # work, but do not overlap writes with any other loader query.
                other = self.query('SELECT query_id FROM system.processes WHERE user = '
                                   + literal(self.user) + ' AND query_id != {control_query_id:String}')
                databases = database if isinstance(database, (tuple, list)) else [database]
                mutations = self.query('SELECT mutation_id FROM system.mutations WHERE database IN ('
                                       + ','.join(literal(db) for db in databases) + ')'
                                       + " AND (table = 'events' OR startsWith(table, 'events__dbt')) AND NOT is_done")
                terminal = set()
                if pending:
                    rows = self.query('SELECT query_id FROM system.query_log WHERE user = '
                                      + literal(self.user) + ' AND log_comment = ' + literal(tag)
                                      + ' AND query_id IN (' + ','.join(literal(q) for q in pending) + ')'
                                      + " AND type IN ('QueryFinish', 'ExceptionBeforeStart', 'ExceptionWhileProcessing')")
                    terminal = {row['query_id'] for row in rows}
                if not active and not other and not mutations and set(pending) <= terminal:
                    return True
                if time.monotonic() >= deadline:
                    return False
                time.sleep(0.25)
        except Exception:
            return False
