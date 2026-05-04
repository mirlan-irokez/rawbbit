from __future__ import annotations

import asyncio
import json
import logging
from typing import Any
from urllib.parse import urlsplit

from nats.aio.client import Client as NATS
from nats.js.api import StorageType, StreamConfig

logger = logging.getLogger("collector_api.nats")


def _redacted_url(url: str) -> str:
    parts = urlsplit(url)
    host = parts.hostname or ""
    try:
        port_value = parts.port
    except ValueError:
        port_value = None
    port = f":{port_value}" if port_value else ""
    return f"{parts.scheme}://{host}{port}"


class NATSPublisher:
    def __init__(
        self,
        nats_url: str,
        stream_name: str,
        subject_prefix: str,
        stream_max_age_seconds: int,
        duplicate_window_seconds: int,
    ) -> None:
        self._nats_url = nats_url
        self._stream_name = stream_name
        self._subject_prefix = subject_prefix
        self._stream_max_age_seconds = stream_max_age_seconds
        self._duplicate_window_seconds = duplicate_window_seconds
        self._nc = NATS()
        self._js = None
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        logger.info(
            "nats_connect_start url=%s stream=%s subject_pattern=%s",
            _redacted_url(self._nats_url),
            self._stream_name,
            f"{self._subject_prefix}.>",
        )
        await self._nc.connect(servers=[self._nats_url])
        self._js = self._nc.jetstream()
        await self._ensure_stream()
        logger.info("nats_connect_ready stream=%s", self._stream_name)

    async def _ensure_stream(self) -> None:
        assert self._js is not None
        cfg = StreamConfig(
            name=self._stream_name,
            subjects=[f"{self._subject_prefix}.>"],
            storage=StorageType.FILE,
            max_age=self._stream_max_age_seconds,
            duplicate_window=self._duplicate_window_seconds,
        )
        try:
            await self._js.add_stream(config=cfg)
            logger.info("stream_created stream=%s", self._stream_name)
        except Exception as exc:
            info = await self._js.stream_info(self._stream_name)
            subjects = set(info.config.subjects or [])
            expected_subject = f"{self._subject_prefix}.>"
            if expected_subject not in subjects:
                raise RuntimeError(
                    f"JetStream stream '{self._stream_name}' exists with incompatible subjects {sorted(subjects)}; expected to include '{expected_subject}'"
                ) from exc
            if info.config.storage != StorageType.FILE:
                raise RuntimeError(
                    f"JetStream stream '{self._stream_name}' uses storage '{info.config.storage}', expected FILE"
                ) from exc
            logger.info("stream_exists_and_compatible stream=%s subjects=%s", self._stream_name, sorted(subjects))

    async def publish(self, subject: str, payload: dict[str, Any], msg_id: str) -> None:
        assert self._js is not None
        data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        headers = {"Nats-Msg-Id": msg_id}
        async with self._lock:
            await self._js.publish(subject=subject, payload=data, headers=headers)

    async def close(self) -> None:
        if self._nc.is_connected:
            await self._nc.drain()
