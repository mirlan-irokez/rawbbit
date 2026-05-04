from __future__ import annotations

import tempfile
import unittest

from collector_api.geoip import GeoIPCountryResolver, extract_client_ip


class _DummyClient:
    def __init__(self, host: str) -> None:
        self.host = host


class _DummyRequest:
    def __init__(self, headers: dict[str, str] | None = None, client_host: str | None = None) -> None:
        self.headers = headers or {}
        self.client = _DummyClient(client_host) if client_host else None


class _FakeReader:
    def __init__(self, response: dict | None = None) -> None:
        self._response = response

    def get(self, _ip_value: str) -> dict | None:
        return self._response


class GeoIPTests(unittest.TestCase):
    def test_extract_client_ip_prefers_forwarded_for(self) -> None:
        request = _DummyRequest(
            headers={"x-forwarded-for": "203.0.113.8, 10.0.0.1", "x-real-ip": "198.51.100.4"},
            client_host="172.16.0.2",
        )
        self.assertEqual(extract_client_ip(request), "203.0.113.8")

    def test_extract_client_ip_falls_back_to_real_ip_then_socket(self) -> None:
        request = _DummyRequest(headers={"x-forwarded-for": "unknown", "x-real-ip": "198.51.100.4"}, client_host="172.16.0.2")
        self.assertEqual(extract_client_ip(request), "198.51.100.4")

        fallback_request = _DummyRequest(headers={}, client_host="172.16.0.2")
        self.assertEqual(extract_client_ip(fallback_request), "172.16.0.2")

    def test_lookup_country_returns_normalized_payload(self) -> None:
        with tempfile.NamedTemporaryFile() as tmp_file:
            resolver = GeoIPCountryResolver(
                enabled=True,
                mmdb_path=tmp_file.name,
                reader_factory=lambda _path: _FakeReader({"country": {"name": "Finland", "iso_code": "fi"}}),
            )
            self.assertEqual(resolver.lookup_country("203.0.113.8"), {"country": "Finland", "country_code": "FI"})

    def test_lookup_country_supports_flat_record_shape(self) -> None:
        with tempfile.NamedTemporaryFile() as tmp_file:
            resolver = GeoIPCountryResolver(
                enabled=True,
                mmdb_path=tmp_file.name,
                reader_factory=lambda _path: _FakeReader({"country_name": "Finland", "country_code": "fi"}),
            )
            self.assertEqual(resolver.lookup_country("203.0.113.8"), {"country": "Finland", "country_code": "FI"})

    def test_lookup_country_fails_open_when_mmdb_missing(self) -> None:
        resolver = GeoIPCountryResolver(enabled=True, mmdb_path="/definitely/missing.mmdb")
        self.assertIsNone(resolver.lookup_country("203.0.113.8"))


if __name__ == "__main__":
    unittest.main()
