from __future__ import annotations

from functools import lru_cache

from pydantic import Field, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    env: str = Field(default="local", alias="ENV")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    nats_url: str = Field(default="nats://localhost:4222", alias="NATS_URL")
    nats_stream: str = Field(default="EVENTS", alias="NATS_STREAM")
    nats_subject_prefix: str = Field(default="events", alias="NATS_SUBJECT_PREFIX")
    nats_consumer: str = Field(default="raw-writer", alias="NATS_CONSUMER")
    nats_fetch_batch: int = Field(default=500, alias="NATS_FETCH_BATCH")
    nats_fetch_timeout_seconds: float = Field(default=1.0, alias="NATS_FETCH_TIMEOUT_SECONDS")
    nats_ack_wait_seconds: int = Field(default=120, alias="NATS_ACK_WAIT_SECONDS")
    nats_max_deliver: int = Field(default=10, alias="NATS_MAX_DELIVER")

    gcs_raw_bucket: str = Field(default="", alias="GCS_RAW_BUCKET")
    gcs_raw_prefix: str = Field(default="raw", alias="GCS_RAW_PREFIX")

    raw_flush_interval_seconds: int = Field(default=60, alias="RAW_FLUSH_INTERVAL_SECONDS")
    raw_max_events_per_file: int = Field(default=1000, alias="RAW_MAX_EVENTS_PER_FILE")
    raw_max_bytes_per_file: int = Field(default=4_194_304, alias="RAW_MAX_BYTES_PER_FILE")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    try:
        return Settings()
    except ValidationError as exc:
        raise RuntimeError(f"Invalid raw-writer settings: {exc}") from exc
