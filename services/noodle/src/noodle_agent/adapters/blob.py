from __future__ import annotations

import hashlib
import shutil
from pathlib import Path


class LocalBlobStore:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def put(self, source: Path, media_type: str) -> tuple[str, str, int]:
        del media_type
        digest = hashlib.sha256()
        with source.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        sha256 = digest.hexdigest()
        suffix = source.suffix.lower() or ".bin"
        key = f"{sha256[:2]}/{sha256}{suffix}"
        target = self.root / key
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            shutil.copyfile(source, target)
        return key, sha256, source.stat().st_size

    def path_for(self, storage_key: str) -> Path:
        path = (self.root / storage_key).resolve()
        if self.root.resolve() not in path.parents:
            raise ValueError("Blob storage key escapes configured root")
        return path
