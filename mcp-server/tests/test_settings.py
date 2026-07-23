from __future__ import annotations

import pytest
from pydantic import ValidationError

from rawbbit_mcp.settings import Settings


def test_settings_parse_static_api_keys_and_auth_mode() -> None:
    settings = Settings(
        MCP_API_KEYS_JSON='{"user1":"token-1","user2":"token-2"}',
    )

    assert settings.api_keys_by_user == {"user1": "token-1", "user2": "token-2"}
    assert settings.auth_mode == "static_tokens"


def test_settings_use_jwt_mode_without_static_tokens() -> None:
    settings = Settings(MCP_JWT_PUBLIC_KEY="public-key")

    assert settings.api_keys_by_user == {}
    assert settings.auth_mode == "jwt"


def test_settings_fail_closed_without_auth() -> None:
    with pytest.raises(ValidationError, match="MCP authentication is required"):
        Settings()


def test_settings_allow_explicit_unauthenticated_mode() -> None:
    settings = Settings(MCP_ALLOW_UNAUTHENTICATED=True)

    assert settings.auth_mode == "none"
    assert settings.allow_unauthenticated is True


@pytest.mark.parametrize(
    "raw_value",
    [
        "[]",
        '{"user1":"same","user2":"same"}',
        '{"":"token"}',
        '{"user1":""}',
    ],
)
def test_settings_reject_invalid_static_api_keys_json(raw_value: str) -> None:
    with pytest.raises(ValidationError):
        Settings(MCP_API_KEYS_JSON=raw_value)
