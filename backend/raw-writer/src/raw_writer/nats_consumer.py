from __future__ import annotations

import logging
from urllib.parse import urlsplit

from nats.aio.client import Client as NATS
from nats.errors import TimeoutError
from nats.js.api import AckPolicy, ConsumerConfig

from raw_writer.settings import Settings

logger = logging.getLogger("raw_writer.nats")


def _redacted_url(url: str) -> str:
    parts = urlsplit(url)
    host = parts.hostname or ""
    try:
        port_value = parts.port
    except ValueError:
        port_value = None
    port = f":{port_value}" if port_value else ""
    return f"{parts.scheme}://{host}{port}"


class NATSConsumer:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._nc = NATS()
        self._subscription = None

    async def connect(self) -> None:
        logger.info(
            "connect_start url=%s stream=%s consumer=%s subject_pattern=%s",
            _redacted_url(self._settings.nats_url),
            self._settings.nats_stream,
            self._settings.nats_consumer,
            f"{self._settings.nats_subject_prefix}.>",
        )
        await self._nc.connect(servers=[self._settings.nats_url])
        js = self._nc.jetstream()
        cfg = ConsumerConfig(
            durable_name=self._settings.nats_consumer,
            ack_policy=AckPolicy.EXPLICIT,
            ack_wait=self._settings.nats_ack_wait_seconds,
            max_deliver=self._settings.nats_max_deliver,
        )
        try:
            self._subscription = await js.pull_subscribe(
                subject=f"{self._settings.nats_subject_prefix}.>",
                durable=self._settings.nats_consumer,
                stream=self._settings.nats_stream,
                config=cfg,
            )
            logger.info("connect_ready stream=%s consumer=%s", self._settings.nats_stream, self._settings.nats_consumer)
        except Exception as exc:
            logger.error(
                "connect_failed stream=%s consumer=%s error_class=%s",
                self._settings.nats_stream,
                self._settings.nats_consumer,
                exc.__class__.__name__,
            )
            raise RuntimeError(
                "Failed to bind JetStream consumer. Ensure stream exists and, if migrating from events.* to events.>, recreate durable consumer."
            ) from exc

    async def fetch(self):
        if self._subscription is None:
            return []
        try:
            return await self._subscription.fetch(
                batch=self._settings.nats_fetch_batch,
                timeout=self._settings.nats_fetch_timeout_seconds,
            )
        except TimeoutError:
            return []

    async def close(self) -> None:
        if self._nc.is_connected:
            await self._nc.drain()

    @property
    def is_connected(self) -> bool:
        return self._nc.is_connected
