"""Concrete infrastructure factories used by the presentation composition root."""
from __future__ import annotations

import subprocess
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from pathlib import Path

from playwright.async_api import async_playwright

from putusan3.application.ports import FetcherOptions, FetcherPort
from putusan3.infrastructure.playwright_engine import Fetcher


class PlaywrightFetcherFactory:
    """Create and manage the lifetime of a Playwright-backed fetcher."""

    @asynccontextmanager
    async def open(
        self,
        pool: Iterable[str],
        *,
        options: FetcherOptions,
        diagnostic_directory: Path | None = None,
    ) -> AsyncIterator[FetcherPort]:
        async with async_playwright() as playwright:
            fetcher = Fetcher(
                playwright,
                list(pool),
                headed=options.headed,
                channel=options.channel,
                timeout_ms=options.timeout_ms,
                dead_after=options.dead_after,
                max_attempts=options.max_attempts,
                direct=options.direct,
            )
            if diagnostic_directory is not None:
                fetcher.set_diagnostic_directory(diagnostic_directory)
            await fetcher.start()
            try:
                yield fetcher
            finally:
                await fetcher.close()


class SubprocessCommandRunner:
    """Process adapter for conductor jobs."""

    def run(self, command: list[str], *, cwd: Path, env=None) -> int:
        return subprocess.call(command, cwd=str(cwd), env=env)
