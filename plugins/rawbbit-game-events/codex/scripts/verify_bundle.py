#!/usr/bin/env python3
"""Verify the self-contained SDK bundle without access to its source repository."""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path, PurePosixPath


PLUGIN = Path(__file__).resolve().parents[1]
SDK_NAMES = {"rawbbit-unity", "rawbbit-browser-game"}
MANIFEST_NAME = "BUNDLE-MANIFEST.json"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def safe_file_path(raw: str) -> PurePosixPath:
    if not isinstance(raw, str) or "\\" in raw or "\x00" in raw:
        raise ValueError(f"Unsafe bundle path: {raw!r}")
    path = PurePosixPath(raw)
    if (
        path.is_absolute() or len(path.parts) < 2 or path.parts[0] not in SDK_NAMES
        or any(part in (".", "..") for part in raw.split("/"))
        or path.as_posix() != raw
    ):
        raise ValueError(f"Unsafe bundle path: {raw!r}")
    return path


def verify(
    bundle: Path = PLUGIN / "skills/rawbbit-game-events/assets/sdk",
    contract: Path = PLUGIN / "skills/rawbbit-game-events/references/collector-contract.json",
    license_file: Path = PLUGIN / "LICENSE",
) -> None:
    manifest = json.loads((bundle / MANIFEST_NAME).read_text())
    if set(manifest) != {"format_version", "sdk_versions", "license", "collector_contract_sha256", "files"}:
        raise ValueError("Unexpected bundle manifest fields")
    if manifest["format_version"] != 1 or set(manifest["sdk_versions"]) != SDK_NAMES:
        raise ValueError("Invalid bundle manifest version or SDK names")
    declared = manifest["files"]
    if not isinstance(declared, dict) or not declared:
        raise ValueError("Empty or invalid bundle inventory")
    for raw, expected_hash in declared.items():
        safe_file_path(raw)
        if not isinstance(expected_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
            raise ValueError(f"Invalid bundle hash: {raw}")
    actual = set()
    for path in bundle.rglob("*"):
        if path.is_symlink():
            raise ValueError(f"Symlink in SDK bundle: {path}")
        if path.is_file() and path != bundle / MANIFEST_NAME:
            actual.add(path.relative_to(bundle).as_posix())
    if actual != set(declared):
        raise ValueError(f"SDK inventory mismatch: missing={sorted(set(declared) - actual)}, extra={sorted(actual - set(declared))}")
    for raw, expected_hash in declared.items():
        if digest(bundle / raw) != expected_hash:
            raise ValueError(f"SDK hash mismatch: {raw}")
    license_info = manifest["license"]
    if set(license_info) != {"id", "sha256"} or license_info["id"] != "Apache-2.0":
        raise ValueError("Invalid bundle license metadata")
    if digest(license_file) != license_info["sha256"]:
        raise ValueError("Plugin license hash mismatch")
    for name in SDK_NAMES:
        path = f"{name}/LICENSE"
        if path not in declared or digest(bundle / path) != license_info["sha256"]:
            raise ValueError(f"Missing or mismatched SDK license: {name}")
    if digest(contract) != manifest["collector_contract_sha256"]:
        raise ValueError("Collector contract facts hash mismatch")
    print(f"Verified {len(declared)} packaged SDK files")


if __name__ == "__main__":
    try:
        verify()
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        print(f"SDK bundle error: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
