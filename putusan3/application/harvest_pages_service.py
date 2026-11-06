"""Stage-1 use-case: save every listing page of one decision year as HTML.

Loads the Indonesian proxy pool from text, reads the real pagination from
page 1 (`data-ci-pagination-page`), fetches pages 1..last through the
rotating pool, writes pages/list_NNNN.html + pagination.json. Resumable:
valid cached pages are skipped.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import random
from pathlib import Path
from typing import Any

from putusan3.application.async_tasks import run_bounded
from putusan3.application.ports import (
    FetcherFactoryPort,
    FetcherOptions,
    FetcherPort,
    PageStorePort,
    PoolLoaderPort,
)
from putusan3.domain.pagination_rules import (
    DEFAULT_POOL,
    DEFAULT_YEAR,
    is_valid_listing,
    parse_last_page,
    parse_listing,
    year_listing_url,
)

LOG = logging.getLogger("putusan3_pages")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Stage-1: save listing pages of one year as HTML")
    p.add_argument("--year", type=int, default=DEFAULT_YEAR, help="Decision year")
    p.add_argument("--pool", default=DEFAULT_POOL, help="Proxy text file (ip:port per line)")
    p.add_argument("--out-dir", default=None,
                   help="Output folder (default: data/harvest/<year>)")
    p.add_argument("--workers", type=int, default=4, help="Concurrent fetchers (one proxy each)")
    p.add_argument("--delay-min", type=float, default=1.0, help="Min pause between requests (s)")
    p.add_argument("--delay-max", type=float, default=3.0, help="Max pause between requests (s)")
    p.add_argument("--timeout", type=int, default=45_000, help="Per-request timeout (ms)")
    p.add_argument("--dead-after", type=int, default=2,
                   help="Consecutive failures before a proxy rests")
    p.add_argument("--max-attempts", type=int, default=0,
                   help="Attempts per URL (0 = auto: 2 x pool size, min 4)")
    p.add_argument("--direct", action="store_true", help="Also try without a proxy")
    p.add_argument("--from-page", type=int, default=1, help="First listing page")
    p.add_argument("--max-pages", type=int, default=0,
                   help="Max pages (0 = all; smoke test: --max-pages 3)")
    p.add_argument("--headed", action="store_true", help="Run the browser visibly")
    p.add_argument("--channel", choices=["chrome", "msedge"], default=None,
                   help="Installed browser channel instead of bundled Chromium")
    p.add_argument("--debug", action="store_true", help="Verbose logging")
    return p


class PagesRun:
    """Stage-1 run: fetch listing pages of one year and record pagination."""

    def __init__(self, args: argparse.Namespace, store: PageStorePort) -> None:
        self.args = args
        self.store = store
        self.year = args.year
        self.out = Path(args.out_dir) if args.out_dir else Path("data", "harvest", str(args.year))
        self.pages_dir = self.out / "pages"
        self.store.ensure_directory(self.pages_dir)
        self.manifest = self.out / "manifest.jsonl"
        self._skipped = 0
        self._fetched = 0
        # meta of the page-1 fetch (proxy/status/latency), surfaced in pagination.json
        self.first_page_meta: dict[str, Any] = {}

    def page_path(self, page: int) -> Path:
        """Canonical on-disk name for one listing page: list_NNNN.html."""
        return self.pages_dir / f"list_{page:04d}.html"

    def _log(self, record: dict[str, Any]) -> None:
        self.store.append_jsonl(self.manifest, record)

    async def fetch_page(self, fetcher: FetcherPort, page: int) -> bool:
        """Fetch one listing page (with disk cache); False when unreachable."""
        path = self.page_path(page)
        url = year_listing_url(self.year, page)

        if self.store.is_usable(path, 5_000):
            try:
                cached = self.store.read_text(path)
                if cached and is_valid_listing(cached):
                    LOG.info("page %d already on disk (%d rows) -- skip",
                             page, len(parse_listing(cached)))
                    self._log({"kind": "page", "page": page, "skipped": True,
                               "reason": "cached", "file": str(path)})
                    if page == 1:
                        self.first_page_meta = {"skipped": True, "reason": "cached"}
                    return True
            except OSError:
                pass
            LOG.info("page %d on disk is not a valid listing -- refetching", page)

        await asyncio.sleep(random.uniform(self.args.delay_min, self.args.delay_max))
        html, meta = await fetcher.get(url, is_valid_listing, label=f"p{page}")
        if page == 1:
            self.first_page_meta = dict(meta or {})
        if html is None:
            LOG.error("page %d UNREACHABLE after all attempts: %s", page, meta)
            self._log({"kind": "page", "page": page, "skipped": True, "url": url, **(meta or {})})
            self._skipped += 1
            return False

        self.store.safe_write(path, html)
        rows = parse_listing(html)
        last = parse_last_page(html)
        self._fetched += 1
        self._log({"kind": "page", "page": page, "url": url, "rows": len(rows),
                   "pagination_last_hint": last, "file": str(path), **meta})
        LOG.info("page %d saved (%d rows, last-on-page hint: %s, %d chars, %s)",
                 page, len(rows), last, len(html), meta.get("proxy"))
        return True

    def write_pagination(self, last_page) -> Path:
        on_disk = self.store.list_files(self.pages_dir, "list_*.html")
        data = {
            "year": self.year,
            "base_url": year_listing_url(self.year, 1),
            "last_page": last_page,
            "capped_at_site_limit": bool(last_page and last_page >= 499),
            "pages_on_disk": [str(p.name) for p in on_disk],
            "pages_fetched_this_run": self._fetched,
            "pages_skipped_this_run": self._skipped,
            "first_page_meta": dict(self.first_page_meta),
        }
        path = self.out / "pagination.json"
        # Safe/atomic write: this file is read by the conductor in another
        # process for its completion target; a torn write would break it.
        self.store.safe_write(path, json.dumps(data, indent=2, ensure_ascii=False))
        return path


async def run(
    args: argparse.Namespace,
    *,
    store: PageStorePort,
    pool_loader: PoolLoaderPort,
    fetcher_factory: FetcherFactoryPort,
) -> int:
    pool = pool_loader.load_pool(Path(args.pool))
    if not pool and not args.direct:
        LOG.error("Proxy pool empty and --direct not given. Nothing to do.")
        return 1

    run_ = PagesRun(args, store)
    print("=" * 78)
    print(f" PUTUSAN3 PAGES -- year {args.year} -> {run_.out} (HTML listing pages only)")
    print("=" * 78)

    last_page = None
    options = FetcherOptions(
        headed=args.headed,
        channel=args.channel,
        timeout_ms=args.timeout,
        dead_after=args.dead_after,
        max_attempts=args.max_attempts,
        direct=args.direct,
    )
    async with fetcher_factory.open(
        pool, options=options, diagnostic_directory=run_.out / "diag"
    ) as fetcher:
        # Page 1 first: its pagination widget tells the real last page.
        if not await run_.fetch_page(fetcher, 1):
            LOG.error("Listing page 1 unreachable -- cannot conclude pagination.")
            run_.write_pagination(None)
            return 1
        p1 = run_.page_path(1)
        first_html = store.read_text(p1)
        last_page = parse_last_page(first_html) or 1
        capped = last_page >= 499
        LOG.info("PAGINATION: year %d => %d page(s)%s", args.year, last_page,
                 " (CAPPED at site limit)" if capped else "")

        # Remaining pages 2..last, bounded by --from-page/--max-pages.
        start = max(1, args.from_page)
        stop = last_page
        if args.max_pages:
            stop = min(stop, start + args.max_pages - 1)
        todo = [p for p in range(start, stop + 1) if p != 1]
        if todo:
            LOG.info("fetching pages %d..%d (%d more)", start, stop, len(todo))
        await run_bounded(todo, lambda p: run_.fetch_page(fetcher, p), args.workers)

    path = run_.write_pagination(last_page)
    print(f"\nPAGINATION: {last_page} page(s) -- summary: {path}")
    print(f"HTML files in {run_.pages_dir}: "
          f"{len(store.list_files(run_.pages_dir, 'list_*.html'))}")
    if run_._skipped:
        print(f"NOTE: {run_._skipped} page(s) skipped -- rerun to resume.")
        return 2
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    return build_parser().parse_args(argv)


def configure_logging(debug: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S",
    )
