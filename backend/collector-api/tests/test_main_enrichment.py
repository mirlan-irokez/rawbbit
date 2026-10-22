from __future__ import annotations

import asyncio
import json
import unittest
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException

from collector_api import main
from collector_api.schemas import EventEnvelope
from collector_api.utils import hash_ip


def _event_input(**overrides: object) -> dict[str, object]:
    event: dict[str, object] = {
        "event_id": "evt-1",
        "app_id": "com.example.game",
        "event_name": "level_completed",
        "event_timestamp": "2026-03-20T18:42:15.123Z",
    }
    event.update(overrides)
    return event


def _event_dump(
    overrides: dict[str, object] | None = None, *, exclude_unset: bool = False
) -> dict[str, object]:
    event = _event_input(**(overrides or {}))
    envelope = EventEnvelope.model_validate(event)
    return envelope.model_dump(mode="json", exclude_unset=exclude_unset)


class _DummyRequest:
    def __init__(
        self,
        headers: dict[str, str] | None = None,
        client_host: str | None = "10.0.0.1",
    ) -> None:
        self.headers = {key.lower(): value for key, value in (headers or {}).items()}
        self.client = (
            SimpleNamespace(host=client_host) if client_host is not None else None
        )


class _BodyRequest(_DummyRequest):
    def __init__(
        self,
        body: bytes,
        headers: dict[str, str] | None = None,
        client_host: str | None = "10.0.0.1",
    ) -> None:
        super().__init__(headers=headers, client_host=client_host)
        self._body = body

    async def stream(self):
        yield self._body


class _ResolverStub:
    def __init__(self, response: dict[str, str] | None) -> None:
        self._response = response

    def lookup_country(self, _ip_value: str) -> dict[str, str] | None:
        return self._response


class _PublisherStub:
    def __init__(self) -> None:
        self.messages: list[tuple[str, dict[str, object], str]] = []
        self.calls = 0

    async def publish_batch(self, messages: list[tuple[str, dict, str]]) -> None:
        self.calls += 1
        self.messages = messages


def _request_body(events: list[dict[str, object]]) -> bytes:
    return json.dumps({"events": events}).encode("utf-8")


