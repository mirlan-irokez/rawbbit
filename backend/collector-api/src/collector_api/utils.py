from __future__ import annotations

import hashlib


def hash_ip(ip_value: str, salt: str) -> str:
    digest = hashlib.sha256(f"{salt}:{ip_value}".encode("utf-8"))
    return digest.hexdigest()


def subject_for_app(prefix: str, app_id: str) -> str:
    normalized = app_id.strip().replace(" ", "_")
    return f"{prefix}.{normalized}"
