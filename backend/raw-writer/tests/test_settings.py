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


if __name__ == "__main__":
    unittest.main()
