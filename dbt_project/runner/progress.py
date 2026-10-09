"""Closed, best-effort dbt progress protocol. Never inspect free-form dbt data.

Verified against core 1.9.10/common 1.39.0: NodeStart/NodeFinished
EventMsg.data.node_info and NodeFinished.data.run_result.status/execution_time.
Counts, backend codes and exception classifications are deliberately unsupported.
"""
import datetime as dt
import fcntl
import json
import math
import os
import queue
import re
import stat
import threading
import uuid

FD_ENV = 'RAWBBIT_DBT_PROGRESS_FD'  # Internal inherited descriptor, not operator config.
MAX_FRAME = 512  # POSIX minimum PIPE_BUF; includes trailing newline.
DRAIN_BYTES = 16384
DRAIN_FRAMES = 64
QUEUE_LINES = 64
MAX_LINE = 1024
MAX_SEQ = 2147483647
CALLBACK_LOCK_SECONDS = 0.01
NODES = {
    'model.rawbbit.rawbbit_events_load': ('model', 'rawbbit_events_load', 'rawbbit_events_load'),
    'test.rawbbit.not_null_rawbbit_events_load_event_id.e0f6e8b4e5':
        ('test', 'not_null_rawbbit_events_load_event_id', 'not_null_event_id'),
    'test.rawbbit.not_null_rawbbit_events_load_app_id.e1528aaf5b':
        ('test', 'not_null_rawbbit_events_load_app_id', 'not_null_app_id'),
    'test.rawbbit.not_null_rawbbit_events_load_event_time.cf3ed7e3eb':
        ('test', 'not_null_rawbbit_events_load_event_time', 'not_null_event_time'),
}
LABELS = {(resource, label) for resource, _, label in NODES.values()}
STATUSES = {'model': {'success': 'OK', 'error': 'ERROR', 'skipped': 'SKIP',
                      'partial success': 'PARTIAL'},
            'test': {'pass': 'PASS', 'error': 'ERROR', 'fail': 'FAIL',
                     'warn': 'WARN', 'skipped': 'SKIP'}}


def validate(frame):
    """Validate exact keys/types/closed values at both ends of the channel."""
    if type(frame) is not dict or type(frame.get('v')) is not int or frame['v'] != 1:
        return False
    if type(frame.get('seq')) is not int or not 1 <= frame['seq'] <= MAX_SEQ:
        return False
    if frame.get('event') == 'diagnostic':
        return set(frame) == {'v', 'seq', 'event', 'category'} and frame['category'] == 'execution_failed'
    required = {'v', 'seq', 'event', 'resource', 'node', 'status'}
    if set(frame) not in (required, required | {'elapsed_seconds'}):
        return False
    resource, node, status = frame['resource'], frame['node'], frame['status']
    if any(type(x) is not str for x in (resource, node, status)) or (resource, node) not in LABELS:
        return False
    if frame['event'] == 'start':
        return status == 'START' and set(frame) == required
    if frame['event'] != 'finish' or status not in STATUSES[resource].values():
        return False
    if 'elapsed_seconds' in frame:
        elapsed = frame['elapsed_seconds']
        if type(elapsed) not in (int, float) or not math.isfinite(elapsed) or not 0 <= elapsed <= 172800:
            return False
    return True


def encode(frame):
    if not validate(frame):
        return None
    data = (json.dumps(frame, separators=(',', ':'), allow_nan=False) + '\n').encode('ascii')
    return data if len(data) <= MAX_FRAME else None


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate field')
        result[key] = value
    return result


class Callback:
    """At most nine atomic writes; serialization waits at most 10 ms, never for I/O."""
    def __init__(self, fd):
        self.fd = fd
        self.lock = threading.Lock()
        self.seen = set()
        self.seq = 0

    def _write(self, frame):
        self.seq += 1
        frame.update(v=1, seq=self.seq)
        data = encode(frame)
        if data:
            os.write(self.fd, data)  # Parent-created FIFO is already O_NONBLOCK.

    def __call__(self, event):
        if not self.lock.acquire(timeout=CALLBACK_LOCK_SECONDS):
            return
        try:
            name = event.info.name
            if type(name) is not str or name not in ('NodeStart', 'NodeFinished'):
                return
            node = event.data.node_info
            identity = node.unique_id
            if type(identity) is not str or len(identity) > 160:
                return
            approved = NODES.get(identity)
            if approved is None:
                return
            resource, expected_name, label = approved
            if (type(node.resource_type) is not str or type(node.node_name) is not str
                    or node.resource_type != resource or node.node_name != expected_name):
                return
            key = (identity, name)
            if key in self.seen:
                return
            frame = {'event': 'start' if name == 'NodeStart' else 'finish',
                     'resource': resource, 'node': label, 'status': 'START'}
            if name == 'NodeFinished':
                result = event.data.run_result
                status = result.status
                if type(status) is not str or status not in STATUSES[resource]:
                    return
                frame['status'] = STATUSES[resource][status]
                elapsed = result.execution_time
                if type(elapsed) in (int, float) and math.isfinite(elapsed) and 0 <= elapsed <= 172800:
                    frame['elapsed_seconds'] = round(elapsed, 3)
            self.seen.add(key)  # No retries/replay when the channel is full or closed.
            self._write(frame)
        except Exception:
            pass  # Only logging, never tracking or invocation, is fail-open.
        finally:
            self.lock.release()

    def failure(self):
        if not self.lock.acquire(timeout=CALLBACK_LOCK_SECONDS):
            return
        try:
            if ('terminal', 'failure') not in self.seen:
                self.seen.add(('terminal', 'failure'))
                self._write({'event': 'diagnostic', 'category': 'execution_failed'})
        except Exception:
            pass
        finally:
            self.lock.release()


