"""Pinned dbt-clickhouse 1.9.8 entrypoint with durable HTTP request ownership.

Adapter cancel() only closes the connection: it is NOT server cancellation.
ChHttpClient command/query/columns_in_query forward settings through kwargs,
including database setup, introspection and incremental temporary-table DDL.
Initialization HTTP metadata calls outside these methods are read-only.
"""
import os
import sys
import uuid
import urllib.parse
from pathlib import Path

from storage import load, save


def install_tracking():
    from importlib.metadata import version
    if version('dbt-clickhouse') != '1.9.8':
        raise RuntimeError('unsupported adapter version')
    if version('clickhouse-connect') != '1.6.0':
        raise RuntimeError('unsupported HTTP client version')
    from dbt.adapters.clickhouse.httpclient import ChHttpClient
    if getattr(ChHttpClient, '_rawbbit_tracking_installed', False):
        return
    ChHttpClient._rawbbit_tracking_installed = True
    original_create = ChHttpClient._create_client

    class NoReplayHTTP:
        """Disable urllib3 retries and the backend's implicit remote-close retry.

        A broken pipe is not proof ClickHouse did not accept a write. Each
        generated request ID may be dispatched exactly once by this process.
        """
        def __init__(self, http):
            self.http = http
            self.seen = set()

        def __getattr__(self, name):
            return getattr(self.http, name)

        def request(self, method, url, **kwargs):
            from urllib3.exceptions import HTTPError
            params = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
            query_id = params.get('query_id', [None])[0]
            if query_id:
                if query_id in self.seen:
                    raise HTTPError('automatic replay refused')
                self.seen.add(query_id)
                path = Path(os.environ['RAWBBIT_DBT_REQUEST_DIR']) / (query_id + '.json')
                if path.exists():
                    record = load(path)
                    # Only fixed nonsecret ownership/synchronization settings.
                    record['transport_settings'] = {key: params.get(key, [None])[0]
                                                    for key in ('log_comment', 'mutations_sync', 'lightweight_deletes_sync')}
                    save(path, record)
            kwargs['retries'] = 0
            return self.http.request(method, url, **kwargs)

    def create(self, credentials):
        client = original_create(self, credentials)
        # Do not silently replay a write after HTTP transport failure.
        client.query_retries = 0
        client._backend.http_retries = 0
        client._backend.http = NoReplayHTTP(client._backend.http)
        return client

    ChHttpClient._create_client = create
    for name in ('command', 'query', 'columns_in_query'):
        original = getattr(ChHttpClient, name)

        def tracked(self, sql, _original=original, **kwargs):
            query_id = str(uuid.uuid4())
            path = Path(os.environ['RAWBBIT_DBT_REQUEST_DIR']) / (query_id + '.json')
            save(path, {'query_id': query_id, 'status': 'dispatched'})
            # All requests, not only model query_settings, carry the attempt tag.
            settings = dict(kwargs.get('settings') or {})
            # In clickhouse-connect 1.6.0 transport_settings are HTTP HEADERS.
            # Its validated settings query_id is a supported transport key,
            # merged into URL query parameters for both command and SELECT.
            settings['query_id'] = query_id
            settings['log_comment'] = os.environ['RAWBBIT_DBT_ATTEMPT_TAG']
            kwargs['settings'] = settings
            result = _original(self, sql, **kwargs)
            record = load(path)
            record['status'] = 'confirmed'
            save(path, record)
            return result

        setattr(ChHttpClient, name, tracked)


def main(args):
    install_tracking()
    from dbt.cli.main import dbtRunner
    callback = None
    try:
        from progress import child_callback
        callback = child_callback(os.environ)
    except Exception:
        pass
    # Only initialization is fail-open. NEVER retry an invocation that launched.
    try:
        api = dbtRunner(callbacks=[callback]) if callback else dbtRunner()
    except Exception:
        api = dbtRunner()
    try:
        result = api.invoke(args)
        if callback and not result.success:
            try:
                callback.failure()
            except Exception:
                pass
        # Never inspect/print result.exception (it can contain credentials or SQL).
        return 0 if result.success else 1
    finally:
        if callback:
            try:
                os.close(callback.fd)
            except OSError:
                pass


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
