from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

from raw_writer.nats_consumer import NATSConsumer
from raw_writer.parquet_writer import write_rows_to_temp_parquet
from raw_writer.partitioning import object_partition_path
from raw_writer.settings import Settings, get_settings
from raw_writer.storage import build_object_storage_uploader
from raw_writer.transformer import event_to_row

logger = logging.getLogger("raw_writer")

# Share of the consumer's max_ack_pending the writer may hold unACKed before it flushes early.
PRESSURE_THRESHOLD_RATIO = 0.8


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


def buffer_threshold(max_ack_pending: int, override: int | None) -> int:
    """Total buffered events that trigger a pressure flush.

    JetStream stops delivering once max_ack_pending messages are unACKed, so the writer must
    flush before its buffer reaches that limit. RAW_MAX_BUFFERED_EVENTS can only lower it.
    """
    threshold = max(1, int(max_ack_pending * PRESSURE_THRESHOLD_RATIO))
    if override is not None:
        threshold = min(threshold, override)
    return threshold


class BufferState:
    def __init__(self) -> None:
        self.rows: list[dict] = []
        self.msgs = []
        self.approx_bytes: int = 0


class RawWriterService:
    def __init__(self, settings: Settings | None = None, consumer=None, uploader=None) -> None:
        self.settings = settings or get_settings()
        self.consumer = consumer or NATSConsumer(self.settings)
        self.uploader = uploader or build_object_storage_uploader(self.settings)
        self.buffers: dict[tuple[str, str, str], BufferState] = defaultdict(BufferState)
        self.buffered_events = 0
        self.consumer_max_ack_pending: int | None = None
        self.threshold = buffer_threshold(self.settings.nats_max_ack_pending, self.settings.raw_max_buffered_events)
        self.last_flush = datetime.now(UTC)
        self.last_successful_flush: datetime | None = None
        self._stopping = asyncio.Event()
        self._closed = False

        credentials_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "")
        storage_bucket = self.settings.gcs_raw_bucket if self.settings.raw_storage_backend == "gcs" else self.settings.s3_bucket
        storage_prefix = self.settings.gcs_raw_prefix if self.settings.raw_storage_backend == "gcs" else self.settings.s3_prefix
        logger.info(
            "startup nats_url=%s stream=%s consumer=%s storage_backend=%s storage_bucket=%s storage_prefix=%s flush_interval_seconds=%s max_events_per_file=%s max_bytes_per_file=%s max_buffered_events_override=%s google_credentials_path_set=%s",
            _redacted_url(self.settings.nats_url),
            self.settings.nats_stream,
            self.settings.nats_consumer,
            self.settings.raw_storage_backend,
            storage_bucket,
            storage_prefix,
            self.settings.raw_flush_interval_seconds,
            self.settings.raw_max_events_per_file,
            self.settings.raw_max_bytes_per_file,
            self.settings.raw_max_buffered_events,
            bool(credentials_path),
        )

    def request_stop(self) -> None:
        if not self._stopping.is_set():
            logger.info("shutdown_requested buffered_events=%s", self.buffered_events)
        self._stopping.set()

    async def refresh_threshold(self) -> None:
        """Re-read max_ack_pending from the server; keep the last known threshold if that fails."""
        try:
            max_ack_pending = await self.consumer.max_ack_pending()
        except Exception as exc:
            logger.warning(
                "consumer_info_failed action=keep_threshold threshold=%s error_class=%s",
                self.threshold,
                exc.__class__.__name__,
            )
            return

        if max_ack_pending <= 0:
            # -1 means unlimited on the server; keep memory bounded by the configured value instead.
            max_ack_pending = self.settings.nats_max_ack_pending

        if max_ack_pending == self.consumer_max_ack_pending:
            return

        if max_ack_pending != self.settings.nats_max_ack_pending:
            logger.warning(
                "consumer_max_ack_pending_differs consumer_max_ack_pending=%s nats_max_ack_pending_setting=%s",
                max_ack_pending,
                self.settings.nats_max_ack_pending,
            )
        self.consumer_max_ack_pending = max_ack_pending
        self.threshold = buffer_threshold(max_ack_pending, self.settings.raw_max_buffered_events)
        logger.info(
            "buffer_threshold threshold=%s consumer_max_ack_pending=%s max_buffered_events_override=%s",
            self.threshold,
            max_ack_pending,
            self.settings.raw_max_buffered_events,
        )

    async def run(self) -> None:
        await self.consumer.connect()
        await self.refresh_threshold()
        logger.info("consumer_ready threshold=%s", self.threshold)

        stop_waiter = asyncio.ensure_future(self._stopping.wait())
        try:
            while not self._stopping.is_set():
                messages = await self._fetch_or_stop(stop_waiter)
                for msg in messages:
                    await self.handle_message(msg)

                elapsed = (datetime.now(UTC) - self.last_flush).total_seconds()
                if elapsed >= self.settings.raw_flush_interval_seconds:
                    await self.flush_all()
                    await self.refresh_threshold()
        finally:
            stop_waiter.cancel()

    async def _fetch_or_stop(self, stop_waiter: asyncio.Future) -> list:
        """Fetch the next batch, but return early if shutdown is requested meanwhile.

        A pull fetch can block for NATS_FETCH_TIMEOUT_SECONDS; shutdown must not wait for it,
        or Docker's stop grace period can expire before the buffer is flushed. Messages the
        server delivered to a cancelled fetch stay unACKed and are redelivered after Ack Wait.
        """
        fetch_task = asyncio.ensure_future(self.consumer.fetch())
        done, _ = await asyncio.wait({fetch_task, stop_waiter}, return_when=asyncio.FIRST_COMPLETED)
        if fetch_task in done:
            return fetch_task.result()
        fetch_task.cancel()
        try:
            await fetch_task
        except asyncio.CancelledError:
            pass
        return []

    async def handle_message(self, msg) -> None:
        try:
            event = json.loads(msg.data.decode("utf-8"))
        except Exception:
            logger.warning("message_decode_failed action=term")
            await msg.term()
            return

        try:
            partition_key, row = event_to_row(event, msg)
        except Exception as exc:
            try:
                stream_sequence = msg.metadata.sequence.stream
            except Exception:
                stream_sequence = None
            # Never interpolate malformed metadata values into the warning.
            if type(stream_sequence) is not int:
                stream_sequence = None
            logger.warning(
                "message_transform_failed action=term error_class=%s stream_sequence=%s",
                exc.__class__.__name__,
                stream_sequence,
            )
            await msg.term()
            return

        buf = self.buffers[partition_key]
        buf.rows.append(row)
        buf.msgs.append(msg)
        buf.approx_bytes += len(msg.data)
        self.buffered_events += 1

        should_flush_by_count = len(buf.rows) >= self.settings.raw_max_events_per_file
        should_flush_by_bytes = buf.approx_bytes >= self.settings.raw_max_bytes_per_file
        if should_flush_by_count or should_flush_by_bytes:
            await self._flush_partition(partition_key, buf)

        if self.buffered_events >= self.threshold:
            await self.flush_under_pressure()

    async def flush_under_pressure(self) -> None:
        """Flush the largest partitions until the buffer is below half the threshold.

        Largest-first keeps files as large as possible while freeing ACK slots quickly.
        Every flushed partition leaves the buffer, ACKed on success or NAKed on failure,
        so the loop always makes progress.
        """
        low_water = self.threshold // 2
        before = self.buffered_events
        flushed_partitions = 0
        for key, state in sorted(self.buffers.items(), key=lambda item: len(item[1].msgs), reverse=True):
            if self.buffered_events < low_water:
                break
            await self._flush_partition(key, state)
            flushed_partitions += 1
        logger.info(
            "pressure_flush threshold=%s buffered_before=%s buffered_after=%s flushed_partitions=%s",
            self.threshold,
            before,
            self.buffered_events,
            flushed_partitions,
        )

    def _release(self, partition_key: tuple[str, str, str], state: BufferState) -> None:
        if self.buffers.get(partition_key) is state:
            self.buffers.pop(partition_key, None)
        self.buffered_events -= len(state.msgs)

    async def _flush_partition(self, partition_key: tuple[str, str, str], state: BufferState) -> None:
        if not state.rows:
            self._release(partition_key, state)
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
        prefix = self.settings.gcs_raw_prefix if self.settings.raw_storage_backend == "gcs" else self.settings.s3_prefix
        object_path = object_partition_path(
            prefix=prefix,
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
                "flush_success app_id=%s event_date=%s hour=%s rows=%s parquet_bytes=%s acked_messages=%s storage_backend=%s object_path=%s",
                app_id_safe,
                event_date,
                hour,
                len(state.rows),
                parquet_bytes,
                len(state.msgs),
                self.settings.raw_storage_backend,
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
        finally:
            if temp_file and temp_file.exists():
                os.unlink(temp_file)
            self._release(partition_key, state)

    async def flush_all(self) -> None:
        for key, state in list(self.buffers.items()):
            await self._flush_partition(key, state)
        self.last_flush = datetime.now(UTC)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self.flush_all()
        await self.consumer.close()
        logger.info("shutdown_complete")


def _install_signal_handlers(service: RawWriterService) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, service.request_stop)
        except (NotImplementedError, RuntimeError):
            pass


async def _run() -> None:
    service = RawWriterService()
    _install_signal_handlers(service)
    try:
        await service.run()
    finally:
        await service.close()


def main() -> None:
    logging.basicConfig(
        level=get_settings().log_level.upper(),
        format="%(asctime)s %(levelname)s service=raw-writer event=%(message)s",
    )
    asyncio.run(_run())


if __name__ == "__main__":
    main()
