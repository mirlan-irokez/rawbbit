"""Closed-schema security and bounded transport tests (no host dbt required)."""
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'runner'))
from progress import (CALLBACK_LOCK_SECONDS, Callback, Channel, DRAIN_BYTES, DRAIN_FRAMES, Emitter, FD_ENV,
                      MAX_FRAME, MAX_LINE, NODES, QUEUE_LINES, STATUSES, child_callback,
                      encode, node_body, prefix, validate)
import dbt_child

POISON = 'SECRET https://user:password@host/private?token=xxx SELECT * FROM private\nINJECT'


def event(identity, name='NodeStart', status='success', elapsed=1.25):
    resource, node_name, _ = NODES[identity]
    node = NS(unique_id=identity, resource_type=resource, node_name=node_name,
              node_path=POISON, meta={'secret': POISON}, node_relation=POISON)
    result = NS(status=status, execution_time=elapsed, message=POISON,
                adapter_response={'code': POISON}, num_failures=POISON)
    return NS(info=NS(name=name, msg=POISON), data=NS(node_info=node, run_result=result))


def frame(seq=1):
    return {'v': 1, 'seq': seq, 'event': 'start', 'resource': 'model',
            'node': 'rawbbit_events_load', 'status': 'START'}


class ProgressTests(unittest.TestCase):
    def channel(self):
        channel = Channel()
        self.addCleanup(channel.close)
        return channel

    def test_all_approved_nodes_statuses_and_poisoned_fields(self):
        channel = self.channel()
        callback = child_callback({FD_ENV: str(channel.write_fd)})
        for identity, (resource, _, _) in NODES.items():
            callback(event(identity))
            status = 'pass' if resource == 'test' else 'success'
            callback(event(identity, 'NodeFinished', status))
        callback.failure()
        frames = channel.drain()
        self.assertEqual(len(frames), 9)
        self.assertEqual(len(callback.seen), 9)
        text = json.dumps(frames) + ''.join(node_body(f) or '' for f in frames)
        for word in ('SECRET', 'password', 'private', 'INJECT', 'adapter_response', 'num_failures'):
            self.assertNotIn(word, text)

    def test_status_mapping_each_independently_and_no_guessed_finish(self):
        for identity, (resource, _, label) in NODES.items():
            for status, mapped in STATUSES[resource].items():
                channel = self.channel()
                cb = Callback(channel.write_fd)
                cb(event(identity, 'NodeFinished', status))
                output = channel.drain()
                self.assertEqual(output[0]['status'], mapped)
                self.assertEqual(output[0]['node'], label)
                self.assertNotIn('failure_count', output[0])
                self.assertNotIn('code', output[0])
        channel = self.channel()
        cb = Callback(channel.write_fd)
        cb(event(next(iter(NODES))))
        self.assertEqual([f['event'] for f in channel.drain()], ['start'])

    def test_callback_unknowns_bad_types_and_no_arbitrary_stringification(self):
        class Bomb:
            def __str__(self):
                raise AssertionError('must not stringify')
        channel = self.channel()
        cb = Callback(channel.write_fd)
        identity = next(iter(NODES))
        for field, value in [('unique_id', POISON), ('node_name', POISON),
                             ('resource_type', 'test'), ('unique_id', Bomb())]:
            item = event(identity)
            setattr(item.data.node_info, field, value)
            cb(item)
        item = event(identity, 'NodeFinished', POISON)
        item.info.msg = Bomb()
        item.data.run_result.message = Bomb()
        item.data.run_result.adapter_response = Bomb()
        cb(item)
        cb(NS(info=NS(name='GenericExceptionOnRun'), data=Bomb()))
        self.assertEqual(channel.drain(), [])
        item = event(identity, 'NodeFinished', elapsed=float('nan'))
        cb(item)
        self.assertNotIn('elapsed_seconds', channel.drain()[0])

    def test_schema_rejects_unknown_keys_controls_counts_codes_nonfinite(self):
        self.assertTrue(validate(frame()))
        for key, value in [('node', POISON), ('resource', 'seed'), ('status', 'PASS'),
                           ('seq', True), ('seq', 0), ('v', True), ('category', 'compilation_error'),
                           ('job_id', POISON), ('failure_count', 3), ('code', 'UNKNOWN_IDENTIFIER')]:
            bad = dict(frame(), **{key: value})
            self.assertFalse(validate(bad))
            self.assertIsNone(encode(bad))
        for value in (True, -1, float('nan'), float('inf'), '1', 172801):
            bad = dict(frame(), event='finish', status='OK', elapsed_seconds=value)
            self.assertFalse(validate(bad))

    def test_split_overflow_truncated_duplicate_unknown_and_sequence(self):
        channel = self.channel()
        data = encode(frame())
        os.write(channel.write_fd, data[:10])
        self.assertEqual(channel.drain(), [])
        os.write(channel.write_fd, data[10:])
        self.assertEqual(channel.drain(), [frame()])
        os.write(channel.write_fd, b'x' * (MAX_FRAME + 20))
        self.assertEqual(channel.drain(), [])
        self.assertLessEqual(len(channel.partial), MAX_FRAME)
        os.write(channel.write_fd, b'\n' + encode(frame(2)))
        self.assertEqual(channel.drain(), [frame(2)])
        duplicate = encode(frame(3)).replace(b'"v":1', b'"v":1,"v":1')
        unknown = (json.dumps(dict(frame(3), secret=POISON)) + '\n').encode()
        os.write(channel.write_fd, duplicate + b'{bad}\n' + unknown)
        self.assertEqual(channel.drain(), [])
        os.write(channel.write_fd, encode(frame(2)) + b'{"v":1')
        channel.close_writer()
        self.assertEqual(channel.drain(), [])
        self.assertEqual(channel.drain(), [])  # No EOF wait or partial-frame promotion.

    def test_child_fd_validation_flags_and_missing_descriptor(self):
        for raw in ('', '0', '1', '2', '-1', '99999999', POISON):
            self.assertIsNone(child_callback({FD_ENV: raw}))
        channel = self.channel()
        os.set_blocking(channel.write_fd, True)
        self.assertIsNone(child_callback({FD_ENV: str(channel.write_fd)}))
        self.assertTrue(os.get_blocking(channel.write_fd))
        os.set_blocking(channel.write_fd, False)
        self.assertIsNotNone(child_callback({FD_ENV: str(channel.write_fd)}))
        self.assertFalse(os.get_inheritable(channel.write_fd))
        self.assertIsNone(child_callback({FD_ENV: str(channel.read_fd)}))
        with open(__file__, 'rb') as regular:
            self.assertIsNone(child_callback({FD_ENV: str(regular.fileno())}))

    def test_child_setup_failure_falls_back_only_before_invoke(self):
        calls = []
        class API:
            def __init__(self, callbacks=None):
                calls.append(('init', callbacks is not None))
                if callbacks:
                    raise RuntimeError('logging initialization')
            def invoke(self, args):
                calls.append(('invoke', tuple(args)))
                return NS(success=True)
        with patch.dict(sys.modules, {'dbt.cli.main': NS(dbtRunner=API)}), \
                patch('dbt_child.install_tracking'), patch('progress.child_callback', side_effect=OSError):
            self.assertEqual(dbt_child.main(['build']), 0)
        self.assertEqual(calls, [('init', False), ('invoke', ('build',))])
        calls.clear()
        channel = self.channel()
        fd = channel.write_fd
        callback = Callback(fd)
        with patch.dict(sys.modules, {'dbt.cli.main': NS(dbtRunner=API)}), \
                patch('dbt_child.install_tracking'), patch('progress.child_callback', return_value=callback):
            self.assertEqual(dbt_child.main(['build']), 0)
        channel.write_fd = None
        self.assertEqual(calls, [('init', True), ('init', False), ('invoke', ('build',))])
        with self.assertRaises(OSError):
            os.fstat(fd)

    def test_child_terminal_never_reads_exception_and_no_launched_replay(self):
        class Result:
            success = False
            @property
            def exception(self):
                raise AssertionError('exception contents must never be touched')
        calls = []
        class API:
            def __init__(self, callbacks=None):
                pass
            def invoke(self, args):
                calls.append(args)
                return Result()
        channel = self.channel()
        callback = Callback(channel.write_fd)
        with patch.dict(sys.modules, {'dbt.cli.main': NS(dbtRunner=API)}), \
                patch('dbt_child.install_tracking'), patch('progress.child_callback', return_value=callback):
            self.assertEqual(dbt_child.main(['build']), 1)
        channel.write_fd = None
        self.assertEqual(len(calls), 1)
        self.assertEqual(channel.drain()[0]['category'], 'execution_failed')
        with patch.dict(sys.modules, {'dbt.cli.main': NS(dbtRunner=API)}), \
                patch('dbt_child.install_tracking'), patch('progress.child_callback', return_value=None), \
                patch.object(API, 'invoke', side_effect=RuntimeError('genuine invocation failure')) as invoke:
            with self.assertRaises(RuntimeError):
                dbt_child.main(['build'])
            invoke.assert_called_once()
        with patch('dbt_child.install_tracking', side_effect=RuntimeError('tracking failure')):
            with self.assertRaises(RuntimeError):
                dbt_child.main(['build'])

    def test_full_closed_pipe_and_concurrent_callback_bounded(self):
        channel = self.channel()
        while True:
            try:
                os.write(channel.write_fd, b'x' * MAX_FRAME)
            except BlockingIOError:
                break
        cb = Callback(channel.write_fd)
        start = time.monotonic()
        cb(event(next(iter(NODES))))
        self.assertLess(time.monotonic() - start, 0.1)
        os.close(channel.read_fd)
        channel.read_fd = None
        cb.failure()
        channel = self.channel()
        cb = Callback(channel.write_fd)
        def flood():
            for _ in range(500):
                for identity in NODES:
                    cb(event(identity))
                    cb(event(identity, 'NodeFinished', 'pass' if NODES[identity][0] == 'test' else 'success'))
        workers = [threading.Thread(target=flood) for _ in range(4)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=2)
            self.assertFalse(worker.is_alive())
        self.assertLessEqual(len(cb.seen), 8)
        self.assertLessEqual(len(channel.drain()), 8)

    def test_concurrent_distinct_nodes_keep_all_events_sequence_and_dedup(self):
        channel = self.channel()
        cb = Callback(channel.write_fd)
        barrier = threading.Barrier(len(NODES))
        def produce(identity):
            barrier.wait(timeout=2)
            cb(event(identity))
            cb(event(identity, 'NodeFinished', 'pass' if NODES[identity][0] == 'test' else 'success'))
        workers = [threading.Thread(target=produce, args=(identity,)) for identity in NODES]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=2)
            self.assertFalse(worker.is_alive())
        frames = channel.drain()
        self.assertEqual(len(frames), 8)
        self.assertEqual([f['seq'] for f in frames], list(range(1, 9)))
        expected = {(resource, label, action) for resource, _, label in NODES.values()
                    for action in ('start', 'finish')}
        self.assertEqual({(f['resource'], f['node'], f['event']) for f in frames}, expected)
        for identity in NODES:
            cb(event(identity))
            cb(event(identity, 'NodeFinished', 'pass' if NODES[identity][0] == 'test' else 'success'))
        self.assertEqual(channel.drain(), [])
        cb.failure()
        cb.failure()
        terminal = channel.drain()
        self.assertEqual(len(terminal), 1)
        self.assertEqual(terminal[0]['seq'], 9)

    def test_held_callback_lock_has_bounded_wait_for_nodes_and_terminal(self):
        channel = self.channel()
        cb = Callback(channel.write_fd)
        cb.lock.acquire()
        try:
            for invoke in (lambda: cb(event(next(iter(NODES)))), cb.failure):
                start = time.monotonic()
                invoke()
                elapsed = time.monotonic() - start
                self.assertGreaterEqual(elapsed, CALLBACK_LOCK_SECONDS * .8)
                self.assertLess(elapsed, CALLBACK_LOCK_SECONDS + .1)
            self.assertEqual(cb.seq, 0)
            self.assertEqual(cb.seen, set())
            self.assertEqual(channel.drain(), [])
        finally:
            cb.lock.release()

    def test_drain_budget_and_setup_descriptor_cleanup(self):
        channel = self.channel()
        with patch('progress.os.read', return_value=b'\n' * DRAIN_BYTES) as read:
            self.assertEqual(channel.drain(), [])
            read.assert_called_once_with(channel.read_fd, DRAIN_BYTES)
        with patch('progress.os.read', return_value=b''.join(encode(frame(i)) for i in range(1, 200))):
            self.assertEqual(len(channel.drain()), DRAIN_FRAMES)
        captured = []
        original = os.pipe
        def pipe():
            ends = original()
            captured.extend(ends)
            return ends
        with patch('progress.os.pipe', side_effect=pipe), patch('progress.os.set_blocking', side_effect=OSError):
            with self.assertRaises(OSError):
                Channel()
        for fd in captured:
            with self.assertRaises(OSError):
                os.fstat(fd)

    def test_context_fixed_parent_owned_identifiers(self):
        record = {'job_id': str(uuid.uuid4()), 'attempt_id': str(uuid.uuid4()),
                  'kind': 'primary', 'actual_database': 'team_a', 'app_id': POISON}
        self.assertNotIn('SECRET', prefix(record))
        for key in ('job_id', 'attempt_id', 'kind', 'actual_database'):
            with self.assertRaises(ValueError):
                prefix(dict(record, **{key: POISON}))

    def test_closed_sink_and_emitter_initialization_failure(self):
        channel = self.channel()
        os.close(channel.read_fd)
        channel.read_fd = None
        emitter = Emitter(channel.write_fd)
        emitter.emit('fixed safe line')
        emitter.close()
        self.assertFalse(emitter.thread.is_alive())
        with patch('progress.threading.Thread.start', side_effect=RuntimeError):
            with self.assertRaises(RuntimeError):
                Emitter()

    def test_emitter_rejects_oversized_and_non_ascii_lines_before_queueing(self):
        channel = self.channel()
        emitter = Emitter(channel.write_fd)
        try:
            emitter.emit('x' * MAX_LINE)
            emitter.emit('\u2603')
            emitter.emit({'message': POISON})
            emitter.emit('fixed safe line')
        finally:
            emitter.close()
        self.assertEqual(os.read(channel.read_fd, DRAIN_BYTES), b'fixed safe line\n')

    def test_blocked_stderr_never_changes_flags_or_blocks_process_exit(self):
        source = '''
import os, time, fcntl
from progress import Emitter, QUEUE_LINES
r,w=os.pipe()
os.set_blocking(w,False)
while True:
 try: os.write(w,b'x'*512)
 except BlockingIOError: break
os.set_blocking(w,True)
os.dup2(w,2)
before=fcntl.fcntl(2,fcntl.F_GETFL)
e=Emitter()
for _ in range(10000): e.emit('fixed safe line')
assert e.queue.qsize()<=QUEUE_LINES
start=time.monotonic();e.close()
assert time.monotonic()-start<0.3
assert fcntl.fcntl(2,fcntl.F_GETFL)==before
print('done',flush=True)
'''
        result = subprocess.run([sys.executable, '-c', source],
                                env=dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / 'runner')),
                                capture_output=True, text=True, timeout=3)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, 'done\n')


if __name__ == '__main__':
    unittest.main()