def child_callback(env):
    """Do not set flags on an arbitrary inherited fd/shared open-file description."""
    try:
        raw = env.get(FD_ENV, '')
        if not re.fullmatch(r'[0-9]{1,8}', raw):
            return None
        fd = int(raw)
        flags = fcntl.fcntl(fd, fcntl.F_GETFL)
        if (fd < 3 or not stat.S_ISFIFO(os.fstat(fd).st_mode)
                or not flags & os.O_NONBLOCK or flags & os.O_ACCMODE != os.O_WRONLY
                or os.fpathconf(fd, 'PC_PIPE_BUF') < MAX_FRAME):
            return None
        os.set_inheritable(fd, False)
        return Callback(fd)
    except Exception:
        return None


class Channel:
    def __init__(self):
        self.read_fd = self.write_fd = None
        self.partial = b''
        self.discard = False
        self.seq = 0
        try:
            self.read_fd, self.write_fd = os.pipe()
            for fd in (self.read_fd, self.write_fd):
                os.set_blocking(fd, False)
                os.set_inheritable(fd, False)
            if os.fpathconf(self.write_fd, 'PC_PIPE_BUF') < MAX_FRAME:
                raise ValueError('unsupported pipe')
        except Exception:
            self.close()
            raise

    def close_writer(self):
        if self.write_fd is not None:
            try:
                os.close(self.write_fd)
            except OSError:
                pass
            self.write_fd = None

    def close(self):
        self.close_writer()
        if self.read_fd is not None:
            try:
                os.close(self.read_fd)
            except OSError:
                pass
            self.read_fd = None
        self.partial = b''

    def drain(self):
        """Single read caps work even under flooding/descendants holding the pipe."""
        try:
            data = os.read(self.read_fd, DRAIN_BYTES)
        except (OSError, TypeError):
            return []
        frames = []
        for chunk in data.splitlines(keepends=True):
            ended = chunk.endswith(b'\n')
            if not self.discard:
                if len(self.partial) + len(chunk) > MAX_FRAME:
                    self.partial = b''
                    self.discard = True
                else:
                    self.partial += chunk
            if ended:
                if not self.discard:
                    try:
                        frame = json.loads(self.partial, object_pairs_hook=_pairs)
                        if validate(frame) and frame['seq'] > self.seq:
                            self.seq = frame['seq']
                            if len(frames) < DRAIN_FRAMES:
                                frames.append(frame)
                    except (ValueError, UnicodeError, RecursionError):
                        pass
                self.partial = b''
                self.discard = False
        return frames


class Emitter:
    """One daemon per invocation, bounded queue, raw writes, bounded shutdown.

    The daemon alone can block on stderr. No Python buffered stream is touched,
    no stderr OFD flags are changed, and no duplicate sink fd is leaked. A stuck
    write is abandoned at process exit (not joined indefinitely).
    """
    def __init__(self, fd=2):
        self.fd = fd
        self.queue = queue.Queue(maxsize=QUEUE_LINES)
        self.closed = threading.Event()
        self.thread = threading.Thread(target=self._run, name='dbt-progress', daemon=True)
        self.thread.start()

    def emit(self, line):
        try:
            if not self.closed.is_set() and type(line) is str and len(line) < MAX_LINE:
                data = (line + '\n').encode('ascii')
                if len(data) <= MAX_LINE:
                    self.queue.put_nowait(data)
        except Exception:
            pass

    def _run(self):
        try:
            while not self.closed.is_set() or not self.queue.empty():
                try:
                    data = self.queue.get(timeout=0.05)
                except queue.Empty:
                    continue
                # Write once. Partial/broken writes are not retried.
                os.write(self.fd, data)
        except Exception:
            self.closed.set()

    def close(self):
        self.closed.set()
        self.thread.join(timeout=0.1)


def prefix(record):
    """Only validated parent-owned identifiers; no app/request/user correlation."""
    job, attempt = record['job_id'], record['attempt_id']
    if any(type(value) is not str or str(uuid.UUID(value)) != value for value in (job, attempt)):
        raise ValueError('invalid correlation')
    kind, database = record['kind'], record['actual_database']
    if kind not in ('default', 'primary', 'fallback') or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,127}', database):
        raise ValueError('invalid context')
    now = dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    return f'{now} [dbt-progress job={job} attempt={attempt} kind={kind} db={database}] '


def node_body(frame):
    if not validate(frame) or frame['event'] == 'diagnostic':
        return None
    body = f"{frame['status']} {frame['resource']} {frame['node']}"
    if 'elapsed_seconds' in frame:
        body += f" elapsed_seconds={frame['elapsed_seconds']:.3f}"
    if frame['status'] == 'FAIL' and frame['resource'] == 'test':
        body += ' category=test_failed'
    elif frame['status'] == 'ERROR':
        body += ' category=node_error'
    return body
