"""Disk storage adapters: atomic page writes, resume checks, manifests."""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any


class LocalPageStore:
    """Filesystem adapter implementing the application page-store port."""

    def ensure_directory(self, path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)

    def exists(self, path: Path) -> bool:
        return path.is_file()

    def remove_file(self, path: Path) -> None:
        path.unlink(missing_ok=True)

    def is_usable(self, path: Path, min_bytes: int) -> bool:
        return is_usable(path, min_bytes)

    def read_text(self, path: Path, encoding: str = "utf-8") -> str:
        return path.read_text(encoding=encoding)

    def write_text(self, path: Path, text: str, encoding: str = "utf-8") -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding=encoding)

    def safe_write(self, path: Path, text: str) -> None:
        safe_write(path, text)

    def append_jsonl(self, path: Path, record: dict[str, Any]) -> None:
        manifest_log(path, record)

    def list_files(self, directory: Path, pattern: str) -> list[Path]:
        return sorted(path for path in directory.glob(pattern) if path.is_file())

    def list_directories(self, directory: Path) -> list[Path]:
        return sorted(path for path in directory.iterdir() if path.is_dir())


def safe_write(path: Path, text: str) -> None:
    """Atomic-ish write: temp file then rename (keeps resume logic honest)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".part")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def is_usable(path: Path, min_bytes: int) -> bool:
    try:
        return path.is_file() and path.stat().st_size >= min_bytes
    except OSError:
        return False


def manifest_log(manifest_path: Path, record: dict[str, Any]) -> None:
    """Append one JSONL record with a timestamp to the manifest."""
    record = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), **record}
    with manifest_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
