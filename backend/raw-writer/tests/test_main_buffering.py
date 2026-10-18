from __future__ import annotations

import asyncio
import json
import os
import signal
import unittest

from raw_writer.main import RawWriterService, _install_signal_handlers, buffer_threshold
from raw_writer.settings import Settings


def _settings(**overrides) -> Settings:
    values = {
        "RAW_STORAGE_BACKEND": "s3",
        "S3_ENDPOINT_URL": "http://seaweedfs:8333",
        "S3_BUCKET": "events",
        "S3_ACCESS_KEY": "key",
        "S3_SECRET_KEY": "secret",
        "RAW_MAX_EVENTS_PER_FILE": 10000,
        "RAW_MAX_BYTES_PER_FILE": 50 * 1024 * 1024,
        "RAW_FLUSH_INTERVAL_SECONDS": 60,
        "NATS_ACK_WAIT_SECONDS": 300,
    }
    values.update(overrides)
    return Settings(**values)


class _Msg:
    def __init__(self, app_id: str, index: int) -> None:
        self.data = json.dumps(
            {
                "event_id": f"{app_id}-{index}",
                "app_id": app_id,
                "event_name": "level_completed",
                "event_timestamp": "2026-01-01T10:00:00Z",
            }
        ).encode("utf-8")
        self.metadata = None
        self.acked = 0
        self.naked = 0

    async def ack(self) -> None:
        self.acked += 1

    async def nak(self) -> None:
        self.naked += 1

    async def term(self) -> None:
        pass


class _Uploader:
    def __init__(self, failing_app_ids: set[str] | None = None) -> None:
        self.failing_app_ids = failing_app_ids or set()
        self.object_paths: list[str] = []

    def upload_file(self, local_path, object_path: str) -> None:
        if any(f"app_id={app_id}/" in object_path for app_id in self.failing_app_ids):
            raise OSError("upload failed")
        self.object_paths.append(object_path)


class _Consumer:
    def __init__(self, max_ack_pending: int | Exception = 10000, batches: list[list[_Msg]] | None = None) -> None:
        self.max_ack_pending_value = max_ack_pending
        self.batches = list(batches or [])
        self.fetch_calls = 0
        self.fetches_while_stopping = 0
        self.closed = False
        self.on_fetch = None
        self.is_stopping = lambda: False

    async def connect(self) -> None:
        pass

    async def max_ack_pending(self) -> int:
        if isinstance(self.max_ack_pending_value, Exception):
            raise self.max_ack_pending_value
        return self.max_ack_pending_value

    async def fetch(self) -> list[_Msg]:
        self.fetch_calls += 1
        if self.is_stopping():
            self.fetches_while_stopping += 1
        batch = self.batches.pop(0) if self.batches else []
        if self.on_fetch is not None:
            self.on_fetch()
        # A real fetch blocks up to NATS_FETCH_TIMEOUT_SECONDS; give the signal time to land.
        await asyncio.sleep(0.05)
        return batch

    async def close(self) -> None:
        self.closed = True


def _messages(sizes: dict[str, int]) -> list[_Msg]:
    """Messages interleaved across apps so no partition fills up first."""
    pending = {app_id: list(range(count)) for app_id, count in sizes.items()}
    out: list[_Msg] = []
    while any(pending.values()):
        for app_id, indexes in pending.items():
            if indexes:
                out.append(_Msg(app_id, indexes.pop(0)))
    return out


def _service(consumer: _Consumer, uploader: _Uploader, **settings) -> RawWriterService:
    return RawWriterService(settings=_settings(**settings), consumer=consumer, uploader=uploader)


async def _feed(service: RawWriterService, msgs: list[_Msg]) -> None:
    await service.refresh_threshold()
    for msg in msgs:
        await service.handle_message(msg)


class BufferThresholdTests(unittest.TestCase):
    def test_threshold_is_share_of_max_ack_pending(self) -> None:
        self.assertEqual(buffer_threshold(10000, None), 8000)
        self.assertEqual(buffer_threshold(1000, None), 800)

    def test_override_only_lowers_threshold(self) -> None:
        self.assertEqual(buffer_threshold(10000, 500), 500)
        self.assertEqual(buffer_threshold(10000, 20000), 8000)


