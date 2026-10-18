from __future__ import annotations

from functools import lru_cache

from pydantic import Field, ValidationError, field_validator, model_validator
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
    # Used only when raw-writer creates the durable consumer; an existing consumer is never changed.
    nats_max_ack_pending: int = Field(default=10000, ge=1, alias="NATS_MAX_ACK_PENDING")

    raw_storage_backend: str = Field(default="gcs", alias="RAW_STORAGE_BACKEND")

    gcs_raw_bucket: str = Field(default="", alias="GCS_RAW_BUCKET")
    gcs_raw_prefix: str = Field(default="raw", alias="GCS_RAW_PREFIX")

    s3_endpoint_url: str = Field(default="", alias="S3_ENDPOINT_URL")
    s3_bucket: str = Field(default="", alias="S3_BUCKET")
    s3_prefix: str = Field(default="raw", alias="S3_PREFIX")
    s3_access_key: str = Field(default="", alias="S3_ACCESS_KEY")
    s3_secret_key: str = Field(default="", alias="S3_SECRET_KEY")
    s3_region: str = Field(default="us-east-1", alias="S3_REGION")
    s3_force_path_style: bool = Field(default=True, alias="S3_FORCE_PATH_STYLE")
    s3_use_ssl: bool = Field(default=True, alias="S3_USE_SSL")
    s3_verify_ssl: bool = Field(default=True, alias="S3_VERIFY_SSL")

    raw_flush_interval_seconds: int = Field(default=60, alias="RAW_FLUSH_INTERVAL_SECONDS")
    raw_max_events_per_file: int = Field(default=1000, alias="RAW_MAX_EVENTS_PER_FILE")
    raw_max_bytes_per_file: int = Field(default=4_194_304, alias="RAW_MAX_BYTES_PER_FILE")
    # Optional cap on buffered events across all partitions; can only lower the consumer-derived threshold.
    raw_max_buffered_events: int | None = Field(default=None, ge=1, alias="RAW_MAX_BUFFERED_EVENTS")

    @field_validator("raw_max_buffered_events", mode="before")
    @classmethod
    def empty_buffered_events_is_unset(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @model_validator(mode="after")
    def validate_flush_timing(self) -> "Settings":
        if self.raw_flush_interval_seconds >= self.nats_ack_wait_seconds:
            raise ValueError(
                "RAW_FLUSH_INTERVAL_SECONDS must be lower than NATS_ACK_WAIT_SECONDS "
                f"(got {self.raw_flush_interval_seconds} >= {self.nats_ack_wait_seconds}); "
                "otherwise buffered messages are redelivered before they are flushed"
            )
        return self

    @model_validator(mode="after")
    def validate_storage_backend(self) -> "Settings":
        backend = self.raw_storage_backend.strip().lower()
        if backend not in {"gcs", "s3"}:
            raise ValueError("RAW_STORAGE_BACKEND must be one of: gcs, s3")

        self.raw_storage_backend = backend
        self.gcs_raw_bucket = self.gcs_raw_bucket.strip()
        self.gcs_raw_prefix = self.gcs_raw_prefix.strip() or "raw"
        self.s3_endpoint_url = self.s3_endpoint_url.strip()
        self.s3_bucket = self.s3_bucket.strip()
        self.s3_prefix = self.s3_prefix.strip() or "raw"
        self.s3_access_key = self.s3_access_key.strip()
        self.s3_secret_key = self.s3_secret_key.strip()

        if backend == "gcs":
            if not self.gcs_raw_bucket:
                raise ValueError("GCS_RAW_BUCKET is required when RAW_STORAGE_BACKEND=gcs")
            return self

        required = {
            "S3_ENDPOINT_URL": self.s3_endpoint_url,
            "S3_BUCKET": self.s3_bucket,
            "S3_ACCESS_KEY": self.s3_access_key,
            "S3_SECRET_KEY": self.s3_secret_key,
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise ValueError(
                "Missing required S3 settings when RAW_STORAGE_BACKEND=s3: " + ", ".join(missing)
            )
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    try:
        return Settings()
    except ValidationError as exc:
        # Report messages only: the default error text echoes the input, including S3 secrets.
        problems = "; ".join(error["msg"] for error in exc.errors(include_url=False, include_input=False))
        raise RuntimeError(f"Invalid raw-writer settings: {problems}") from None
