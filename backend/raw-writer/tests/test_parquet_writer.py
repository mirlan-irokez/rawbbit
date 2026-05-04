from __future__ import annotations

import os
import unittest

import pyarrow as pa
import pyarrow.parquet as pq

from raw_writer.parquet_writer import RAW_SCHEMA, write_rows_to_temp_parquet


def _base_row() -> dict[str, object]:
    return {
        "event_id": "evt-1",
        "app_id": "com.example.app",
        "environment": "prod",
        "event_name": "opened",
        "event_timestamp": "2026-04-13T10:00:00Z",
        "received_at": "2026-04-13T10:00:01Z",
        "user_id": "u1",
        "user_pseudo_id": "anon",
        "session_id": "s1",
        "platform": "web",
        "app_version": "1.0.0",
        "os_version": "macOS",
        "device_model": "Mac",
        "locale": "en-US",
        "timezone": "UTC",
        "event_params_json": "{}",
        "user_properties_json": "{}",
        "traffic_source_json": "{}",
        "geo_json": "{}",
        "consent_json": "{}",
        "ingest_request_id": "req-1",
        "ingest_user_agent": "ua",
        "ingest_ip_hash": "hash",
        "nats_stream": "EVENTS",
        "nats_sequence": 123,
    }


class ParquetWriterTests(unittest.TestCase):
    def test_write_rows_uses_explicit_schema(self) -> None:
        path = write_rows_to_temp_parquet([_base_row()])
        try:
            schema = pq.read_schema(path)
            self.assertEqual(schema, RAW_SCHEMA)
            self.assertEqual(schema.field("event_timestamp").type, pa.string())
            self.assertEqual(schema.field("received_at").type, pa.string())
            self.assertEqual(schema.field("geo_json").type, pa.string())
            self.assertEqual(schema.field("nats_sequence").type, pa.int64())
        finally:
            if path.exists():
                os.unlink(path)

    def test_null_heavy_rows_keep_schema(self) -> None:
        row = _base_row()
        for key in row:
            if key not in {"app_id", "event_id", "event_name", "event_timestamp", "nats_sequence"}:
                row[key] = None

        path = write_rows_to_temp_parquet([row])
        try:
            schema = pq.read_schema(path)
            self.assertEqual(schema, RAW_SCHEMA)
            table = pq.read_table(path)
            self.assertEqual(table.column("nats_sequence").to_pylist(), [123])
        finally:
            if path.exists():
                os.unlink(path)


if __name__ == "__main__":
    unittest.main()
