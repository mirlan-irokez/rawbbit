from __future__ import annotations

import json
from typing import Any

from nats.aio.msg import Msg

from raw_writer.partitioning import event_partition

INT64_MIN = -(2**63)
INT64_MAX = 2**63 - 1


def _json_string(value: Any) -> str:
    return json.dumps(value if value is not None else {}, separators=(",", ":"), ensure_ascii=True)


def _string_or_none(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _int_or_zero(value: Any) -> int:
    def _within_int64(number: int) -> int:
        return number if INT64_MIN <= number <= INT64_MAX else 0

    if value is None:
        return 0
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return _within_int64(value)
    if isinstance(value, float):
        return _within_int64(int(value)) if value.is_integer() else 0

    try:
        converted = int(str(value).strip())
    except (TypeError, ValueError):
        return 0

    return _within_int64(converted)


def event_to_row(event: dict[str, Any], msg: Msg) -> tuple[tuple[str, str, str], dict[str, Any]]:
    ingest = event.get("ingest") or {}
    user = event.get("user") or {}
    device = event.get("device") or {}

    event_date, hour = event_partition(
        event_timestamp=event.get("event_timestamp"),
        fallback_timestamp=event.get("received_at") or ingest.get("received_at"),
    )
    app_id = str(event.get("app_id") or "unknown_app")

    metadata = msg.metadata
    nats_stream = _string_or_none(metadata.stream) if metadata else None
    nats_sequence = _int_or_zero(metadata.sequence.stream) if metadata else 0

    row = {
        "event_id": _string_or_none(event.get("event_id")),
        "app_id": app_id,
        "environment": _string_or_none(event.get("environment")),
        "event_name": _string_or_none(event.get("event_name")),
        "event_timestamp": _string_or_none(event.get("event_timestamp")),
        "received_at": _string_or_none(event.get("received_at") or ingest.get("received_at")),
        "user_id": _string_or_none(user.get("user_id")),
        "user_pseudo_id": _string_or_none(user.get("user_pseudo_id")),
        "session_id": _string_or_none(user.get("session_id")),
        "platform": _string_or_none(device.get("platform")),
        "app_version": _string_or_none(device.get("app_version")),
        "os_version": _string_or_none(device.get("os_version")),
        "device_model": _string_or_none(device.get("device_model")),
        "locale": _string_or_none(device.get("locale")),
        "timezone": _string_or_none(device.get("timezone")),
        "event_params_json": _json_string(event.get("event_params")),
        "user_properties_json": _json_string(event.get("user_properties")),
        "traffic_source_json": _json_string(event.get("traffic_source")),
        "geo_json": _json_string(event.get("geo")),
        "consent_json": _json_string(event.get("consent")),
        "ingest_request_id": _string_or_none(ingest.get("request_id")),
        "ingest_user_agent": _string_or_none(ingest.get("user_agent")),
        "ingest_ip_hash": _string_or_none(ingest.get("ip_hash")),
        "nats_stream": nats_stream,
        "nats_sequence": nats_sequence,
    }

    return (app_id, event_date, hour), row
