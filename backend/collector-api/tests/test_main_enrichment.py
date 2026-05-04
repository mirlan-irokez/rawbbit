from __future__ import annotations

import unittest
from unittest.mock import patch

from collector_api.main import _enrich_event, settings


class _DummyClient:
    def __init__(self, host: str) -> None:
        self.host = host


class _DummyRequest:
    def __init__(self, headers: dict[str, str] | None = None, client_host: str | None = None) -> None:
        self.headers = headers or {}
        self.client = _DummyClient(client_host) if client_host else None


class _ResolverStub:
    def __init__(self, response: dict[str, str] | None) -> None:
        self._response = response

    def lookup_country(self, _ip_value: str) -> dict[str, str] | None:
        return self._response


class MainEnrichmentTests(unittest.TestCase):
    def test_country_enrichment_overrides_only_country_fields(self) -> None:
        event_payload = {
            "event_id": "evt-1",
            "geo": {"country": "Wrong", "country_code": "WR", "city": "Oulu"},
        }
        request = _DummyRequest(headers={"x-forwarded-for": "203.0.113.8"}, client_host="10.0.0.1")

        with patch("collector_api.main.geoip_resolver", _ResolverStub({"country": "Finland", "country_code": "FI"})):
            enriched = _enrich_event(event_payload, request, request_id="req-1", cfg=settings)

        self.assertEqual(enriched["geo"]["country"], "Finland")
        self.assertEqual(enriched["geo"]["country_code"], "FI")
        self.assertEqual(enriched["geo"]["city"], "Oulu")
        self.assertIn("ingest", enriched)
        self.assertEqual(enriched["ingest"]["request_id"], "req-1")

    def test_lookup_failure_keeps_ingestion_successful(self) -> None:
        event_payload = {"event_id": "evt-1"}
        request = _DummyRequest(headers={"x-forwarded-for": "203.0.113.8"}, client_host="10.0.0.1")

        with patch("collector_api.main.geoip_resolver", _ResolverStub(None)):
            enriched = _enrich_event(event_payload, request, request_id="req-1", cfg=settings)

        self.assertNotIn("geo", enriched)
        self.assertIn("ingest", enriched)

    def test_no_ip_skips_geoip_lookup_path(self) -> None:
        event_payload = {"event_id": "evt-1"}
        request = _DummyRequest(headers={}, client_host=None)

        with patch("collector_api.main.geoip_resolver", _ResolverStub({"country": "Finland", "country_code": "FI"})):
            enriched = _enrich_event(event_payload, request, request_id="req-1", cfg=settings)

        self.assertNotIn("geo", enriched)
        self.assertIn("ingest", enriched)


if __name__ == "__main__":
    unittest.main()
