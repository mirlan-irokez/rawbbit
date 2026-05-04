from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class UserContext(BaseModel):
    user_id: str | None = None
    user_pseudo_id: str | None = None
    session_id: str | None = None


class DeviceContext(BaseModel):
    platform: str | None = None
    app_version: str | None = None
    os_version: str | None = None
    device_model: str | None = None
    locale: str | None = None
    timezone: str | None = None


class TrafficSource(BaseModel):
    source: str | None = None
    medium: str | None = None
    campaign: str | None = None


class GeoContext(BaseModel):
    country: str | None = None
    country_code: str | None = None
    city: str | None = None
    region: str | None = None
    timezone: str | None = None


class ConsentContext(BaseModel):
    analytics_storage: bool | None = None
    ads_storage: bool | None = None
    personalization: bool | None = None


class IngestContext(BaseModel):
    user_agent: str | None = None
    ip_hash: str | None = None
    ip: str | None = None
    received_at: datetime | None = None
    request_id: str | None = None


class EventEnvelope(BaseModel):
    event_id: str
    app_id: str
    environment: str | None = None
    event_name: str
    event_timestamp: datetime
    received_at: datetime | None = None

    user: UserContext | None = None
    device: DeviceContext | None = None
    traffic_source: TrafficSource | None = None
    geo: GeoContext | None = None

    event_params: dict[str, Any] = Field(default_factory=dict)
    user_properties: dict[str, Any] = Field(default_factory=dict)
    consent: ConsentContext | None = None
    ingest: IngestContext | None = None


class EventBatchRequest(BaseModel):
    events: list[EventEnvelope] = Field(default_factory=list)


class EventBatchResponse(BaseModel):
    request_id: str
    accepted_events: int


class ErrorResponse(BaseModel):
    detail: str
