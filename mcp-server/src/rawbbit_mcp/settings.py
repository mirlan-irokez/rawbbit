from __future__ import annotations

import json
from functools import lru_cache

from pydantic import Field, ValidationError, field_validator
from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    env: str = Field(default="local", alias="ENV")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    mcp_name: str = Field(default="Rawbbit MCP server", alias="MCP_NAME")
    mcp_host: str = Field(default="0.0.0.0", alias="MCP_HOST")
    mcp_port: int = Field(default=8000, alias="MCP_PORT")
    mcp_path: str = Field(default="/mcp", alias="MCP_PATH")

    clickhouse_host: str = Field(default="host.docker.internal", alias="CLICKHOUSE_HOST")
    clickhouse_port: int = Field(default=8123, alias="CLICKHOUSE_PORT")
    clickhouse_user: str = Field(default="default", alias="CLICKHOUSE_USER")
    clickhouse_password: str = Field(default="", alias="CLICKHOUSE_PASSWORD")
    clickhouse_database: str = Field(default="analytics", alias="CLICKHOUSE_DATABASE")
    clickhouse_secure: bool = Field(default=False, alias="CLICKHOUSE_SECURE")
    clickhouse_verify: bool = Field(default=True, alias="CLICKHOUSE_VERIFY")
    clickhouse_table: str = Field(default="events", alias="CLICKHOUSE_TABLE")

    max_query_rows: int = Field(default=500, alias="MCP_MAX_QUERY_ROWS")
    max_sample_rows: int = Field(default=50, alias="MCP_MAX_SAMPLE_ROWS")
    max_execution_seconds: int = Field(default=30, alias="MCP_MAX_EXECUTION_SECONDS")

    bot_user_agent_regex: str = Field(
        default="bot|spider|crawler|headless|bytespider|ahrefs|googlebot",
        alias="MCP_BOT_USER_AGENT_REGEX",
    )

    mcp_api_keys_json: str = Field(default="", alias="MCP_API_KEYS_JSON")

    jwt_public_key: str = Field(default="", alias="MCP_JWT_PUBLIC_KEY")
    jwt_jwks_uri: str = Field(default="", alias="MCP_JWT_JWKS_URI")
    jwt_issuer: str = Field(default="", alias="MCP_JWT_ISSUER")
    jwt_audience: str = Field(default="", alias="MCP_JWT_AUDIENCE")
    allow_unauthenticated: bool = Field(default=False, alias="MCP_ALLOW_UNAUTHENTICATED")

    @field_validator("mcp_path")
    @classmethod
    def validate_mcp_path(cls, value: str) -> str:
        value = value.strip() or "/mcp"
        if not value.startswith("/"):
            value = f"/{value}"
        return value

    @field_validator("clickhouse_database", "clickhouse_table")
    @classmethod
    def validate_identifier_part(cls, value: str) -> str:
        value = value.strip()
        if not value.replace("_", "").isalnum():
            raise ValueError("ClickHouse database and table names may only contain letters, numbers, and underscores")
        return value

    @field_validator("mcp_api_keys_json")
    @classmethod
    def validate_mcp_api_keys_json(cls, value: str) -> str:
        value = value.strip()
        if not value:
            return ""

        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError("MCP_API_KEYS_JSON must be valid JSON") from exc

        if not isinstance(parsed, dict) or not parsed:
            raise ValueError("MCP_API_KEYS_JSON must be a non-empty JSON object mapping user labels to bearer tokens")

        seen_tokens: set[str] = set()
        for label, token in parsed.items():
            if not isinstance(label, str) or not label.strip():
                raise ValueError("MCP_API_KEYS_JSON keys must be non-empty strings")
            if not isinstance(token, str) or not token.strip():
                raise ValueError("MCP_API_KEYS_JSON values must be non-empty strings")
            normalized = token.strip()
            if normalized in seen_tokens:
                raise ValueError("MCP_API_KEYS_JSON token values must be unique")
            seen_tokens.add(normalized)

        return value

    @model_validator(mode="after")
    def validate_auth_is_explicit(self) -> "Settings":
        if self.auth_mode == "none" and not self.allow_unauthenticated:
            raise ValueError(
                "MCP authentication is required. Set MCP_API_KEYS_JSON or JWT settings, "
                "or explicitly set MCP_ALLOW_UNAUTHENTICATED=1 for local-only development."
            )
        return self

    @property
    def table_ref(self) -> str:
        return f"{self.clickhouse_database}.{self.clickhouse_table}"

    @property
    def api_keys_by_user(self) -> dict[str, str]:
        if not self.mcp_api_keys_json.strip():
            return {}
        raw = json.loads(self.mcp_api_keys_json)
        return {str(label).strip(): str(token).strip() for label, token in raw.items()}

    @property
    def auth_mode(self) -> str:
        if self.api_keys_by_user:
            return "static_tokens"
        if self.jwt_public_key or self.jwt_jwks_uri:
            return "jwt"
        return "none"

    @property
    def query_settings(self) -> dict[str, int | str]:
        return {
            "readonly": 1,
            "max_execution_time": self.max_execution_seconds,
            "max_result_rows": self.max_query_rows,
            "result_overflow_mode": "break",
        }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    try:
        return Settings()
    except ValidationError as exc:
        raise RuntimeError(f"Invalid MCP server settings: {exc}") from exc
