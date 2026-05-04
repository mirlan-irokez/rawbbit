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
from collector_api.nats_publisher import NATSPublisher
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

if settings.cors_origins_list:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins_list,
        allow_credentials=settings.cors_allow_credentials,
        allow_methods=settings.cors_methods_list,
        allow_headers=settings.cors_headers_list,
        max_age=settings.cors_max_age_seconds,
    )

publisher = NATSPublisher(
    nats_url=settings.nats_url,
    stream_name=settings.nats_stream,
    subject_prefix=settings.nats_subject_prefix,
    stream_max_age_seconds=settings.nats_stream_max_age_seconds,
    duplicate_window_seconds=settings.nats_duplicate_window_seconds,
)
geoip_resolver = GeoIPCountryResolver(enabled=settings.geoip_enabled, mmdb_path=settings.geoip_mmdb_path)


def _enrich_event(event_payload: dict, request: Request, request_id: str, cfg: Settings) -> dict:
    now = datetime.now(UTC).isoformat()
    client_ip = extract_client_ip(request)
    ingest = dict(event_payload.get("ingest") or {})

    ingest.setdefault("received_at", now)
    ingest.setdefault("request_id", request_id)
    ingest.setdefault("user_agent", request.headers.get("user-agent"))

    if client_ip:
        ingest.setdefault("ip_hash", hash_ip(client_ip, cfg.ip_hash_salt))
        if cfg.store_raw_ip:
            ingest.setdefault("ip", client_ip)

        geo_enrichment = geoip_resolver.lookup_country(client_ip)
        if geo_enrichment:
            geo = dict(event_payload.get("geo") or {})
            geo.update(geo_enrichment)
            event_payload["geo"] = geo

    event_payload["ingest"] = ingest
    event_payload.setdefault("received_at", now)
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
        "startup env=%s port=%s nats_url=%s stream=%s subject_prefix=%s max_request_bytes=%s max_events_per_request=%s cors_enabled=%s geoip_enabled=%s geoip_mmdb_path=%s",
        settings.env,
        settings.port,
        _redacted_url(settings.nats_url),
        settings.nats_stream,
        settings.nats_subject_prefix,
        settings.max_request_bytes,
        settings.max_events_per_request,
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
            logger.warning("request_rejected reason=request_too_large request_bytes=%s", len(body))
            raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="Request too large")

    try:
        payload = EventBatchRequest.model_validate_json(body)
    except ValidationError as exc:
        logger.warning("request_rejected reason=validation_error request_bytes=%s", len(body))
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=exc.errors()) from exc

    if not payload.events:
        logger.warning("request_rejected reason=empty_events request_bytes=%s", len(body))
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="events cannot be empty")

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
        logger.warning("request_rejected reason=%s events=%s", reason, len(payload.events))
        raise

    published_events = 0
    for event in payload.events:
        subject = subject_for_app(settings.nats_subject_prefix, event.app_id)
        msg_id = f"{event.app_id}:{event.event_id}"
        enriched_payload = _enrich_event(event.model_dump(mode="json"), request, request_id, settings)

        try:
            await publisher.publish(subject=subject, payload=enriched_payload, msg_id=msg_id)
            published_events += 1
        except Exception as exc:
            logger.error(
                "publish_failed request_id=%s app_id=%s published_events=%s error_class=%s",
                request_id,
                event.app_id,
                published_events,
                exc.__class__.__name__,
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

    return EventBatchResponse(request_id=request_id, accepted_events=len(payload.events))
