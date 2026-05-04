from __future__ import annotations

import ipaddress
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import Request

logger = logging.getLogger("collector_api")


def _open_mmdb_reader(path: str) -> Any:
    import maxminddb

    return maxminddb.open_database(path)


def _parse_ip_candidate(raw_value: str) -> str | None:
    value = (raw_value or "").strip().strip('"')
    if not value or value.lower() == "unknown":
        return None

    if value.startswith("[") and "]" in value:
        value = value[1 : value.index("]")]

    try:
        ipaddress.ip_address(value)
        return value
    except ValueError:
        pass

    # Parsing for common IPv4-with-port forwarded values.
    if value.count(":") == 1 and "." in value:
        host, _port = value.rsplit(":", 1)
        try:
            ipaddress.ip_address(host)
            return host
        except ValueError:
            return None

    return None


def extract_client_ip(request: Request) -> str:
    """Client IP extraction.

    Rule (phase 1):
    1) first valid IP in X-Forwarded-For,
    2) X-Real-IP,
    3) request.client.host.

    This is intentionally best-effort and does not assert trusted proxy semantics.
    """

    forwarded_for = request.headers.get("x-forwarded-for", "")
    for item in forwarded_for.split(","):
        parsed = _parse_ip_candidate(item)
        if parsed:
            return parsed

    real_ip = _parse_ip_candidate(request.headers.get("x-real-ip", ""))
    if real_ip:
        return real_ip

    direct_ip = request.client.host if request.client else ""
    return _parse_ip_candidate(direct_ip) or ""


class GeoIPCountryResolver:
    def __init__(
        self,
        enabled: bool,
        mmdb_path: str,
        reader_factory: Callable[[str], Any] | None = None,
    ) -> None:
        self.enabled = enabled
        self.mmdb_path = mmdb_path
        self._reader_factory = reader_factory or _open_mmdb_reader
        self._reader: Any | None = None
        self._reader_checked = False

    def _get_reader(self) -> Any | None:
        if not self.enabled:
            return None
        if self._reader_checked:
            return self._reader

        self._reader_checked = True
        if not self.mmdb_path:
            logger.warning("geoip_disabled reason=missing_path")
            return None

        mmdb_file = Path(self.mmdb_path)
        if not mmdb_file.exists() or not mmdb_file.is_file():
            logger.warning("geoip_unavailable reason=missing_mmdb path=%s", self.mmdb_path)
            return None

        try:
            self._reader = self._reader_factory(self.mmdb_path)
            logger.info("geoip_ready path=%s", self.mmdb_path)
        except Exception as exc:
            logger.warning(
                "geoip_unavailable reason=reader_open_failed path=%s error_class=%s",
                self.mmdb_path,
                exc.__class__.__name__,
            )
            self._reader = None
        return self._reader

    def warmup(self) -> None:
        if self.enabled:
            self._get_reader()
        else:
            logger.info("geoip_disabled reason=feature_flag_off")

    def lookup_country(self, ip_value: str) -> dict[str, str] | None:
        if not ip_value:
            return None

        reader = self._get_reader()
        if reader is None:
            return None

        try:
            record = reader.get(ip_value)
        except Exception as exc:
            logger.warning(
                "geoip_lookup_failed error_class=%s",
                exc.__class__.__name__,
            )
            return None

        if not isinstance(record, dict):
            return None

        country_data = record.get("country")
        country_name = ""
        country_code = ""
        if isinstance(country_data, dict):
            country_name = str(country_data.get("name") or "").strip()
            country_code = str(country_data.get("iso_code") or "").strip().upper()

        if not country_name:
            country_name = str(record.get("country_name") or "").strip()
        if not country_code:
            country_code = str(record.get("country_code") or "").strip().upper()

        if not country_name and not country_code:
            return None

        payload: dict[str, str] = {}
        if country_name:
            payload["country"] = country_name
        if country_code:
            payload["country_code"] = country_code
        return payload

    def close(self) -> None:
        if self._reader is None:
            return
        close_fn = getattr(self._reader, "close", None)
        if callable(close_fn):
            close_fn()
        self._reader = None
