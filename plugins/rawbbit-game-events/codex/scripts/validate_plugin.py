#!/usr/bin/env python3
"""Validate a clean extracted plugin. Requires jsonschema and PyYAML."""

from __future__ import annotations

import json
import copy
import re
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree

import yaml
from jsonschema import Draft202012Validator, SchemaError, ValidationError


PLUGIN = Path(__file__).resolve().parents[1]
SKILL = PLUGIN / "skills" / "rawbbit-game-events" / "SKILL.md"
SKILL_ROOT = SKILL.parent
LINK = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
REQUIRED = (
    "LICENSE",
    "assets/rawbbit-icon.svg",
    "skills/rawbbit-game-events/assets/sdk/BUNDLE-MANIFEST.json",
    "scripts/verify_bundle.py",
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


def validate_listing(manifest: dict) -> None:
    extension = manifest.get("extensions", {}).get("com.openai", {})
    if extension.get("requires_local_executor") is not True:
        raise ValueError("Local game checkout requires desktop execution")
    interface = extension.get("interface")
    if not isinstance(interface, dict):
        raise ValueError("Missing OpenAI listing interface")
    for field, limit in (
        ("displayName", 30), ("shortDescription", 30),
        ("longDescription", 4000), ("developerName", 80),
    ):
        value = interface.get(field)
        if not isinstance(value, str) or not value.strip() or len(value) > limit:
            raise ValueError(f"Invalid listing field: {field}")
        if field != "longDescription" and ("\n" in value or "\r" in value):
            raise ValueError(f"Listing field must be single-line: {field}")
    if not isinstance(interface.get("category"), str) or not interface["category"].strip():
        raise ValueError("Missing listing category")
    capabilities = interface.get("capabilities")
    if not isinstance(capabilities, list) or not 1 <= len(capabilities) <= 20 or any(
        not isinstance(item, str) or not item.strip() or len(item) > 120 for item in capabilities
    ):
        raise ValueError("Invalid listing capabilities")
    prompts = interface.get("defaultPrompt", [])
    if not isinstance(prompts, list) or len(prompts) > 3 or any(
        not isinstance(item, str) or not item.strip() or len(item) > 128 for item in prompts
    ) or len({item.casefold().strip() for item in prompts}) != len(prompts):
        raise ValueError("Invalid or duplicate starter prompts")
    for field in ("logo", "composerIcon"):
        value = interface.get(field)
        if not isinstance(value, str) or not value.startswith("./"):
            raise ValueError(f"Missing or invalid listing asset: {field}")
        asset = (PLUGIN / value).resolve()
        if not asset.is_relative_to(PLUGIN) or not asset.is_file() or asset.suffix.lower() != ".svg":
            raise ValueError(f"Missing or unsafe listing asset: {field}")
        if asset.stat().st_size > 5 * 1024 * 1024:
            raise ValueError(f"Listing asset too large: {field}")
        root = ElementTree.parse(asset).getroot()
        try:
            width = float(root.attrib["width"])
            height = float(root.attrib["height"])
        except (KeyError, ValueError) as exc:
            raise ValueError(f"Listing icon must have numeric dimensions: {field}") from exc
        if width != height or width < 48:
            raise ValueError(f"Listing icon must be square and at least 48px: {field}")


def validate() -> None:
    schema = json.loads((PLUGIN / "scripts" / "plugin.schema.json").read_text())
    Draft202012Validator.check_schema(schema)
    manifest = json.loads((PLUGIN / "plugin.json").read_text())
    Draft202012Validator(schema).validate(manifest)
    if manifest["name"] != "rawbbit-game-events":
        raise ValueError("Unexpected plugin name")
    validate_listing(manifest)

    mcp_schema = json.loads((PLUGIN / "scripts" / "mcp.schema.json").read_text())
    Draft202012Validator.check_schema(mcp_schema)
    mcp_config = json.loads((PLUGIN / "mcp.json").read_text())
    # Agent Plugins 1.0 MCP schema omits host-specific extensions that OpenAI's
    # plugin docs explicitly place on each server. Validate the portable core
    # against the schema, then validate the complete OpenAI overlay below.
    portable_mcp = copy.deepcopy(mcp_config)
    for entry in portable_mcp.get("mcpServers", {}).values():
        entry.pop("extensions", None)
    Draft202012Validator(mcp_schema).validate(portable_mcp)
    if set(mcp_config.get("mcpServers", {})) != {"rawbbit-sdk-catalog"}:
        raise ValueError("Expected exactly the public Rawbbit SDK catalog MCP connection")
    server = mcp_config["mcpServers"]["rawbbit-sdk-catalog"]
    if set(server) != {"type", "url", "extensions"}:
        raise ValueError("Unexpected SDK catalog MCP connection fields")
    if server.get("type") != "streamable-http" or server.get("url") != "https://sdk-catalog.rawbbit.one/mcp":
        raise ValueError("Unexpected SDK catalog transport or endpoint")
    if server.get("extensions", {}).get("com.openai", {}).get("auth") != {"type": "none"}:
        raise ValueError("Public SDK catalog must declare no authentication")
    if server.get("headers") or server.get("env"):
        raise ValueError("Public SDK catalog connection must not include credentials")

    raw_skill = SKILL.read_text()
    frontmatter = re.match(r"\A---\n(.*?)\n---\n(.*)\Z", raw_skill, re.DOTALL)
    if frontmatter is None or not frontmatter.group(2).strip():
        raise ValueError("Missing skill frontmatter or body")
    metadata = yaml.safe_load(frontmatter.group(1))
    if not isinstance(metadata, dict):
        raise ValueError("Skill frontmatter must be a mapping")
    if metadata.get("name") != manifest["name"]:
        raise ValueError("Skill name does not match plugin")
    description = metadata.get("description")
    if not isinstance(description, str) or not description.strip() or len(description) > 1024:
        raise ValueError("Skill description is missing or invalid")

    for relative in REQUIRED:
        if not (PLUGIN / relative).is_file():
            raise ValueError(f"Missing required package file: {relative}")
    contract = json.loads((SKILL_ROOT / "references" / "collector-contract.json").read_text())
    if not contract.get("models", {}).get("EventEnvelope") or not contract.get("defaults"):
        raise ValueError("Missing generated Collector contract facts")

    documents = [PLUGIN / "README.md", SKILL, *(SKILL_ROOT / "references").glob("*.md")]
    for document in documents:
        for target in LINK.findall(document.read_text()):
            url = urlsplit(target)
            if url.scheme or url.netloc or not url.path:
                continue
            resolved = (document.parent / unquote(url.path)).resolve()
            if not resolved.is_relative_to(PLUGIN) or not resolved.is_file():
                raise ValueError(f"Broken local documentation link in {document}: {target}")
    print(f"Validated plugin manifest, listing, skill, required assets, and links in {len(documents)} documents")


if __name__ == "__main__":
    try:
        validate()
    except (
        OSError, ValueError, KeyError, json.JSONDecodeError, yaml.YAMLError,
        SchemaError, ValidationError, ElementTree.ParseError,
    ) as exc:
        print(f"Plugin validation error: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
