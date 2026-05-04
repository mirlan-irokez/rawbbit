from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

RAW_SCHEMA = pa.schema(
    [
        pa.field("event_id", pa.string()),
        pa.field("app_id", pa.string()),
        pa.field("environment", pa.string()),
        pa.field("event_name", pa.string()),
        pa.field("event_timestamp", pa.string()),
        pa.field("received_at", pa.string()),
        pa.field("user_id", pa.string()),
        pa.field("user_pseudo_id", pa.string()),
        pa.field("session_id", pa.string()),
        pa.field("platform", pa.string()),
        pa.field("app_version", pa.string()),
        pa.field("os_version", pa.string()),
        pa.field("device_model", pa.string()),
        pa.field("locale", pa.string()),
        pa.field("timezone", pa.string()),
        pa.field("event_params_json", pa.string()),
        pa.field("user_properties_json", pa.string()),
        pa.field("traffic_source_json", pa.string()),
        pa.field("geo_json", pa.string()),
        pa.field("consent_json", pa.string()),
        pa.field("ingest_request_id", pa.string()),
        pa.field("ingest_user_agent", pa.string()),
        pa.field("ingest_ip_hash", pa.string()),
        pa.field("nats_stream", pa.string()),
        pa.field("nats_sequence", pa.int64()),
    ]
)


def write_rows_to_temp_parquet(rows: list[dict[str, Any]]) -> Path:
    if not rows:
        raise ValueError("rows cannot be empty")

    table = pa.Table.from_pylist(rows, schema=RAW_SCHEMA)
    with tempfile.NamedTemporaryFile(prefix="raw-events-", suffix=".parquet", delete=False) as tmp:
        temp_path = Path(tmp.name)

    pq.write_table(table, temp_path, compression="snappy")
    return temp_path
