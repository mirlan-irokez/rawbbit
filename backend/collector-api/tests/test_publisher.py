from __future__ import annotations

import asyncio
import json
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException
from nats.js.api import StorageType

from collector_api import main
from collector_api.nats_publisher import BatchPublishError, NATSPublisher


class _FakeJetStream:
    def __init__(self, delay_seconds: float = 0.0, failing_msg_ids: set[str] | None = None) -> None:
        self.delay_seconds = delay_seconds
        self.failing_msg_ids = failing_msg_ids or set()
        self.inflight = 0
        self.max_inflight = 0
        self.published: list[tuple[str, dict, str]] = []

    async def publish(self, subject: str, payload: bytes, headers: dict[str, str]) -> None:
        self.inflight += 1
        self.max_inflight = max(self.max_inflight, self.inflight)
        try:
            await asyncio.sleep(self.delay_seconds)
            msg_id = headers["Nats-Msg-Id"]
            self.published.append((subject, json.loads(payload), msg_id))
            if msg_id in self.failing_msg_ids:
                raise TimeoutError("no JetStream ACK")
        finally:
            self.inflight -= 1


def _publisher(js, max_inflight_publishes: int = 512) -> NATSPublisher:
    publisher = NATSPublisher(
        nats_url="nats://nats:4222",
        stream_name="EVENTS",
        subject_prefix="events",
        stream_max_age_seconds=86400,
        duplicate_window_seconds=600,
        max_inflight_publishes=max_inflight_publishes,
    )
    publisher._js = js
    return publisher


def _messages(count: int) -> list[tuple[str, dict, str]]:
    return [("events.app", {"event_id": f"e{i}"}, f"app:e{i}") for i in range(count)]


class PublishBatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_batch_is_published_concurrently(self) -> None:
        js = _FakeJetStream(delay_seconds=0.05)
        started = time.perf_counter()

        await _publisher(js).publish_batch(_messages(50))

        elapsed = time.perf_counter() - started
        self.assertLess(elapsed, 50 * 0.05 / 5)
        self.assertEqual(len(js.published), 50)
        self.assertEqual(js.max_inflight, 50)

    async def test_inflight_publishes_are_capped(self) -> None:
        js = _FakeJetStream(delay_seconds=0.01)
        publisher = _publisher(js, max_inflight_publishes=8)

        await asyncio.gather(publisher.publish_batch(_messages(30)), publisher.publish_batch(_messages(30)))

        self.assertEqual(js.max_inflight, 8)
        self.assertEqual(len(js.published), 60)

    async def test_failure_reports_counts_after_attempting_every_event(self) -> None:
        js = _FakeJetStream(failing_msg_ids={"app:e3"})

        with self.assertRaises(BatchPublishError) as ctx:
            await _publisher(js).publish_batch(_messages(10))

        self.assertEqual(len(js.published), 10)
        self.assertEqual((ctx.exception.published, ctx.exception.failed), (9, 1))
        self.assertIsInstance(ctx.exception.first_error, TimeoutError)


class _StreamRaceJetStream:
    """add_stream succeeds once; a concurrent second call reports the stream already exists."""

    def __init__(self) -> None:
        self.created = False

    async def add_stream(self, config) -> None:
        await asyncio.sleep(0)
        if self.created:
            raise RuntimeError("stream name already in use")
        self.created = True

    async def stream_info(self, name: str):
        return SimpleNamespace(config=SimpleNamespace(subjects=["events.>"], storage=StorageType.FILE))


class EnsureStreamTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_workers_starting_at_once_both_connect(self) -> None:
        js = _StreamRaceJetStream()
        first, second = _publisher(js), _publisher(js)

        await asyncio.gather(first._ensure_stream(), second._ensure_stream())

        self.assertTrue(js.created)


class _Request:
    def __init__(self, body: bytes) -> None:
        self._body = body
        self.headers = {"user-agent": "test"}
        self.client = SimpleNamespace(host="203.0.113.8")

    async def stream(self):
        yield self._body


def _batch_body(count: int) -> bytes:
    events = [
        {
            "event_id": f"evt-{i}",
            "app_id": "com.example.game",
            "event_name": "level_completed",
            "event_timestamp": "2026-03-20T18:42:15.123Z",
        }
        for i in range(count)
    ]
    return json.dumps({"events": events}).encode("utf-8")


class IngestEndpointTests(unittest.IsolatedAsyncioTestCase):
    def _patches(self, js):
        return (
            patch.object(main.settings, "collector_api_keys_json", json.dumps({"k1": "com.example.game"})),
            patch.object(main, "publisher", _publisher(js)),
        )

    async def test_msg_ids_are_app_and_event_id(self) -> None:
        js = _FakeJetStream()
        keys, publisher = self._patches(js)
        with keys, publisher:
            response = await main.ingest_events(_Request(_batch_body(5)), x_api_key="k1")

        self.assertEqual(response.accepted_events, 5)
        self.assertEqual(sorted(msg_id for _, _, msg_id in js.published), [f"com.example.game:evt-{i}" for i in range(5)])
        self.assertTrue(all(subject == "events.com.example.game" for subject, _, _ in js.published))

    async def test_one_failed_publish_returns_503_and_logs_counts(self) -> None:
        js = _FakeJetStream(failing_msg_ids={"com.example.game:evt-2"})
        keys, publisher = self._patches(js)
        with keys, publisher, self.assertLogs("collector_api", level="ERROR") as logs:
            with self.assertRaises(HTTPException) as ctx:
                await main.ingest_events(_Request(_batch_body(5)), x_api_key="k1")

        self.assertEqual(ctx.exception.status_code, 503)
        self.assertEqual(len(js.published), 5)
        self.assertIn("published_events=4 failed_events=1", logs.output[0])


if __name__ == "__main__":
    unittest.main()
