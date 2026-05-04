from __future__ import annotations

import logging
import time
from pathlib import Path

from google.cloud import storage

logger = logging.getLogger("raw_writer.gcs")


class GCSUploader:
    def __init__(self, bucket_name: str) -> None:
        self._client = storage.Client()
        self._bucket = self._client.bucket(bucket_name)

    def upload_file(self, local_path: Path, object_path: str, retries: int = 3) -> None:
        blob = self._bucket.blob(object_path)
        attempt = 0
        while True:
            try:
                blob.upload_from_filename(str(local_path), timeout=60)
                return
            except Exception as exc:
                attempt += 1
                logger.warning(
                    "upload_retry bucket=%s object_path=%s attempt=%s/%s error_class=%s",
                    self._bucket.name,
                    object_path,
                    attempt,
                    retries,
                    exc.__class__.__name__,
                )
                if attempt >= retries:
                    raise
                time.sleep(2**attempt)
