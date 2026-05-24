from __future__ import annotations

import unittest
from unittest.mock import patch

from raw_writer.settings import Settings
import raw_writer.storage.gcs  # ensure submodule is loaded for patch target resolution
import raw_writer.storage.s3  # ensure submodule is loaded for patch target resolution
from raw_writer.storage.factory import build_object_storage_uploader


class StorageFactoryTests(unittest.TestCase):
    @patch("raw_writer.storage.gcs.GCSStorageUploader")
    def test_builds_gcs_uploader(self, gcs_cls) -> None:
        settings = Settings(RAW_STORAGE_BACKEND="gcs", GCS_RAW_BUCKET="bucket-a")
        build_object_storage_uploader(settings)
        gcs_cls.assert_called_once_with(bucket_name="bucket-a")

    @patch("raw_writer.storage.s3.S3StorageUploader")
    def test_builds_s3_uploader(self, s3_cls) -> None:
        settings = Settings(
            RAW_STORAGE_BACKEND="s3",
            S3_ENDPOINT_URL="http://seaweedfs:8333",
            S3_BUCKET="events",
            S3_ACCESS_KEY="key",
            S3_SECRET_KEY="secret",
        )
        build_object_storage_uploader(settings)
        s3_cls.assert_called_once()


if __name__ == "__main__":
    unittest.main()
