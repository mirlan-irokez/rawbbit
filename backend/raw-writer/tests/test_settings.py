from __future__ import annotations

import unittest

from raw_writer.settings import Settings


class SettingsTests(unittest.TestCase):
    def test_gcs_backend_requires_bucket(self) -> None:
        with self.assertRaises(ValueError):
            Settings(RAW_STORAGE_BACKEND="gcs", GCS_RAW_BUCKET="")

    def test_gcs_backend_allows_bucket(self) -> None:
        s = Settings(RAW_STORAGE_BACKEND="gcs", GCS_RAW_BUCKET="bucket-a")
        self.assertEqual(s.raw_storage_backend, "gcs")

    def test_backend_selector_is_trimmed_and_lowercased(self) -> None:
        s = Settings(RAW_STORAGE_BACKEND=" S3 ", S3_ENDPOINT_URL="http://seaweedfs:8333", S3_BUCKET="events", S3_ACCESS_KEY="key", S3_SECRET_KEY="secret")
        self.assertEqual(s.raw_storage_backend, "s3")

    def test_gcs_backend_rejects_whitespace_bucket(self) -> None:
        with self.assertRaises(ValueError):
            Settings(RAW_STORAGE_BACKEND="gcs", GCS_RAW_BUCKET="   ")

    def test_s3_backend_requires_core_fields(self) -> None:
        with self.assertRaises(ValueError):
            Settings(RAW_STORAGE_BACKEND="s3", S3_BUCKET="events")

    def test_s3_backend_allows_required_fields(self) -> None:
        s = Settings(
            RAW_STORAGE_BACKEND="s3",
            S3_ENDPOINT_URL="http://seaweedfs:8333",
            S3_BUCKET="events",
            S3_ACCESS_KEY="key",
            S3_SECRET_KEY="secret",
        )
        self.assertEqual(s.raw_storage_backend, "s3")
        self.assertEqual(s.s3_region, "us-east-1")

    def test_s3_backend_rejects_whitespace_required_fields(self) -> None:
        with self.assertRaises(ValueError):
            Settings(
                RAW_STORAGE_BACKEND="s3",
                S3_ENDPOINT_URL="   ",
                S3_BUCKET="events",
                S3_ACCESS_KEY="key",
                S3_SECRET_KEY="secret",
            )

    def _s3(self, **overrides) -> Settings:
        values = dict(
            RAW_STORAGE_BACKEND="s3",
            S3_ENDPOINT_URL="http://seaweedfs:8333",
            S3_BUCKET="events",
            S3_ACCESS_KEY="key",
            S3_SECRET_KEY="secret",
        )
        values.update(overrides)
        return Settings(**values)

    def test_flush_interval_must_be_below_ack_wait(self) -> None:
        with self.assertRaisesRegex(ValueError, "RAW_FLUSH_INTERVAL_SECONDS must be lower than NATS_ACK_WAIT_SECONDS"):
            self._s3(RAW_FLUSH_INTERVAL_SECONDS=300, NATS_ACK_WAIT_SECONDS=300)
        with self.assertRaises(ValueError):
            self._s3(RAW_FLUSH_INTERVAL_SECONDS=900, NATS_ACK_WAIT_SECONDS=120)

    def test_default_and_quickstart_timing_pass(self) -> None:
        self.assertEqual(self._s3().raw_flush_interval_seconds, 60)
        s = self._s3(RAW_FLUSH_INTERVAL_SECONDS=60, NATS_ACK_WAIT_SECONDS=300)
        self.assertEqual(s.nats_ack_wait_seconds, 300)

    def test_max_ack_pending_default_and_empty_buffer_override(self) -> None:
        s = self._s3(RAW_MAX_BUFFERED_EVENTS="")
        self.assertEqual(s.nats_max_ack_pending, 10000)
        self.assertIsNone(s.raw_max_buffered_events)
        self.assertEqual(self._s3(RAW_MAX_BUFFERED_EVENTS="500").raw_max_buffered_events, 500)


if __name__ == "__main__":
    unittest.main()
