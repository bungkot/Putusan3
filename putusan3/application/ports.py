"""Interfaces required by application use cases.

Infrastructure adapters implement these contracts; application code never
imports a concrete browser, filesystem, or process adapter.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

ListingValidator = Callable[[str, int], bool]
AsyncJob = Callable[[Any], Awaitable[Any]]


@dataclass(frozen=True)
class FetcherOptions:
    headed: bool
    channel: str | None
    timeout_ms: int
    dead_after: int
    max_attempts: int
    direct: bool


class FetcherPort(Protocol):
    async def get(
        self,
        url: str,
        validator: ListingValidator | None = None,
        label: str = "",
    ) -> tuple[str | None, dict[str, Any]]: ...

    def set_diagnostic_directory(self, path: Path) -> None: ...


class FetcherFactoryPort(Protocol):
    def open(
        self,
        pool: Iterable[str],
        *,
        options: FetcherOptions,
        diagnostic_directory: Path | None = None,
    ) -> AbstractAsyncContextManager[FetcherPort]: ...


class PoolLoaderPort(Protocol):
    def load_pool(self, path: Path) -> list[str]: ...

    def load_courts_file(self, path: Path) -> list[str]: ...


class PageStorePort(Protocol):
    def ensure_directory(self, path: Path) -> None: ...

    def exists(self, path: Path) -> bool: ...

    def remove_file(self, path: Path) -> None: ...

    def is_usable(self, path: Path, min_bytes: int) -> bool: ...

    def read_text(self, path: Path, encoding: str = "utf-8") -> str: ...

    def write_text(self, path: Path, text: str, encoding: str = "utf-8") -> None: ...

    def safe_write(self, path: Path, text: str) -> None: ...

    def append_jsonl(self, path: Path, record: dict[str, Any]) -> None: ...

    def list_files(self, directory: Path, pattern: str) -> list[Path]: ...

    def list_directories(self, directory: Path) -> list[Path]: ...


class CommandRunnerPort(Protocol):
    def run(
        self,
        command: list[str],
        *,
        cwd: Path,
        env: Mapping[str, str] | None = None,
    ) -> int: ...
