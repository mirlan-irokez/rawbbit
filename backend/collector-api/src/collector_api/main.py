from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from urllib.parse import urlsplit
from uuid import uuid4

from fastapi import FastAPI, Header, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import ValidationError

from collector_api.auth import assert_api_key_allowed
from collector_api.geoip import GeoIPCountryResolver, extract_client_ip
from collector_api.nats_publisher import BatchPublishError, NATSPublisher
from collector_api.schemas import EventBatchRequest, EventBatchResponse
from collector_api.settings import Settings, get_settings
from collector_api.utils import hash_ip, subject_for_app

logging.basicConfig(
    level=get_settings().log_level.upper(),
    format="%(asctime)s %(levelname)s service=collector-api event=%(message)s",
)
logger = logging.getLogger("collector_api")


def _redacted_url(url: str) -> str:
    parts = urlsplit(url)
    host = parts.hostname or ""
    try:
        port_value = parts.port
    except ValueError:
        port_value = None
    port = f":{port_value}" if port_value else ""
    return f"{parts.scheme}://{host}{port}"


app = FastAPI(title="collector-api", version="0.1.0")
settings = get_settings()

_MAX_VALIDATION_ERRORS = 20
_VALIDATION_TRUNCATION_ERROR = {
    "type": "errors_truncated",
    "loc": [],
    "msg": "Additional validation errors omitted",
}
_SAFE_VALIDATION_FIELDS = frozenset(
    {
        "events",
        "event_id",
        "app_id",
        "environment",
        "event_name",
        "event_timestamp",
        "received_at",
        "user",
        "device",
        "traffic_source",
        "geo",
        "event_params",
        "user_properties",
        "consent",
        "ingest",
        "user_id",
        "user_pseudo_id",
        "session_id",
        "platform",
        "app_version",
        "os_version",
        "device_model",
        "locale",
        "timezone",
        "source",
        "medium",
        "campaign",
        "country",
        "country_code",
        "city",
        "region",
        "analytics_storage",
        "ads_storage",
        "personalization",
        "user_agent",
        "ip_hash",
        "ip",
        "request_id",
    }
)
_SAFE_VALIDATION_MESSAGES = {
    "json_invalid": "Invalid JSON",
    "missing": "Field required",
    "string_type": "Input should be a valid string",
    "datetime_type": "Input should be a valid datetime",
    "datetime_parsing": "Input should be a valid datetime",
    "model_type": "Input should be a valid object",
    "list_type": "Input should be a valid list",
    "dict_type": "Input should be a valid dictionary",
    "bool_type": "Input should be a valid boolean",
    "int_type": "Input should be a valid integer",
    "float_type": "Input should be a valid number",
}


def _sanitize_validation_errors(exc: ValidationError) -> list[dict[str, object]]:
    errors = exc.errors(include_input=False, include_context=False, include_url=False)
    sanitized: list[dict[str, object]] = []
    for error in errors[:_MAX_VALIDATION_ERRORS]:
        error_type = error.get("type")
        safe_type = (
            error_type
            if isinstance(error_type, str)
            and error_type
            and all(
                char.isascii() and (char.islower() or char.isdigit() or char == "_")
                for char in error_type
            )
            else "validation_error"
        )
        raw_location = error.get("loc", ())
        location = [
            part
            if type(part) is int and part >= 0
            else part
            if isinstance(part, str) and part in _SAFE_VALIDATION_FIELDS
            else "<field>"
            for part in raw_location
        ]
        sanitized.append(
            {
                "type": safe_type,
                "loc": location,
                "msg": _SAFE_VALIDATION_MESSAGES.get(safe_type, "Input is invalid"),
            }
        )

    if len(errors) > _MAX_VALIDATION_ERRORS:
        return [
            *sanitized[: _MAX_VALIDATION_ERRORS - 1],
            _VALIDATION_TRUNCATION_ERROR.copy(),
        ]
    return sanitized


if settings.cors_origins_list:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins_list,
        allow_credentials=settings.cors_allow_credentials,
        allow_methods=settings.cors_methods_list,
        allow_headers=settings.cors_headers_list,
        expose_headers=["Retry-After"],
        max_age=settings.cors_max_age_seconds,
    )

publisher = NATSPublisher(
    nats_url=settings.nats_url,
    stream_name=settings.nats_stream,
    subject_prefix=settings.nats_subject_prefix,
    stream_max_age_seconds=settings.nats_stream_max_age_seconds,
    duplicate_window_seconds=settings.nats_duplicate_window_seconds,
    max_inflight_publishes=settings.nats_max_inflight_publishes,
)
geoip_resolver = GeoIPCountryResolver(
    enabled=settings.geoip_enabled, mmdb_path=settings.geoip_mmdb_path
)


