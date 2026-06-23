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