class MainEnrichmentTests(unittest.TestCase):
    def _assert_fresh_equal_utc_timestamps(
        self,
        enriched: dict[str, object],
        before: datetime,
        after: datetime,
    ) -> None:
        ingest = enriched["ingest"]
        self.assertIsInstance(ingest, dict)
        nested_received_at = ingest["received_at"]
        top_level_received_at = enriched["received_at"]
        self.assertIsInstance(top_level_received_at, str)
        self.assertIsInstance(nested_received_at, str)
        self.assertEqual(top_level_received_at, nested_received_at)

        parsed = datetime.fromisoformat(str(top_level_received_at))
        self.assertEqual(parsed.tzinfo, UTC)
        self.assertLessEqual(before, parsed)
        self.assertLessEqual(parsed, after)

    def test_omitted_null_empty_and_all_null_ingest_get_server_metadata(self) -> None:
        ip_address = "203.0.113.8"
        request = _DummyRequest(
            headers={"user-agent": "collector-test/1.0", "x-forwarded-for": ip_address}
        )
        cfg = main.settings.model_copy(
            update={"store_raw_ip": False, "ip_hash_salt": "test-salt"}
        )
        variants = [
            ("omitted", {}, True),
            ("null", {"ingest": None}, False),
            ("empty", {"ingest": {}}, False),
            (
                "all-null",
                {
                    "ingest": {
                        "user_agent": None,
                        "ip_hash": None,
                        "ip": None,
                        "received_at": None,
                        "request_id": None,
                    }
                },
                False,
            ),
        ]

        for name, overrides, exclude_unset in variants:
            with self.subTest(ingest=name):
                payload = _event_dump(overrides, exclude_unset=exclude_unset)
                before = datetime.now(UTC)
                with patch.object(main, "geoip_resolver", _ResolverStub(None)):
                    enriched = main._enrich_event(payload, request, "batch-req-1", cfg)
                after = datetime.now(UTC)

                self._assert_fresh_equal_utc_timestamps(enriched, before, after)
                ingest = enriched["ingest"]
                self.assertEqual(ingest["request_id"], "batch-req-1")
                self.assertEqual(ingest["user_agent"], "collector-test/1.0")
                self.assertEqual(ingest["ip_hash"], hash_ip(ip_address, "test-salt"))
                self.assertNotIn("ip", ingest)

    def test_spoofed_metadata_is_overwritten_and_other_event_data_is_preserved(
        self,
    ) -> None:
        ip_address = "203.0.113.19"
        spoofed_received_at = "2001-01-01T00:00:00+00:00"
        payload = _event_dump(
            {
                "received_at": spoofed_received_at,
                "ingest": {
                    "user_agent": "spoofed-agent",
                    "ip_hash": "spoofed-hash",
                    "ip": "198.51.100.44",
                    "received_at": spoofed_received_at,
                    "request_id": "spoofed-request",
                },
                "geo": {"country": "Wrong", "country_code": "WR", "city": "Oulu"},
                "event_params": {"level": 17},
            }
        )
        request = _DummyRequest(
            headers={"user-agent": "real-agent/2.0", "x-forwarded-for": ip_address}
        )
        cfg = main.settings.model_copy(
            update={"store_raw_ip": True, "ip_hash_salt": "test-salt"}
        )
        before = datetime.now(UTC)
        with patch.object(
            main,
            "geoip_resolver",
            _ResolverStub({"country": "Finland", "country_code": "FI"}),
        ):
            enriched = main._enrich_event(payload, request, "server-request", cfg)
        after = datetime.now(UTC)

        ingest = enriched["ingest"]
        self._assert_fresh_equal_utc_timestamps(enriched, before, after)
        self.assertEqual(ingest["request_id"], "server-request")
        self.assertEqual(ingest["user_agent"], "real-agent/2.0")
        self.assertEqual(ingest["ip_hash"], hash_ip(ip_address, "test-salt"))
        self.assertEqual(ingest["ip"], ip_address)
        self.assertNotEqual(enriched["received_at"], spoofed_received_at)
        self.assertNotEqual(ingest["received_at"], spoofed_received_at)
        self.assertEqual(
            enriched["geo"],
            {
                "country": "Finland",
                "country_code": "FI",
                "city": "Oulu",
                "region": None,
                "timezone": None,
            },
        )
        self.assertEqual(enriched["event_params"], {"level": 17})

    def test_raw_ip_setting_controls_whether_extracted_ip_is_published(self) -> None:
        ip_address = "203.0.113.22"
        request = _DummyRequest(headers={"x-forwarded-for": ip_address})
        payload = _event_dump({"ingest": {"ip": "198.51.100.1"}})

        for store_raw_ip in (True, False):
            with self.subTest(store_raw_ip=store_raw_ip):
                cfg = main.settings.model_copy(
                    update={"store_raw_ip": store_raw_ip, "ip_hash_salt": "raw-ip-test"}
                )
                with patch.object(main, "geoip_resolver", _ResolverStub(None)):
                    enriched = main._enrich_event(
                        payload.copy(), request, "request-1", cfg
                    )

                ingest = enriched["ingest"]
                if store_raw_ip:
                    self.assertEqual(ingest["ip"], ip_address)
                else:
                    self.assertNotIn("ip", ingest)
                self.assertEqual(ingest["ip_hash"], hash_ip(ip_address, "raw-ip-test"))

    def test_ip_hash_is_derived_from_valid_request_ip(self) -> None:
        ip_address = "203.0.113.23"
        request = _DummyRequest(headers={"x-forwarded-for": ip_address})
        payload = _event_dump({"ingest": {"ip_hash": "spoofed-hash"}})

        for store_raw_ip in (True, False):
            with self.subTest(store_raw_ip=store_raw_ip):
                cfg = main.settings.model_copy(
                    update={"store_raw_ip": store_raw_ip, "ip_hash_salt": "hash-test"}
                )
                with patch.object(main, "geoip_resolver", _ResolverStub(None)):
                    enriched = main._enrich_event(
                        payload.copy(), request, "request-1", cfg
                    )

                self.assertEqual(
                    enriched["ingest"]["ip_hash"], hash_ip(ip_address, "hash-test")
                )

    def test_missing_ip_does_not_fall_back_to_body_ip_metadata(self) -> None:
        payload = _event_dump(
            {"ingest": {"ip_hash": "spoofed-hash", "ip": "198.51.100.20"}}
        )
        request = _DummyRequest(headers={}, client_host="not-an-ip")
        cfg = main.settings.model_copy(
            update={"store_raw_ip": True, "ip_hash_salt": "test-salt"}
        )

        with patch.object(
            main, "geoip_resolver", _ResolverStub({"country": "Finland"})
        ):
            enriched = main._enrich_event(payload, request, "server-request", cfg)

        ingest = enriched["ingest"]
        self.assertIsNone(ingest["ip_hash"])
        self.assertNotIn("ip", ingest)
        self.assertIsNone(enriched["geo"])

    def test_missing_user_agent_overwrites_spoofed_body_value(self) -> None:
        payload = _event_dump({"ingest": {"user_agent": "spoofed-agent"}})
        request = _DummyRequest(headers={}, client_host=None)
        cfg = main.settings.model_copy(update={"store_raw_ip": False})

        with patch.object(main, "geoip_resolver", _ResolverStub(None)):
            enriched = main._enrich_event(payload, request, "server-request", cfg)

        self.assertIsNone(enriched["ingest"]["user_agent"])

    def test_batch_response_id_is_used_for_every_enriched_event(self) -> None:
        ip_address = "203.0.113.28"
        spoofed_received_at = "2001-01-01T00:00:00+00:00"
        events = [
            _event_input(
                event_id=f"evt-{index}",
                received_at=spoofed_received_at,
                ingest={
                    "request_id": "client-request",
                    "received_at": spoofed_received_at,
                    "user_agent": "client-agent",
                    "ip_hash": "client-hash",
                    "ip": "198.51.100.10",
                },
            )
            for index in range(2)
        ]
        request = _BodyRequest(
            _request_body(events),
            headers={"user-agent": "batch-agent/1.0", "x-forwarded-for": ip_address},
        )
        publisher = _PublisherStub()
        key_map = patch.object(
            main.settings, "collector_api_keys_json", '{"test-key":"com.example.game"}'
        )
        raw_ip = patch.object(main.settings, "store_raw_ip", True)
        publisher_patch = patch.object(main, "publisher", publisher)
        resolver_patch = patch.object(main, "geoip_resolver", _ResolverStub(None))
        before = datetime.now(UTC)
        with key_map, raw_ip, publisher_patch, resolver_patch:
            response = asyncio.run(main.ingest_events(request, x_api_key="test-key"))
        after = datetime.now(UTC)

        self.assertEqual(response.accepted_events, 2)
        self.assertEqual(publisher.calls, 1)
        self.assertEqual(len(publisher.messages), 2)
        for _, event, _ in publisher.messages:
            self.assertEqual(event["ingest"]["request_id"], response.request_id)
            self._assert_fresh_equal_utc_timestamps(event, before, after)
            self.assertEqual(event["ingest"]["user_agent"], "batch-agent/1.0")
            self.assertEqual(event["ingest"]["ip"], ip_address)
            self.assertNotEqual(event["received_at"], spoofed_received_at)

    def test_invalid_ingest_metadata_types_return_422_without_publishing(self) -> None:
        invalid_overrides = [
            ("ingest is not an object", {"ingest": ["invalid"]}),
            ("request ID is not a string", {"ingest": {"request_id": 123}}),
            ("receipt time is invalid", {"received_at": "not-a-timestamp"}),
        ]
        publisher = _PublisherStub()
        key_map = patch.object(
            main.settings, "collector_api_keys_json", '{"test-key":"com.example.game"}'
        )
        publisher_patch = patch.object(main, "publisher", publisher)

        with key_map, publisher_patch:
            for name, overrides in invalid_overrides:
                with self.subTest(metadata=name):
                    request = _BodyRequest(_request_body([_event_input(**overrides)]))
                    with self.assertRaises(HTTPException) as context:
                        asyncio.run(main.ingest_events(request, x_api_key="test-key"))
                    self.assertEqual(context.exception.status_code, 422)
                    self.assertEqual(publisher.calls, 0)
                    self.assertEqual(publisher.messages, [])

    def test_geoip_overrides_country_fields_and_preserves_other_geo_fields(
        self,
    ) -> None:
        payload = _event_dump(
            {"geo": {"country": "Wrong", "country_code": "WR", "city": "Oulu"}}
        )
        request = _DummyRequest(headers={"x-forwarded-for": "203.0.113.8"})

        with patch.object(
            main,
            "geoip_resolver",
            _ResolverStub({"country": "Finland", "country_code": "FI"}),
        ):
            enriched = main._enrich_event(payload, request, "req-1", main.settings)

        self.assertEqual(enriched["geo"]["country"], "Finland")
        self.assertEqual(enriched["geo"]["country_code"], "FI")
        self.assertEqual(enriched["geo"]["city"], "Oulu")


if __name__ == "__main__":
    unittest.main()
