from __future__ import annotations

import asyncio
import json
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException
from nats.js.api import StorageType
from nats.js.errors import NotFoundError

from collector_api import main
from collector_api.nats_publisher import BatchPublishError, NATSPublisher


class _FakeJetStream:
    def __init__(
        self, delay_seconds: float = 0.0, failing_msg_ids: set[str] | None = None
    ) -> None:
        self.delay_seconds = delay_seconds
        self.failing_msg_ids = failing_msg_ids or set()
        self.inflight = 0
        self.max_inflight = 0
        self.published: list[tuple[str, dict, str]] = []

    async def publish(
        self, subject: str, payload: bytes, headers: dict[str, str]
    ) -> None:
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

        await asyncio.gather(
            publisher.publish_batch(_messages(30)),
            publisher.publish_batch(_messages(30)),
        )

        self.assertEqual(js.max_inflight, 8)
        self.assertEqual(len(js.published), 60)

    async def test_failure_reports_counts_after_attempting_every_event(self) -> None:
        js = _FakeJetStream(failing_msg_ids={"app:e3"})

        with self.assertRaises(BatchPublishError) as ctx:
            await _publisher(js).publish_batch(_messages(10))

        self.assertEqual(len(js.published), 10)
        self.assertEqual((ctx.exception.published, ctx.exception.failed), (9, 1))
        self.assertIsInstance(ctx.exception.first_error, TimeoutError)


def _stream_info(
    *,
    subjects: list[str] | None = None,
    storage: StorageType = StorageType.FILE,
    did_create: bool | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        config=SimpleNamespace(
            subjects=subjects if subjects is not None else ["events.>"],
            storage=storage,
        ),
        did_create=did_create,
    )


class _ControlledStreamJetStream:
    def __init__(
        self,
        *,
        existing=None,
        add_result=None,
        add_error: BaseException | None = None,
        created_after_add_error=None,
        info_error: BaseException | None = None,
        followup_info_error: BaseException | None = None,
    ) -> None:
        self.info = existing
        self.add_result = add_result
        self.add_error = add_error
        self.created_after_add_error = created_after_add_error
        self.info_error = info_error
        self.followup_info_error = followup_info_error
        self.calls: list[str] = []
        self.add_configs = []

    async def stream_info(self, name: str):
        self.calls.append("stream_info")
        if self.info_error is not None:
            raise self.info_error
        if len(self.calls) > 1 and self.followup_info_error is not None:
            raise self.followup_info_error
        if self.info is None:
            raise NotFoundError()
        return self.info

    async def add_stream(self, config):
        self.calls.append("add_stream")
        self.add_configs.append(config)
        if self.add_error is not None:
            self.info = self.created_after_add_error
            raise self.add_error
        self.info = self.add_result
        return self.add_result


class _TwoWorkerStreamJetStream:
    """Both workers observe NotFound before either idempotent add completes."""

    def __init__(self) -> None:
        self.present = False
        self.initial_info_calls = 0
        self.all_initial_lookups_done = asyncio.Event()
        self.calls: list[str] = []
        self.add_configs = []

    async def stream_info(self, name: str):
        self.calls.append("stream_info")
        if self.initial_info_calls < 2:
            self.initial_info_calls += 1
            if self.initial_info_calls == 2:
                self.all_initial_lookups_done.set()
            await self.all_initial_lookups_done.wait()
            raise NotFoundError()
        if not self.present:
            raise NotFoundError()
        return _stream_info(did_create=False)

    async def add_stream(self, config):
        self.calls.append("add_stream")
        self.add_configs.append(config)
        await asyncio.sleep(0)
        self.present = True
        # The second call is an idempotent success. did_create is deliberately
        # false and must not override the approved absent->present observation.
        return _stream_info(did_create=len(self.add_configs) == 1)


