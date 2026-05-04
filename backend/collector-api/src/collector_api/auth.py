from __future__ import annotations

from fastapi import HTTPException, status


def assert_api_key_allowed(api_key_map: dict[str, str], api_key: str | None, app_id: str) -> None:
    if not api_key:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing X-API-Key")

    allowed_app = api_key_map.get(api_key)
    if not allowed_app:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")

    if allowed_app != app_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="API key is not allowed for this app_id",
        )
