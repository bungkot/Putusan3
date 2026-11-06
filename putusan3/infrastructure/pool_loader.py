"""Filesystem proxy-pool adapter."""
from __future__ import annotations

from pathlib import Path

from putusan3.infrastructure.playwright_engine import load_courts_file, load_pool


class FilePoolLoader:
    """Load proxy and court lists from text files."""

    def load_pool(self, path: Path) -> list[str]:
        return load_pool(path)

    def load_courts_file(self, path: Path) -> list[str]:
        return load_courts_file(path)
