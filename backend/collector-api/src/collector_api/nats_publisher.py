from __future__ import annotations

import asyncio
import json
import logging
from typing import Any
from urllib.parse import urlsplit

from nats.aio.client import Client as NATS
from nats.js.api import StorageType, StreamConfig, StreamInfo
from nats.js.errors import NotFoundError

logger = logging.getLogger("collector_api.nats")


class BatchPublishError(Exception):
    """Raised when at least one event of a batch failed to publish."""

    def __init__(self, published: int, failed: int, first_error: BaseException) -> None:
        super().__init__(f"{failed} of {published + failed} events failed to publish")
        self.published = published
        self.failed = failed
        self.first_error = first_error


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
        max_inflight_publishes: int = 512,
    ) -> None:
        self._nats_url = nats_url
        self._stream_name = stream_name
        self._subject_prefix = subject_prefix
        self._stream_max_age_seconds = stream_max_age_seconds
        self._duplicate_window_seconds = duplicate_window_seconds
        self._nc = NATS()
        self._js = None
        # Caps JetStream publishes awaiting an ACK across all requests in this process.
        self._inflight = asyncio.Semaphore(max_inflight_publishes)

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
            info = await self._js.stream_info(self._stream_name)
        except NotFoundError:
            pass
        else:
            subjects = self._validate_stream_compatibility(info)
            logger.info(
                "stream_exists_and_compatible stream=%s subjects=%s",
                self._stream_name,
                subjects,
            )
            return

        try:
            created_info = await self._js.add_stream(config=cfg)
        except Exception as add_error:  # noqa: BLE001 - verify a possible create race.
            try:
                info = await self._js.stream_info(self._stream_name)
            except NotFoundError:
                raise add_error
            except Exception as info_error:
                raise add_error from info_error

            subjects = self._validate_stream_compatibility(info, cause=add_error)
            logger.info(
                "stream_exists_and_compatible stream=%s subjects=%s",
                self._stream_name,
                subjects,
            )
            return

        self._validate_stream_compatibility(created_info)
        logger.info("stream_created stream=%s", self._stream_name)

    def _validate_stream_compatibility(
        self, info: StreamInfo, *, cause: Exception | None = None
    ) -> list[str]:
        subjects = sorted(set(info.config.subjects or []))
        expected_subject = f"{self._subject_prefix}.>"
        if expected_subject not in subjects:
            error = RuntimeError(
                f"JetStream stream '{self._stream_name}' exists with incompatible subjects {subjects}; expected to include '{expected_subject}'"
            )
            if cause is not None:
                raise error from cause
            raise error
        if info.config.storage != StorageType.FILE:
            error = RuntimeError(
                f"JetStream stream '{self._stream_name}' uses storage '{info.config.storage}', expected FILE"
            )
            if cause is not None:
                raise error from cause
            raise error
        return subjects

    async def publish(self, subject: str, payload: dict[str, Any], msg_id: str) -> None:
        assert self._js is not None
        data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        headers = {"Nats-Msg-Id": msg_id}
        async with self._inflight:
            await self._js.publish(subject=subject, payload=data, headers=headers)

    async def publish_batch(
        self, messages: list[tuple[str, dict[str, Any], str]]
    ) -> None:
        """Publish (subject, payload, msg_id) tuples concurrently and wait for every ACK.

        Order within the batch is not preserved. Every event is attempted; if any fails,
        BatchPublishError reports how many succeeded. Retries are safe because JetStream
        deduplicates on Nats-Msg-Id within the duplicate window.
        """
        results = await asyncio.gather(
            *(
                self.publish(subject=subject, payload=payload, msg_id=msg_id)
                for subject, payload, msg_id in messages
            ),
            return_exceptions=True,
        )
        errors = [result for result in results if isinstance(result, BaseException)]
        if errors:
            raise BatchPublishError(
                published=len(results) - len(errors),
                failed=len(errors),
                first_error=errors[0],
            )

    async def close(self) -> None:
        if self._nc.is_connected:
            await self._nc.drain()
