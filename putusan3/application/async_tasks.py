"""Small bounded task runner used by application use cases."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from typing import Any

from putusan3.application.ports import AsyncJob

LOG = logging.getLogger("putusan3.application")


async def run_bounded(
    items: Iterable[Any],
    job: AsyncJob,
    workers: int,
) -> None:
    """Run jobs concurrently while keeping per-job failures visible in logs."""
    queue: asyncio.Queue[Any] = asyncio.Queue()
    for item in items:
        queue.put_nowait(item)

    async def worker() -> None:
        while True:
            try:
                item = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            try:
                await job(item)
            except asyncio.CancelledError:
                raise
            except Exception:
                LOG.exception("Application worker failed for item %r", item)

    await asyncio.gather(*(worker() for _ in range(max(1, workers))))