class PressureFlushTests(unittest.IsolatedAsyncioTestCase):
    async def test_flushes_before_any_partition_reaches_file_limit(self) -> None:
        uploader = _Uploader()
        service = _service(_Consumer(max_ack_pending=1250), uploader)
        msgs = _messages({f"app{i}": 100 for i in range(10)})

        await _feed(service, msgs)

        self.assertEqual(service.threshold, 1000)
        self.assertGreater(len(uploader.object_paths), 0)
        self.assertLess(service.buffered_events, service.threshold // 2)
        flushed = [m for m in msgs if m.acked]
        self.assertEqual(len(flushed), 1000 - service.buffered_events)
        self.assertTrue(all(m.acked == 1 and m.naked == 0 for m in flushed))
        self.assertTrue(all(len(state.msgs) < 10000 for state in service.buffers.values()))

    async def test_flushes_largest_partitions_first(self) -> None:
        uploader = _Uploader()
        service = _service(_Consumer(max_ack_pending=1250), uploader)
        msgs = _messages({"big": 500, "mid": 300, "small": 150, "tiny": 50})

        await _feed(service, msgs)

        flushed_apps = sorted(p.split("app_id=")[1].split("/")[0] for p in uploader.object_paths)
        self.assertEqual(flushed_apps, ["big", "mid"])
        self.assertEqual(service.buffered_events, 200)
        self.assertLess(service.buffered_events, service.threshold // 2)
        self.assertEqual(sorted(key[0] for key in service.buffers), ["small", "tiny"])

    async def test_upload_failure_naks_only_that_partition(self) -> None:
        uploader = _Uploader(failing_app_ids={"big"})
        service = _service(_Consumer(max_ack_pending=1250), uploader)
        msgs = _messages({"big": 500, "mid": 300, "small": 150, "tiny": 50})

        await _feed(service, msgs)

        big = [m for m in msgs if b'"app_id": "big"' in m.data]
        mid = [m for m in msgs if b'"app_id": "mid"' in m.data]
        self.assertTrue(all(m.naked == 1 and m.acked == 0 for m in big))
        self.assertTrue(all(m.acked == 1 and m.naked == 0 for m in mid))
        self.assertNotIn("big", {key[0] for key in service.buffers})
        self.assertEqual(service.buffered_events, sum(len(s.msgs) for s in service.buffers.values()))

    async def test_per_partition_count_flush_still_applies(self) -> None:
        uploader = _Uploader()
        service = _service(_Consumer(max_ack_pending=10000), uploader, RAW_MAX_EVENTS_PER_FILE=100)
        msgs = _messages({"solo": 250})

        await _feed(service, msgs)

        self.assertEqual(len(uploader.object_paths), 2)
        self.assertEqual(service.buffered_events, 50)


class ThresholdRefreshTests(unittest.IsolatedAsyncioTestCase):
    async def test_threshold_comes_from_consumer_and_override_only_lowers_it(self) -> None:
        service = _service(_Consumer(max_ack_pending=10000), _Uploader())
        await service.refresh_threshold()
        self.assertEqual(service.threshold, 8000)

        lowered = _service(_Consumer(max_ack_pending=10000), _Uploader(), RAW_MAX_BUFFERED_EVENTS=500)
        await lowered.refresh_threshold()
        self.assertEqual(lowered.threshold, 500)

        not_raised = _service(_Consumer(max_ack_pending=1000), _Uploader(), RAW_MAX_BUFFERED_EVENTS=5000)
        await not_raised.refresh_threshold()
        self.assertEqual(not_raised.threshold, 800)

    async def test_threshold_follows_consumer_changes(self) -> None:
        consumer = _Consumer(max_ack_pending=10000)
        service = _service(consumer, _Uploader())
        await service.refresh_threshold()
        consumer.max_ack_pending_value = 1000
        await service.refresh_threshold()
        self.assertEqual(service.threshold, 800)

    async def test_failed_refresh_keeps_last_known_threshold(self) -> None:
        consumer = _Consumer(max_ack_pending=5000)
        service = _service(consumer, _Uploader())
        await service.refresh_threshold()
        consumer.max_ack_pending_value = ConnectionError("nats unavailable")

        with self.assertLogs("raw_writer", level="WARNING"):
            await service.refresh_threshold()

        self.assertEqual(service.threshold, 4000)

    async def test_unlimited_consumer_falls_back_to_setting(self) -> None:
        service = _service(_Consumer(max_ack_pending=-1), _Uploader(), NATS_MAX_ACK_PENDING=2000)
        await service.refresh_threshold()
        self.assertEqual(service.threshold, 1600)


class ShutdownTests(unittest.IsolatedAsyncioTestCase):
    async def _run_until_sigterm(self, uploader: _Uploader) -> tuple[_Consumer, list[_Msg]]:
        msgs = _messages({"app0": 30, "app1": 20})
        consumer = _Consumer(max_ack_pending=10000, batches=[msgs])
        service = _service(consumer, uploader)
        _install_signal_handlers(service)
        consumer.is_stopping = service._stopping.is_set
        # Signal while waiting on the next fetch, with the first batch already buffered.
        consumer.on_fetch = lambda: os.kill(os.getpid(), signal.SIGTERM) if consumer.fetch_calls == 2 else None
        try:
            await service.run()
            await service.close()
        finally:
            loop = asyncio.get_running_loop()
            loop.remove_signal_handler(signal.SIGTERM)
            loop.remove_signal_handler(signal.SIGINT)
        return consumer, msgs

    async def test_sigterm_flushes_and_acks_buffer(self) -> None:
        uploader = _Uploader()
        consumer, msgs = await self._run_until_sigterm(uploader)

        self.assertEqual(consumer.fetches_while_stopping, 0)
        self.assertTrue(consumer.closed)
        self.assertTrue(all(m.acked == 1 for m in msgs))
        self.assertEqual(len(uploader.object_paths), 2)

    async def test_sigterm_naks_buffer_when_upload_fails(self) -> None:
        uploader = _Uploader(failing_app_ids={"app0", "app1"})
        consumer, msgs = await self._run_until_sigterm(uploader)

        self.assertEqual(consumer.fetches_while_stopping, 0)
        self.assertTrue(all(m.naked == 1 and m.acked == 0 for m in msgs))

    async def test_stop_does_not_wait_for_a_blocked_fetch(self) -> None:
        msgs = _messages({"app0": 10})

        class _BlockingConsumer(_Consumer):
            async def fetch(self) -> list[_Msg]:
                self.fetch_calls += 1
                if self.fetch_calls == 1:
                    return msgs
                await asyncio.sleep(3600)  # far longer than any stop grace period
                return []

        consumer = _BlockingConsumer(max_ack_pending=10000)
        uploader = _Uploader()
        service = _service(consumer, uploader)
        asyncio.get_running_loop().call_later(0.1, service.request_stop)

        await asyncio.wait_for(service.run(), timeout=2)
        await service.close()

        self.assertEqual(consumer.fetch_calls, 2)
        self.assertTrue(all(m.acked == 1 for m in msgs))
        self.assertEqual(len(uploader.object_paths), 1)


if __name__ == "__main__":
    unittest.main()
