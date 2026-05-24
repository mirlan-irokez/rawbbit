from __future__ import annotations

from datetime import UTC, datetime


def parse_iso_datetime(value: str | None) -> datetime | None:
    if not value:
        return None

    candidate = value.strip()
    if candidate.endswith("Z"):
        candidate = candidate[:-1] + "+00:00"

    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def event_partition(event_timestamp: str | None, fallback_timestamp: str | None) -> tuple[str, str]:
    parsed = parse_iso_datetime(event_timestamp) or parse_iso_datetime(fallback_timestamp) or datetime.now(UTC)
    return parsed.strftime("%Y-%m-%d"), parsed.strftime("%H")


def object_partition_path(prefix: str, app_id: str, event_date: str, hour: str, filename: str) -> str:
    cleaned_prefix = prefix.strip("/")
    suffix = f"app_id={app_id}/event_date={event_date}/hour={hour}/{filename}"
    if not cleaned_prefix:
        return suffix
    return f"{cleaned_prefix}/{suffix}"


def gcs_partition_path(prefix: str, app_id: str, event_date: str, hour: str, filename: str) -> str:
    return object_partition_path(prefix, app_id, event_date, hour, filename)
