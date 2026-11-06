"""Application composition root for command-line entry points."""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from putusan3.application import conductor_service, harvest_pages_service
from putusan3.infrastructure.factories import (
    PlaywrightFetcherFactory,
    SubprocessCommandRunner,
)
from putusan3.infrastructure.page_store import LocalPageStore
from putusan3.infrastructure.pool_loader import FilePoolLoader

ROOT = Path(__file__).resolve().parents[2]


def pages_main(argv: list[str] | None = None) -> int:
    args = harvest_pages_service.parse_args(argv)
    harvest_pages_service.configure_logging(args.debug)
    try:
        return asyncio.run(harvest_pages_service.run(
            args,
            store=LocalPageStore(),
            pool_loader=FilePoolLoader(),
            fetcher_factory=PlaywrightFetcherFactory(),
        ))
    except KeyboardInterrupt:
        print("\nInterrupted -- saved HTML stays on disk; rerun to resume.")
        return 130


def conductor_main(argv: list[str] | None = None) -> int:
    args = conductor_service.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    try:
        return conductor_service.run(
            args,
            store=LocalPageStore(),
            command_runner=SubprocessCommandRunner(),
            project_root=ROOT,
            environment=os.environ,
        )
    except KeyboardInterrupt:
        print("\nInterrupted — rerun the same command to resume.")
        return 130
