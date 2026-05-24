from __future__ import annotations

from pathlib import Path
from typing import Protocol


class ObjectStorageUploader(Protocol):
    def upload_file(self, local_path: Path, object_path: str, retries: int = 3) -> None:
        ...
