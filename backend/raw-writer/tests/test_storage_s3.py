from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from raw_writer.storage.s3 import S3StorageUploader


class S3StorageUploaderTests(unittest.TestCase):
    @patch("raw_writer.storage.s3.boto3.client")
    def test_upload_file_uses_s3_client(self, boto_client) -> None:
        mock_client = MagicMock()
        boto_client.return_value = mock_client

        uploader = S3StorageUploader(
            endpoint_url="http://seaweedfs:8333",
            bucket_name="events",
            access_key="key",
            secret_key="secret",
            region="us-east-1",
            force_path_style=True,
            use_ssl=False,
            verify_ssl=False,
        )

        with tempfile.NamedTemporaryFile() as temp:
            uploader.upload_file(Path(temp.name), object_path="raw/app_id=a/event_date=2026-01-01/hour=00/part.parquet")

        mock_client.upload_file.assert_called_once()


if __name__ == "__main__":
    unittest.main()
