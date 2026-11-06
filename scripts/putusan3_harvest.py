#!/usr/bin/env python3
"""
putusan3_harvest.py — legacy entry point (thin compatibility shim).
=====================================================================
Logic lives in the putusan3 package now:
  * pure rules & parsers      -> putusan3.domain.pagination_rules
  * storage helpers           -> putusan3.infrastructure.page_store
  * playwright engine + pool  -> putusan3.infrastructure.playwright_engine
  * Harvest orchestration     -> putusan3.application.harvest_service
  * CLI (build_parser/run/main) -> putusan3.presentation.harvest_cli
This shim re-exports the full historical import surface so existing
tests (`tests/test_harvest.py`) and one-off scripts keep working.
"""
from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

from putusan3.application.async_tasks import run_bounded as run_pool  # noqa: E402,F401
from putusan3.application.harvest_service import Harvest as _Harvest  # noqa: E402
from putusan3.domain.pagination_rules import (  # noqa: E402,F401
    BASE,
    CHALLENGE_MARKERS,
    CHROME_UA,
    COURT_LINK_RE,
    DEFAULT_POOL,
    DEFAULT_YEAR,
    DIRECT_KEY,
    DOWNLOAD_RE,
    EMPTY_MARKERS,
    PAGE_HREF_RE,
    PAGE_LINK_RE,
    ROW_DETAIL_RE,
    ROWS_PER_PAGE,
    SITE_MAX_PAGES,
    TAG_RE,
    VIEWPORTS,
    court_listing_url,
    ensure_absolute,
    is_challenge,
    is_valid_detail,
    is_valid_listing,
    main_region,
    page_is_empty,
    parse_courts,
    parse_detail,
    parse_last_page,
    parse_listing,
    year_listing_url,
)
from putusan3.infrastructure.page_store import (  # noqa: E402,F401
    LocalPageStore,
    is_usable,
    safe_write,
)
from putusan3.infrastructure.playwright_engine import (  # noqa: E402,F401
    Fetcher,
    load_courts_file,
    load_pool,
)
from putusan3.infrastructure.pool_loader import FilePoolLoader  # noqa: E402
from putusan3.presentation.harvest_cli import (  # noqa: E402,F401
    build_parser,
    main,
    run,
)


class Harvest(_Harvest):
    """Compatibility constructor for callers of the historical shim."""

    def __init__(self, args):
        super().__init__(args, store=LocalPageStore(), pool_loader=FilePoolLoader())


if __name__ == "__main__":
    _sys.exit(main())