def _enrich_event(
    event_payload: dict, request: Request, request_id: str, cfg: Settings
) -> dict:
    now = datetime.now(UTC).isoformat()
    client_ip = extract_client_ip(request)
    ingest = dict(event_payload.get("ingest") or {})

    ingest["received_at"] = now
    ingest["request_id"] = request_id
    ingest["user_agent"] = request.headers.get("user-agent")
    ingest["ip_hash"] = hash_ip(client_ip, cfg.ip_hash_salt) if client_ip else None

    if client_ip and cfg.store_raw_ip:
        ingest["ip"] = client_ip
    else:
        ingest.pop("ip", None)

    if client_ip:
        geo_enrichment = geoip_resolver.lookup_country(client_ip)
        if geo_enrichment:
            geo = dict(event_payload.get("geo") or {})
            geo.update(geo_enrichment)
            event_payload["geo"] = geo

    event_payload["ingest"] = ingest
    event_payload["received_at"] = now
    return event_payload


def _auth_error_reason(detail: str) -> str:
    if detail == "Missing X-API-Key":
        return "auth_missing_key"
    if detail == "Invalid API key":
        return "auth_invalid_key"
    if detail == "API key is not allowed for this app_id":
        return "auth_app_mismatch"
    return "auth_error"


@app.on_event("startup")
async def startup_event() -> None:
    logger.info(
        "startup env=%s port=%s nats_url=%s stream=%s subject_prefix=%s max_request_bytes=%s max_events_per_request=%s max_inflight_publishes=%s cors_enabled=%s geoip_enabled=%s geoip_mmdb_path=%s",
        settings.env,
        settings.port,
        _redacted_url(settings.nats_url),
        settings.nats_stream,
        settings.nats_subject_prefix,
        settings.max_request_bytes,
        settings.max_events_per_request,
        settings.nats_max_inflight_publishes,
        bool(settings.cors_origins_list),
        settings.geoip_enabled,
        settings.geoip_mmdb_path,
    )
    geoip_resolver.warmup()
    await publisher.connect()
    logger.info("nats_ready stream=%s", settings.nats_stream)


@app.on_event("shutdown")
async def shutdown_event() -> None:
    geoip_resolver.close()
    await publisher.close()
    logger.info("shutdown_complete")


@app.get("/healthz")
async def healthcheck() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/v1/events:batch", response_model=EventBatchResponse)
async def ingest_events(
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> EventBatchResponse:
    started = time.perf_counter()
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > settings.max_request_bytes:
            logger.warning(
                "request_rejected reason=request_too_large request_bytes=%s", len(body)
            )
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail="Request too large",
            )

    try:
        payload = EventBatchRequest.model_validate_json(body)
    except ValidationError as exc:
        logger.warning(
            "request_rejected reason=validation_error request_bytes=%s", len(body)
        )
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=_sanitize_validation_errors(exc),
        ) from exc

    if not payload.events:
        logger.warning(
            "request_rejected reason=empty_events request_bytes=%s", len(body)
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="events cannot be empty"
        )

    if len(payload.events) > settings.max_events_per_request:
        logger.warning(
            "request_rejected reason=events_limit_exceeded events=%s max=%s",
            len(payload.events),
            settings.max_events_per_request,
        )
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"events limit exceeded: {settings.max_events_per_request}",
        )

    request_id = str(uuid4())

    try:
        for event in payload.events:
            assert_api_key_allowed(settings.api_key_map, x_api_key, event.app_id)
    except HTTPException as exc:
        reason = _auth_error_reason(str(exc.detail))
        logger.warning(
            "request_rejected reason=%s events=%s", reason, len(payload.events)
        )
        raise

    messages = [
        (
            subject_for_app(settings.nats_subject_prefix, event.app_id),
            _enrich_event(event.model_dump(mode="json"), request, request_id, settings),
            f"{event.app_id}:{event.event_id}",
        )
        for event in payload.events
    ]

    try:
        await publisher.publish_batch(messages)
    except BatchPublishError as exc:
        logger.error(
            "publish_failed request_id=%s events=%s published_events=%s failed_events=%s error_class=%s",
            request_id,
            len(messages),
            exc.published,
            exc.failed,
            exc.first_error.__class__.__name__,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Failed to publish events",
        ) from exc

    publish_ms = int((time.perf_counter() - started) * 1000)
    distinct_apps = len({event.app_id for event in payload.events})
    logger.info(
        "batch_accepted request_id=%s accepted_events=%s distinct_app_ids=%s request_bytes=%s publish_ms=%s",
        request_id,
        len(payload.events),
        distinct_apps,
        len(body),
        publish_ms,
    )

    return EventBatchResponse(
        request_id=request_id, accepted_events=len(payload.events)
    )
