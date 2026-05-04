from __future__ import annotations

import asyncio
import json
import logging
import os
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

from raw_writer.gcs_client import GCSUploader
from raw_writer.nats_consumer import NATSConsumer
from raw_writer.parquet_writer import write_rows_to_temp_parquet
from raw_writer.partitioning import gcs_partition_path
from raw_writer.settings import get_settings
from raw_writer.transformer import event_to_row

logging.basicConfig(
    level=get_settings().log_level.upper(),
    format="%(asctime)s %(levelname)s service=raw-writer event=%(message)s",
)
logger = logging.getLogger("raw_writer")


def _redacted_url(url: str) -> str:
    parts = urlsplit(url)
    host = parts.hostname or ""
    try:
        port_value = parts.port
    except ValueError:
        port_value = None
    port = f":{port_value}" if port_value else ""
    return f"{parts.scheme}://{host}{port}"


def _safe_token(value: str) -> str:
    return "".join(ch if ch.isprintable() and ch not in "\r\n\t" else "_" for ch in value)


class BufferState:
    def __init__(self) -> None:
        self.rows: list[dict] = []
        self.msgs = []
        self.approx_bytes: int = 0


class RawWriterService:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.consumer = NATSConsumer(self.settings)
        self.uploader = GCSUploader(self.settings.gcs_raw_bucket)
        self.buffers: dict[tuple[str, str, str], BufferState] = defaultdict(BufferState)
        self.last_flush = datetime.now(UTC)
        self.last_successful_flush: datetime | None = None

        credentials_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "")
        logger.info(
            "startup nats_url=%s stream=%s consumer=%s gcs_bucket=%s gcs_prefix=%s flush_interval_seconds=%s max_events_per_file=%s max_bytes_per_file=%s credentials_path_set=%s",
            _redacted_url(self.settings.nats_url),
            self.settings.nats_stream,
            self.settings.nats_consumer,
            self.settings.gcs_raw_bucket,
            self.settings.gcs_raw_prefix,
            self.settings.raw_flush_interval_seconds,
            self.settings.raw_max_events_per_file,
            self.settings.raw_max_bytes_per_file,
            bool(credentials_path),
        )

    async def run(self) -> None:
        await self.consumer.connect()
        logger.info("consumer_ready")

        while True:
            messages = await self.consumer.fetch()
            for msg in messages:
                try:
                    event = json.loads(msg.data.decode("utf-8"))
                except Exception:
                    logger.warning("message_decode_failed action=term")
                    await msg.term()
                    continue

                partition_key, row = event_to_row(event, msg)
                buf = self.buffers[partition_key]
                buf.rows.append(row)
                buf.msgs.append(msg)
                buf.approx_bytes += len(msg.data)

                should_flush_by_count = len(buf.rows) >= self.settings.raw_max_events_per_file
                should_flush_by_bytes = buf.approx_bytes >= self.settings.raw_max_bytes_per_file
                if should_flush_by_count or should_flush_by_bytes:
                    await self._flush_partition(partition_key, buf)

            elapsed = (datetime.now(UTC) - self.last_flush).total_seconds()
            if elapsed >= self.settings.raw_flush_interval_seconds:
                await self.flush_all()

    async def _flush_partition(self, partition_key: tuple[str, str, str], state: BufferState) -> None:
        if not state.rows:
            return

        app_id, event_date, hour = partition_key
        app_id_safe = _safe_token(app_id)
        logger.debug(
            "flush_start app_id=%s event_date=%s hour=%s rows=%s approx_bytes=%s",
            app_id_safe,
            event_date,
            hour,
            len(state.rows),
            state.approx_bytes,
        )
        filename = f"part-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S')}-{uuid4().hex[:12]}.parquet"
        object_path = gcs_partition_path(
            prefix=self.settings.gcs_raw_prefix,
            app_id=app_id,
            event_date=event_date,
            hour=hour,
            filename=filename,
        )

        temp_file: Path | None = None
        try:
            temp_file = write_rows_to_temp_parquet(state.rows)
            parquet_bytes = temp_file.stat().st_size
            self.uploader.upload_file(temp_file, object_path=object_path)
            for msg in state.msgs:
                await msg.ack()
            self.last_successful_flush = datetime.now(UTC)
            logger.info(
                "flush_success app_id=%s event_date=%s hour=%s rows=%s parquet_bytes=%s acked_messages=%s gcs_object_path=%s",
                app_id_safe,
                event_date,
                hour,
                len(state.rows),
                parquet_bytes,
                len(state.msgs),
                object_path,
            )
        except Exception as exc:
            for msg in state.msgs:
                try:
                    await msg.nak()
                except Exception:
                    pass
            logger.error(
                "flush_failed app_id=%s event_date=%s hour=%s rows=%s naks=%s error_class=%s",
                app_id_safe,
                event_date,
                hour,
                len(state.rows),
                len(state.msgs),
                exc.__class__.__name__,
            )
            self.buffers.pop(partition_key, None)
            return
        finally:
            if temp_file and temp_file.exists():
                os.unlink(temp_file)

        self.buffers.pop(partition_key, None)

    async def flush_all(self) -> None:
        for key, state in list(self.buffers.items()):
            await self._flush_partition(key, state)
        self.last_flush = datetime.now(UTC)

    async def close(self) -> None:
        await self.flush_all()
        await self.consumer.close()
        logger.info("shutdown_complete")


async def _run() -> None:
    service = RawWriterService()
    try:
        await service.run()
    finally:
        await service.close()


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
