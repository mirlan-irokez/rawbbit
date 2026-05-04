from __future__ import annotations

import unittest

from raw_writer.transformer import event_to_row


class _Sequence:
    def __init__(self, stream: object) -> None:
        self.stream = stream


class _Metadata:
    def __init__(self, stream: object, sequence_stream: object) -> None:
        self.stream = stream
        self.sequence = _Sequence(sequence_stream)


class _Msg:
    def __init__(self, metadata: object | None) -> None:
        self.metadata = metadata


class TransformerTests(unittest.TestCase):
    def test_event_to_row_normalizes_types(self) -> None:
        event = {
            "event_id": 123,
            "app_id": "com.example.app",
            "environment": 42,
            "event_name": "opened",
            "event_timestamp": "2026-04-13T10:00:00Z",
            "received_at": "2026-04-13T10:00:01Z",
            "user": {"user_id": 7, "user_pseudo_id": "anon", "session_id": "s1"},
            "device": {"platform": "web", "timezone": "UTC"},
            "event_params": {"a": 1},
            "user_properties": None,
            "traffic_source": None,
            "geo": {"country": "FI"},
            "consent": None,
            "ingest": {"request_id": 987, "user_agent": "ua", "ip_hash": "abc"},
        }
        msg = _Msg(_Metadata("EVENTS", "12345"))

        partition, row = event_to_row(event, msg)

        self.assertEqual(partition, ("com.example.app", "2026-04-13", "10"))
        self.assertEqual(row["event_id"], "123")
        self.assertEqual(row["environment"], "42")
        self.assertEqual(row["event_timestamp"], "2026-04-13T10:00:00Z")
        self.assertEqual(row["received_at"], "2026-04-13T10:00:01Z")
        self.assertEqual(row["ingest_request_id"], "987")
        self.assertEqual(row["nats_stream"], "EVENTS")
        self.assertEqual(row["nats_sequence"], 12345)
        self.assertIsInstance(row["event_params_json"], str)
        self.assertEqual(row["user_properties_json"], "{}")

    def test_event_to_row_uses_none_for_missing_strings_and_zero_for_sequence(self) -> None:
        event = {
            "event_id": "evt-1",
            "app_id": "com.example.app",
            "event_name": "opened",
            "event_timestamp": "2026-04-13T10:00:00Z",
            "ingest": {},
        }
        msg = _Msg(None)

        _partition, row = event_to_row(event, msg)

        self.assertIsNone(row["environment"])
        self.assertIsNone(row["user_id"])
        self.assertIsNone(row["nats_stream"])
        self.assertEqual(row["nats_sequence"], 0)

    def test_event_to_row_sets_sequence_to_zero_when_over_int64_range(self) -> None:
        event = {
            "event_id": "evt-1",
            "app_id": "com.example.app",
            "event_name": "opened",
            "event_timestamp": "2026-04-13T10:00:00Z",
        }
        msg = _Msg(_Metadata("EVENTS", str(2**70)))

        _partition, row = event_to_row(event, msg)
        self.assertEqual(row["nats_sequence"], 0)

        int_msg = _Msg(_Metadata("EVENTS", 2**70))
        _partition, int_row = event_to_row(event, int_msg)
        self.assertEqual(int_row["nats_sequence"], 0)

    def test_event_to_row_sets_sequence_to_zero_when_under_int64_range(self) -> None:
        event = {
            "event_id": "evt-1",
            "app_id": "com.example.app",
            "event_name": "opened",
            "event_timestamp": "2026-04-13T10:00:00Z",
        }
        msg = _Msg(_Metadata("EVENTS", -(2**70)))

        _partition, row = event_to_row(event, msg)
        self.assertEqual(row["nats_sequence"], 0)


if __name__ == "__main__":
    unittest.main()
