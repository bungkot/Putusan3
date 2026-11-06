"""Legacy harvest CLI (build_parser / run / main), presentation layer."""
from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path

from putusan3.application.harvest_service import Harvest
from putusan3.application.ports import FetcherOptions
from putusan3.domain.pagination_rules import (  # noqa: E402
    DEFAULT_POOL,
    DEFAULT_YEAR,
    SITE_MAX_PAGES,
    __version__,
)
from putusan3.infrastructure.factories import PlaywrightFetcherFactory
from putusan3.infrastructure.page_store import LocalPageStore
from putusan3.infrastructure.pool_loader import FilePoolLoader

LOG = logging.getLogger("putusan3_harvest")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="putusan3_harvest.py",
        description=(
            "Harvest PDF/ZIP download URLs for one decision year on "
            "putusan3.mahkamahagung.go.id. Saves listing + detail HTML under "
            "data/harvest/<year> by default "
            "and parses URLs offline. Collects URLs only — never downloads the "
            "documents."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--year", type=int, default=DEFAULT_YEAR, help="Decision year (t_put filter)")
    p.add_argument("--out-dir", default=None,
                   help="Output dir for HTML + URLs (default: data/harvest/<year>)")
    p.add_argument("--pool", default=DEFAULT_POOL, help="Proxy list file (ip:port per line)")
    p.add_argument("--direct", action="store_true",
                   help="Try without a proxy first/too (usually 403 from Cloudflare)")
    p.add_argument("--scope", choices=["year", "courts"], default="year",
                   help="'year' = one overall listing (capped at 499 pages); "
                        "'courts' = per-court listings (complete coverage)")
    p.add_argument("--courts-file", default=None,
                   help="Optional text file of court slugs for --scope courts (one per line)")
    p.add_argument("--workers", type=int, default=4, help="Concurrent requests (one proxy each)")
    p.add_argument("--delay-min", type=float, default=1.0, help="Minimum pause between requests (s)")
    p.add_argument("--delay-max", type=float, default=3.0, help="Maximum pause between requests (s)")
    p.add_argument("--timeout", type=int, default=45_000, help="Per-request timeout (ms)")
    p.add_argument("--dead-after", type=int, default=2, help="Consecutive failures before a proxy rests")
    p.add_argument("--max-attempts", type=int, default=0,
                   help="Attempts per URL (0 = auto: 2 x pool size, min 4)")
    p.add_argument("--from-page", type=int, default=1, help="First listing page")
    p.add_argument("--max-pages", type=int, default=0,
                   help=f"Max listing pages per scope (0 = all, site cap {SITE_MAX_PAGES})")
    p.add_argument("--max-details", type=int, default=0, help="Max detail pages (0 = all)")
    p.add_argument("--listing-only", action="store_true", help="Fetch listing pages only; no detail hop")
    p.add_argument("--details-only", action="store_true",
                   help="Fetch missing detail pages from already-saved listings")
    p.add_argument("--parse-only", action="store_true", help="Offline parse of existing HTML only")
    p.add_argument("--no-parse", action="store_true", help="Skip the offline parse at the end")
    p.add_argument("--headed", action="store_true", help="Run the browser visibly")
    p.add_argument("--channel", choices=["chrome", "msedge"], default=None,
                   help="Use an installed browser channel instead of bundled Chromium")
    p.add_argument("--debug", action="store_true", help="Verbose DEBUG logging")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return p


async def run(args: argparse.Namespace) -> int:
    store = LocalPageStore()
    pool_loader = FilePoolLoader()
    fetcher_factory = PlaywrightFetcherFactory()
    harvest = Harvest(args, store=store, pool_loader=pool_loader)

    if args.parse_only:
        parsed = harvest.parse_all()
        paths = harvest.write_results(parsed)
        st = parsed["stats"]
        LOG.info("Offline parse: %s", st)
        if not parsed["pdf"] and not parsed["zip"]:
            print("NO PDF/ZIP LINKS FOUND in existing HTML files.")
            return 3
        print(f"Listing files : {st['listing_files']} ({st['listing_rows']} decisions)")
        print(f"Detail files  : {st['detail_files']}")
        print(f"PDF URLs      : {st['pdf_urls']} -> {paths['pdf_urls']}")
        print(f"ZIP URLs      : {st['zip_urls']} -> {paths['zip_urls']}")
        print(f"Detail URLs   : {st['listing_rows']} -> {paths['detail_urls']}")
        print(f"JSONL records : {len(parsed['details'])} -> {paths['details_jsonl']}")
        print(f"CSV inventory : {st['listing_rows']} rows -> {paths['links_csv']}")
        return 0

    pool = pool_loader.load_pool(Path(args.pool))
    if not pool and not args.direct:
        LOG.error("Empty proxy pool and --direct not given. Nothing to do.")
        return 1

    print("=" * 78)
    print(f" PUTUSAN3 HARVEST — year {args.year} | scope={args.scope} | "
          f"URLs only, HTML in {harvest.out}")
    print(f" out: {harvest.out}")
    print("=" * 78)

    options = FetcherOptions(
        headed=args.headed,
        channel=args.channel,
        timeout_ms=args.timeout,
        dead_after=args.dead_after,
        max_attempts=args.max_attempts,
        direct=args.direct,
    )

    if args.details_only:
        # Stage-2 only: visit the detail pages of decisions already listed on
        # disk. (Previously this flag skipped the fetch phase entirely and
        # silently degraded to --parse-only.)
        async with fetcher_factory.open(
            pool, options=options, diagnostic_directory=harvest.out / "diag"
        ) as fetcher:
            await harvest.fetch_details(fetcher)
    else:
        async with fetcher_factory.open(
            pool, options=options, diagnostic_directory=harvest.out / "diag"
        ) as fetcher:
            if args.scope == "courts":
                await harvest.fetch_courts_scope(fetcher)
            else:
                await harvest.fetch_year_scope(fetcher)
            if args.listing_only:
                LOG.info("--listing-only: skipping detail phase")
            else:
                await harvest.fetch_details(fetcher)

    if args.no_parse:
        print("Fetch phase done (--no-parse). Rerun with --parse-only to extract URLs.")
        return 0

    parsed = harvest.parse_all()
    paths = harvest.write_results(parsed)
    st = parsed["stats"]
    LOG.info("Parse: %s", st)
    print()
    print(f"Listing files : {st['listing_files']} ({st['listing_rows']} decisions)")
    print(f"Detail files  : {st['detail_files']}")
    print(f"PDF URLs      : {st['pdf_urls']} -> {paths['pdf_urls']}")
    print(f"ZIP URLs      : {st['zip_urls']} -> {paths['zip_urls']}")
    print(f"Detail URLs   : {st['listing_rows']} -> {paths['detail_urls']}")
    print(f"JSONL records : {len(parsed['details'])} -> {paths['details_jsonl']}")
    print(f"CSV inventory : {st['listing_rows']} rows -> {paths['links_csv']}")

    if not parsed["pdf"] and not parsed["zip"]:
        print("NO PDF/ZIP LINKS PARSED (empty input or format change).")
        return 3
    if harvest._counter["skipped"]:
        print(f"NOTE: {harvest._counter['skipped']} request(s) skipped — rerun to resume.")
        return 2
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    if args.delay_max < args.delay_min:
        args.delay_max = args.delay_min
    if args.from_page < 1:
        args.from_page = 1
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\nInterrupted — saved HTML stays on disk; rerun to resume.")
        return 130
    except Exception as exc:  # noqa: BLE001
        LOG.exception("Fatal error: %s", exc)
        return 1
