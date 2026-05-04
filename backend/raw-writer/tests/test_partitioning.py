from __future__ import annotations

import unittest

from raw_writer.partitioning import event_partition, gcs_partition_path


class PartitioningTests(unittest.TestCase):
    def test_event_partition_uses_event_timestamp(self) -> None:
        event_date, hour = event_partition("2026-03-16T18:42:15.123Z", "2026-03-16T20:00:00Z")
        self.assertEqual(event_date, "2026-03-16")
        self.assertEqual(hour, "18")

    def test_event_partition_falls_back(self) -> None:
        event_date, hour = event_partition("bad-date", "2026-03-16T20:00:00Z")
        self.assertEqual(event_date, "2026-03-16")
        self.assertEqual(hour, "20")

    def test_gcs_partition_path(self) -> None:
        path = gcs_partition_path("raw/", "mygame", "2026-03-16", "20", "part-1.parquet")
        self.assertEqual(path, "raw/app_id=mygame/event_date=2026-03-16/hour=20/part-1.parquet")


if __name__ == "__main__":
    unittest.main()
