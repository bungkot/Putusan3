"""Backward-compatible launcher for the legacy proxy scraper CLI."""

import asyncio
import sys

from putusan3.presentation.legacy_cli import logger, main

if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
        sys.exit(0)
    except Exception as exc:
        logger.exception("Fatal error: %s", exc)
        sys.exit(1)