class EnsureStreamTests(unittest.IsolatedAsyncioTestCase):
    async def test_existing_compatible_stream_is_checked_without_add(self) -> None:
        js = _ControlledStreamJetStream(existing=_stream_info(did_create=True))

        with self.assertLogs("collector_api.nats", level="INFO") as logs:
            await _publisher(js)._ensure_stream()

        self.assertEqual(js.calls, ["stream_info"])
        self.assertIn("stream_exists_and_compatible stream=EVENTS", logs.output[0])

    async def test_not_found_then_successful_add_logs_created_even_if_did_create_false(
        self,
    ) -> None:
        for did_create in (True, False, None):
            with self.subTest(did_create=did_create):
                info = _stream_info(did_create=did_create)
                if did_create is None:
                    del info.did_create
                js = _ControlledStreamJetStream(add_result=info)

                with self.assertLogs("collector_api.nats", level="INFO") as logs:
                    await _publisher(js)._ensure_stream()

                self.assertEqual(js.calls, ["stream_info", "add_stream"])
                self.assertIn("stream_created stream=EVENTS", logs.output[0])
                config = js.add_configs[0]
                self.assertEqual(config.name, "EVENTS")
                self.assertEqual(config.subjects, ["events.>"])
                self.assertEqual(config.storage, StorageType.FILE)
                self.assertEqual(config.max_age, 86400)
                self.assertEqual(config.duplicate_window, 600)

    async def test_successful_add_result_is_checked_for_compatibility(self) -> None:
        js = _ControlledStreamJetStream(
            add_result=_stream_info(subjects=["other.>"], did_create=True)
        )

        with self.assertRaisesRegex(RuntimeError, "incompatible subjects"):
            await _publisher(js)._ensure_stream()

    async def test_successful_add_result_must_use_file_storage(self) -> None:
        js = _ControlledStreamJetStream(
            add_result=_stream_info(storage=StorageType.MEMORY)
        )

        with self.assertRaisesRegex(
            RuntimeError, "uses storage 'StorageType.MEMORY', expected FILE"
        ):
            await _publisher(js)._ensure_stream()

    async def test_existing_incompatible_subjects_and_storage_keep_errors(self) -> None:
        cases = (
            (_stream_info(subjects=["other.>"]), "incompatible subjects"),
            (
                _stream_info(storage=StorageType.MEMORY),
                "uses storage 'StorageType.MEMORY', expected FILE",
            ),
        )
        for info, message in cases:
            with self.subTest(message=message):
                js = _ControlledStreamJetStream(existing=info)
                with self.assertRaisesRegex(RuntimeError, message):
                    await _publisher(js)._ensure_stream()
                self.assertEqual(js.calls, ["stream_info"])

    async def test_non_not_found_stream_info_error_is_not_treated_as_absence(
        self,
    ) -> None:
        error = PermissionError("stream info denied")
        js = _ControlledStreamJetStream(info_error=error)

        with self.assertRaises(PermissionError) as caught:
            await _publisher(js)._ensure_stream()

        self.assertIs(caught.exception, error)
        self.assertEqual(js.calls, ["stream_info"])

    async def test_add_error_race_confirms_existing_compatible_stream(self) -> None:
        js = _ControlledStreamJetStream(
            add_error=RuntimeError("stream already exists"),
            created_after_add_error=_stream_info(did_create=False),
        )

        with self.assertLogs("collector_api.nats", level="INFO") as logs:
            await _publisher(js)._ensure_stream()

        self.assertEqual(js.calls, ["stream_info", "add_stream", "stream_info"])
        self.assertIn("stream_exists_and_compatible stream=EVENTS", logs.output[0])
        self.assertNotIn("stream_created", "\n".join(logs.output))

    async def test_add_error_without_confirmed_stream_reraises_original_error(
        self,
    ) -> None:
        error = RuntimeError("stream create failed")
        js = _ControlledStreamJetStream(add_error=error)

        with self.assertRaises(RuntimeError) as caught:
            await _publisher(js)._ensure_stream()

        self.assertIs(caught.exception, error)
        self.assertEqual(js.calls, ["stream_info", "add_stream", "stream_info"])

    async def test_followup_info_failure_preserves_original_add_error(self) -> None:
        add_error = RuntimeError("create failed")
        info_error = PermissionError("follow-up info denied")
        js = _ControlledStreamJetStream(
            add_error=add_error,
            followup_info_error=info_error,
        )

        with self.assertRaises(RuntimeError) as caught:
            await _publisher(js)._ensure_stream()

        self.assertIs(caught.exception, add_error)
        self.assertIs(caught.exception.__cause__, info_error)
        self.assertEqual(js.calls, ["stream_info", "add_stream", "stream_info"])

    async def test_add_error_race_with_incompatible_stream_keeps_compatibility_error(
        self,
    ) -> None:
        error = RuntimeError("stream name already in use")
        js = _ControlledStreamJetStream(
            add_error=error,
            created_after_add_error=_stream_info(subjects=["other.>"]),
        )

        with self.assertRaisesRegex(RuntimeError, "incompatible subjects") as caught:
            await _publisher(js)._ensure_stream()

        self.assertIs(caught.exception.__cause__, error)
        self.assertEqual(js.calls, ["stream_info", "add_stream", "stream_info"])

    async def test_two_workers_starting_at_once_both_connect(self) -> None:
        js = _TwoWorkerStreamJetStream()
        first, second = _publisher(js), _publisher(js)

        with self.assertLogs("collector_api.nats", level="INFO") as logs:
            await asyncio.gather(first._ensure_stream(), second._ensure_stream())

        self.assertTrue(js.present)
        self.assertEqual(js.calls.count("stream_info"), 2)
        self.assertEqual(js.calls.count("add_stream"), 2)
        self.assertEqual(
            "\n".join(logs.output).count("stream_created stream=EVENTS"), 2
        )


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
            patch.object(
                main.settings,
                "collector_api_keys_json",
                json.dumps({"k1": "com.example.game"}),
            ),
            patch.object(main, "publisher", _publisher(js)),
        )

    async def test_msg_ids_are_app_and_event_id(self) -> None:
        js = _FakeJetStream()
        keys, publisher = self._patches(js)
        with keys, publisher:
            response = await main.ingest_events(
                _Request(_batch_body(5)), x_api_key="k1"
            )

        self.assertEqual(response.accepted_events, 5)
        self.assertEqual(
            sorted(msg_id for _, _, msg_id in js.published),
            [f"com.example.game:evt-{i}" for i in range(5)],
        )
        self.assertTrue(
            all(subject == "events.com.example.game" for subject, _, _ in js.published)
        )

    async def test_one_failed_publish_returns_503_and_logs_counts(self) -> None:
        js = _FakeJetStream(failing_msg_ids={"com.example.game:evt-2"})
        keys, publisher = self._patches(js)
        with (
            keys,
            publisher,
            self.assertLogs("collector_api", level="ERROR") as logs,
            self.assertRaises(HTTPException) as ctx,
        ):
            await main.ingest_events(_Request(_batch_body(5)), x_api_key="k1")

        self.assertEqual(ctx.exception.status_code, 503)
        self.assertEqual(len(js.published), 5)
        self.assertIn("published_events=4 failed_events=1", logs.output[0])


if __name__ == "__main__":
    unittest.main()
