from __future__ import annotations

import logging
import time
from pathlib import Path

import boto3
from botocore.config import Config

logger = logging.getLogger("raw_writer.storage.s3")


class S3StorageUploader:
    def __init__(
        self,
        *,
        endpoint_url: str,
        bucket_name: str,
        access_key: str,
        secret_key: str,
        region: str,
        force_path_style: bool,
        use_ssl: bool,
        verify_ssl: bool,
    ) -> None:
        self._bucket_name = bucket_name
        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=region,
            use_ssl=use_ssl,
            verify=verify_ssl,
            config=Config(s3={"addressing_style": "path" if force_path_style else "virtual"}),
        )

    def upload_file(self, local_path: Path, object_path: str, retries: int = 3) -> None:
        attempt = 0
        while True:
            try:
                self._client.upload_file(str(local_path), self._bucket_name, object_path)
                return
            except Exception as exc:
                attempt += 1
                logger.warning(
                    "upload_retry backend=s3 bucket=%s object_path=%s attempt=%s/%s error_class=%s",
                    self._bucket_name,
                    object_path,
                    attempt,
                    retries,
                    exc.__class__.__name__,
                )
                if attempt >= retries:
                    raise
                time.sleep(2**attempt)
