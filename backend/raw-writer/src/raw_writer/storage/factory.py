from __future__ import annotations

from raw_writer.settings import Settings
from raw_writer.storage.base import ObjectStorageUploader


def build_object_storage_uploader(settings: Settings) -> ObjectStorageUploader:
    if settings.raw_storage_backend == "gcs":
        from raw_writer.storage.gcs import GCSStorageUploader

        return GCSStorageUploader(bucket_name=settings.gcs_raw_bucket)

    if settings.raw_storage_backend == "s3":
        from raw_writer.storage.s3 import S3StorageUploader

        return S3StorageUploader(
            endpoint_url=settings.s3_endpoint_url,
            bucket_name=settings.s3_bucket,
            access_key=settings.s3_access_key,
            secret_key=settings.s3_secret_key,
            region=settings.s3_region,
            force_path_style=settings.s3_force_path_style,
            use_ssl=settings.s3_use_ssl,
            verify_ssl=settings.s3_verify_ssl,
        )

    raise RuntimeError(f"Unsupported RAW_STORAGE_BACKEND: {settings.raw_storage_backend}")
