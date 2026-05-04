from __future__ import annotations

import json
from functools import lru_cache

from pydantic import Field, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    env: str = Field(default="local", alias="ENV")
    port: int = Field(default=8080, alias="PORT")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    collector_api_keys_json: str = Field(default="{}", alias="COLLECTOR_API_KEYS_JSON")
    nats_url: str = Field(default="nats://localhost:4222", alias="NATS_URL")
    nats_stream: str = Field(default="EVENTS", alias="NATS_STREAM")
    nats_subject_prefix: str = Field(default="events", alias="NATS_SUBJECT_PREFIX")
    nats_stream_max_age_seconds: int = Field(default=604800, alias="NATS_STREAM_MAX_AGE_SECONDS")
    nats_duplicate_window_seconds: int = Field(default=600, alias="NATS_DUPLICATE_WINDOW_SECONDS")

    max_events_per_request: int = Field(default=500, alias="MAX_EVENTS_PER_REQUEST")
    max_request_bytes: int = Field(default=1_048_576, alias="MAX_REQUEST_BYTES")
    ip_hash_salt: str = Field(default="change-me", alias="IP_HASH_SALT")
    store_raw_ip: bool = Field(default=False, alias="STORE_RAW_IP")
    geoip_enabled: bool = Field(default=False, alias="GEOIP_ENABLED")
    geoip_mmdb_path: str = Field(default="", alias="GEOIP_MMDB_PATH")
    cors_allow_origins: str = Field(default="", alias="CORS_ALLOW_ORIGINS")
    cors_allow_methods: str = Field(default="POST,OPTIONS", alias="CORS_ALLOW_METHODS")
    cors_allow_headers: str = Field(default="Content-Type,X-API-Key", alias="CORS_ALLOW_HEADERS")
    cors_allow_credentials: bool = Field(default=False, alias="CORS_ALLOW_CREDENTIALS")
    cors_max_age_seconds: int = Field(default=600, alias="CORS_MAX_AGE_SECONDS")

    @field_validator("collector_api_keys_json")
    @classmethod
    def validate_keys_json(cls, value: str) -> str:
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError("COLLECTOR_API_KEYS_JSON must be valid JSON") from exc

        if not isinstance(parsed, dict):
            raise ValueError("COLLECTOR_API_KEYS_JSON must be a JSON object")
        return value

    @property
    def api_key_map(self) -> dict[str, str]:
        data = json.loads(self.collector_api_keys_json)
        return {str(k): str(v) for k, v in data.items()}

    @staticmethod
    def _csv_to_list(value: str) -> list[str]:
        return [item.strip() for item in value.split(",") if item.strip()]

    @property
    def cors_origins_list(self) -> list[str]:
        return self._csv_to_list(self.cors_allow_origins)

    @property
    def cors_methods_list(self) -> list[str]:
        return [item.upper() for item in self._csv_to_list(self.cors_allow_methods)]

    @property
    def cors_headers_list(self) -> list[str]:
        return self._csv_to_list(self.cors_allow_headers)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    try:
        return Settings()
    except ValidationError as exc:
        raise RuntimeError(f"Invalid collector settings: {exc}") from exc
