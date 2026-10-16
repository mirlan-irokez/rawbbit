#!/usr/bin/env python3
"""Validate a self-contained Claude plugin without private source access."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit


PLUGIN = Path(__file__).resolve().parents[1]
SKILL_ROOT = PLUGIN / "skills/rawbbit-game-events"
LINK = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
REQUIRED = (
    "LICENSE",
    "rawbbit-icon.svg",
    "scripts/verify_bundle.py",
    "skills/rawbbit-game-events/assets/sdk/BUNDLE-MANIFEST.json",
    "skills/rawbbit-game-events/assets/sdk/rawbbit-unity/LICENSE",
    "skills/rawbbit-game-events/assets/sdk/rawbbit-unity/README.md",
    "skills/rawbbit-game-events/assets/sdk/rawbbit-unity/INTEGRATION.md",
    "skills/rawbbit-game-events/assets/sdk/rawbbit-unity/Examples/GameAnalyticsTemplate/GameAnalyticsAdapter.cs.template",
    "skills/rawbbit-game-events/assets/sdk/rawbbit-browser-game/LICENSE",
    "skills/rawbbit-game-events/assets/sdk/rawbbit-browser-game/README.md",
    "skills/rawbbit-game-events/assets/sdk/rawbbit-browser-game/INTEGRATION.md",
    "skills/rawbbit-game-events/assets/sdk/rawbbit-browser-game/examples/GameAnalyticsAdapter.js.template",
    "skills/rawbbit-game-events/references/collector-contract.json",
)


def validate() -> None:
    manifest = json.loads((PLUGIN / ".claude-plugin/plugin.json").read_text())
    required = {"name", "description", "version", "author", "license"}
    listing = {"displayName", "homepage", "repository", "keywords"}
    if not required <= set(manifest) <= required | listing:
        raise ValueError("Unexpected Claude manifest fields")
    for key in ("displayName", "homepage", "repository"):
        if key in manifest and not (isinstance(manifest[key], str) and manifest[key].strip()):
            raise ValueError(f"Invalid Claude manifest field: {key}")
    for key in ("homepage", "repository"):
        if key in manifest and not manifest[key].startswith("https://"):
            raise ValueError(f"Claude manifest {key} must be an https URL")
    keywords = manifest.get("keywords", [])
    if not isinstance(keywords, list) or not all(isinstance(item, str) and item.strip() for item in keywords):
        raise ValueError("Invalid Claude manifest keywords")
    if manifest["name"] != "rawbbit-game-events":
        raise ValueError("Unexpected plugin name")
    if not all(isinstance(manifest[key], str) and manifest[key].strip() for key in
               ("description", "version", "license")):
        raise ValueError("Missing Claude plugin metadata")
    if not re.fullmatch(r"\d+\.\d+\.\d+", manifest["version"]):
        raise ValueError("Invalid plugin version")
    author = manifest["author"]
    if not isinstance(author, dict) or not isinstance(author.get("name"), str) or not author["name"].strip():
        raise ValueError("Missing plugin author")
    if manifest["license"] != "Apache-2.0":
        raise ValueError("Unexpected license")
    for forbidden in (".mcp.json", "mcp.json"):
        if (PLUGIN / forbidden).exists():
            raise ValueError("Skills-only package contains MCP configuration")
    if len((PLUGIN / "README.md").read_text().split()) < 40:
        raise ValueError("Plugin README is too short")

    skill = SKILL_ROOT / "SKILL.md"
    raw = skill.read_text()
    frontmatter = re.match(r"\A---\n(.*?)\n---\n(.*)\Z", raw, re.DOTALL)
    if frontmatter is None or not frontmatter.group(2).strip():
        raise ValueError("Missing skill frontmatter or body")
    fields = dict(re.findall(r"^(name|description):\s*(.+)$", frontmatter.group(1), re.MULTILINE))
    if fields.get("name") != manifest["name"] or not fields.get("description", "").strip():
        raise ValueError("Invalid skill metadata")
    if "## Claude execution surface" not in raw:
        raise ValueError("Missing Claude local-execution boundary")
    for relative in REQUIRED:
        path = PLUGIN / relative
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"Missing or unsafe package file: {relative}")
    contract = json.loads((SKILL_ROOT / "references/collector-contract.json").read_text())
    if not contract.get("models", {}).get("EventEnvelope") or not contract.get("defaults"):
        raise ValueError("Missing Collector contract facts")

    documents = [PLUGIN / "README.md", skill, *(SKILL_ROOT / "references").glob("*.md")]
    for document in documents:
        for target in LINK.findall(document.read_text()):
            url = urlsplit(target)
            if url.scheme or url.netloc or not url.path:
                continue
            path = (document.parent / unquote(url.path)).resolve()
            if not path.is_relative_to(PLUGIN) or not path.is_file():
                raise ValueError(f"Broken local link in {document}: {target}")
    print(f"Validated Claude manifest, skill, required assets, and links in {len(documents)} documents")


if __name__ == "__main__":
    try:
        validate()
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        print(f"Claude plugin validation error: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
