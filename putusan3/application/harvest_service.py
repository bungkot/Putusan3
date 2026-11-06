"""Harvest use-case: full listing+detail pipeline orchestration (offline
parse included). The CLI that drives it lives in the presentation layer
(putusan3.presentation.harvest_cli).
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import io
import json
import logging
import random
import time
from pathlib import Path
from typing import Any

from putusan3.application.async_tasks import run_bounded
from putusan3.application.ports import FetcherPort, PageStorePort, PoolLoaderPort
from putusan3.domain.pagination_rules import (
    BASE,
    ROWS_PER_PAGE,
    SITE_MAX_PAGES,
    court_listing_url,
    is_valid_detail,
    is_valid_listing,
    nomor_from_text,
    page_is_empty,
    parse_courts,
    parse_detail,
    parse_last_page,
    parse_listing,
    parse_nomor,
    year_listing_url,
)

LOG = logging.getLogger("putusan3.harvest")


class Harvest:
    def __init__(
        self,
        args: argparse.Namespace,
        *,
        store: PageStorePort,
        pool_loader: PoolLoaderPort,
    ) -> None:
        self.args = args
        self.store = store
        self.pool_loader = pool_loader
        self.year = args.year
        if args.out_dir:
            self.out = Path(args.out_dir)
        else:
            self.out = Path("data", "harvest", str(args.year))
        self.store.ensure_directory(self.out)
        self.pages_dir = self.out / "pages"
        self.courts_dir = self.out / "courts"
        self.details_dir = self.out / "details"
        for d in (self.pages_dir, self.courts_dir, self.details_dir):
            self.store.ensure_directory(d)
        self.manifest_path = self.out / "manifest.jsonl"
        self.summary_path = self.out / "summary.json"
        self._counter = {"pages": 0, "details": 0, "skipped": 0}

    # -- logging / manifest ------------------------------------------------
    def _log(self, record: dict[str, Any]) -> None:
        self.store.append_jsonl(self.manifest_path, record)

    def _bump(self, field: str) -> int:
        self._counter[field] += 1
        return self._counter[field]

    # -- path helpers ------------------------------------------------------
    def listing_path(self, scope: str, page: int) -> Path:
        """Canonical on-disk name for one listing page: list_NNNN.html."""
        if scope == "year":
            return self.pages_dir / f"list_{page:04d}.html"
        return self.courts_dir / scope / f"list_{page:04d}.html"

    def detail_path(self, kode: str) -> Path:
        return self.details_dir / f"detail_{kode}.html"

    # -- local scans -------------------------------------------------------
    def local_listing_files(self) -> list[Path]:
        files = self.store.list_files(self.pages_dir, "list_*.html")
        for court_dir in self.store.list_directories(self.courts_dir):
            files.extend(self.store.list_files(court_dir, "list_*.html"))
        return sorted(files)

    def local_detail_files(self) -> list[Path]:
        return self.store.list_files(self.details_dir, "detail_*.html")

    def local_rows(self) -> dict[str, dict[str, Any]]:
        """kode -> {kode, url, title, listing_pages} from saved listing HTML."""
        found: dict[str, dict[str, Any]] = {}
        for path in self.local_listing_files():
            try:
                html = self.store.read_text(path)
            except OSError:
                continue
            for kode, url, title in parse_listing(html):
                rec = found.setdefault(kode, {"kode": kode, "url": url,
                                              "title": title, "listing_pages": []})
                if title and not rec["title"]:
                    rec["title"] = title
                rel = str(path.relative_to(self.out)).replace("\\", "/")
                if rel not in rec["listing_pages"]:
                    rec["listing_pages"].append(rel)
        return found

    # -- fetch one listing page -------------------------------------------
    async def _fetch_listing_page(
        self, fetcher: FetcherPort, scope: str, page: int, url: str
    ) -> tuple[int, int, bool]:
        path = self.listing_path(scope, page)
        if self.store.is_usable(path, 5_000):
            try:
                cached = self.store.read_text(path)
            except OSError:
                cached = ""
            if cached and is_valid_listing(cached):
                rows = len(parse_listing(cached))
                LOG.debug("%s page %d already on disk (%d rows)", scope, page, rows)
                return page, rows, True
            LOG.info("%s page %d on disk is not a valid listing (challenge/partial) "
                     "— refetching", scope, page)

        await asyncio.sleep(random.uniform(self.args.delay_min, self.args.delay_max))
        html, meta = await fetcher.get(url, is_valid_listing, label=f"{scope} p{page}")
        if html is None:
            self._log({"kind": "list_page", "scope": scope, "page": page,
                       "skipped": True, **(meta or {})})
            self._bump("skipped")
            return page, 0, False

        row_list = parse_listing(html)
        self.store.safe_write(path, html)
        self._log({"kind": "list_page", "scope": scope, "page": page, "url": url,
                   "rows": len(row_list), "file": str(path), **meta})
        n = self._bump("pages")
        if n % 10 == 0 or page == 1:
            LOG.info("listing pages saved: %d (last: %s p%d, %d rows, %s, %.1fs)",
                     n, scope, page, len(row_list), meta.get("proxy"),
                     meta.get("latency_ms", 0) / 1000)
        if not row_list and page_is_empty(html):
            LOG.info("%s page %d is empty (end of listing)", scope, page)
        return page, len(row_list), True

    # -- phase 1: listing pages -------------------------------------------
    async def fetch_year_scope(self, fetcher: FetcherPort) -> dict[str, Any]:
        a = self.args
        # Page 1 first: its pagination tells us the real last page.
        # (returns page/rows/ok; the page content is read back from disk below)
        _page, _rows, page_one_ok = await self._fetch_listing_page(
            fetcher, "year", 1, year_listing_url(self.year, 1))
        if not page_one_ok:
            LOG.error("Listing page 1 could not be fetched or validated.")
            return {"scope": "year", "ok": False, "last_page": None}

        first_path = self.listing_path("year", 1)
        if not self.store.is_usable(first_path, 5_000):
            LOG.error("Listing page 1 unreachable — cannot discover pagination.")
            return {"scope": "year", "ok": False, "last_page": None}

        first_html = self.store.read_text(first_path)
        if not is_valid_listing(first_html):
            LOG.error("Listing page 1 on disk is not a valid listing.")
            return {"scope": "year", "ok": False, "last_page": None}

        last = parse_last_page(first_html) or 1
        capped = last >= SITE_MAX_PAGES
        start = max(1, a.from_page)
        stop = last
        if a.max_pages:
            stop = min(stop, start + a.max_pages - 1)
        LOG.info("scope=year: pages %d..%d (advertised last=%d%s)",
                 start, stop, last, ", CAPPED at site limit" if capped else "")
        if capped:
            LOG.warning("The overall %d listing is capped at %d pages (~%d decisions). "
                        "Use --scope courts for complete coverage.",
                        self.year, SITE_MAX_PAGES, SITE_MAX_PAGES * ROWS_PER_PAGE)

        pages = list(range(start, stop + 1))
        if 1 in pages:
            pages.remove(1)
        await run_bounded(
            pages,
            lambda p: self._fetch_listing_page(
                fetcher, "year", p, year_listing_url(self.year, p)),
            a.workers,
        )
        return {"scope": "year", "ok": True, "last_page": last, "capped": capped,
                "fetched_upto": stop}

    async def ensure_courts(self, fetcher: FetcherPort) -> list[str]:
        a = self.args
        cache = self.out / "courts.json"
        if self.store.exists(cache):
            try:
                data = json.loads(self.store.read_text(cache))
                slugs = [c["slug"] for c in data.get("courts", []) if c.get("slug")]
                if slugs:
                    LOG.info("Court list from cache: %d courts", len(slugs))
                    return slugs
            except (OSError, json.JSONDecodeError):
                pass

        if a.courts_file:
            slugs = self.pool_loader.load_courts_file(Path(a.courts_file))
            if slugs:
                self.store.safe_write(
                    cache,
                    json.dumps({"source": a.courts_file,
                                "courts": [{"slug": s} for s in slugs]},
                               indent=2, ensure_ascii=False),
                )
                LOG.info("Court list from --courts-file: %d courts", len(slugs))
                return slugs

        index_url = f"{BASE}/pengadilan.html"
        LOG.info("Fetching court index: %s", index_url)
        html, meta = await fetcher.get(index_url, is_valid_listing, label="court index")
        if html is None:
            LOG.error("Court index unreachable: %s", meta)
            return []
        self.store.safe_write(self.out / "courts_index.html", html)
        slugs = parse_courts(html)
        if not slugs:
            # Fall back to whatever courts appear on the year listing page.
            for path in self.local_listing_files():
                slugs.extend(parse_courts(self.store.read_text(path)))
            slugs = list(dict.fromkeys(slugs))
        self.store.write_text(
            cache,
            json.dumps({"source": index_url, "courts": [{"slug": s} for s in slugs]},
                       indent=2, ensure_ascii=False),
        )
        LOG.info("Court list discovered: %d courts", len(slugs))
        return slugs

    async def fetch_courts_scope(self, fetcher: FetcherPort) -> dict[str, Any]:
        a = self.args
        courts = await self.ensure_courts(fetcher)
        if not courts:
            LOG.error("No courts discovered — falling back to scope=year.")
            return await self.fetch_year_scope(fetcher)

        LOG.info("scope=courts: %d courts × year %d", len(courts), self.year)
        # Stage A — page 1 of every court.
        await run_bounded(
            courts,
            lambda slug: self._fetch_listing_page(
                fetcher, slug, 1, court_listing_url(slug, self.year, 1)),
            a.workers,
        )

        # Stage B — remaining pages per court, discovered from its page 1.
        page_jobs: list[tuple[str, int]] = []
        discovered: list[dict[str, Any]] = []
        for slug in courts:
            p1 = self.listing_path(slug, 1)
            if not self.store.is_usable(p1, 5_000):
                discovered.append({"slug": slug, "last_page": None, "ok": False})
                continue
            try:
                html = self.store.read_text(p1)
            except OSError:
                continue
            last = parse_last_page(html) or 1
            start = max(1, a.from_page)
            stop = last
            if a.max_pages:
                stop = min(stop, start + a.max_pages - 1)
            for page in range(max(2, start), stop + 1):
                page_jobs.append((slug, page))
            discovered.append({"slug": slug, "last_page": last,
                               "capped": last >= SITE_MAX_PAGES,
                               "pages": [start, stop], "ok": True})

        LOG.info("scope=courts: %d additional listing pages to fetch", len(page_jobs))
        await run_bounded(
            page_jobs,
            lambda job: self._fetch_listing_page(
                fetcher, job[0], job[1],
                court_listing_url(job[0], self.year, job[1])),
            a.workers,
        )
        self.store.safe_write(
            self.out / "courts.json",
            json.dumps({"source": "pengadilan.html", "year": self.year,
                        "courts": discovered}, indent=2, ensure_ascii=False),
        )
        capped = sum(1 for c in discovered if c.get("capped"))
        if capped:
            LOG.warning("%d court listing(s) also hit the 499-page cap; "
                        "split those by category.", capped)
        return {"scope": "courts", "ok": True, "courts": len(courts),
                "extra_pages": len(page_jobs), "capped_courts": capped}

    # -- phase 2: detail pages ---------------------------------------------
    async def fetch_details(self, fetcher: FetcherPort) -> dict[str, Any]:
        a = self.args
        rows = self.local_rows()
        if not rows:
            LOG.error("No listing rows on disk — run the listing phase first.")
            return {"ok": False, "details": 0}

        missing: list[tuple[str, str]] = []
        for kode, rec in rows.items():
            path = self.detail_path(kode)
            if self.store.is_usable(path, 5_000):
                try:
                    if is_valid_detail(self.store.read_text(path)):
                        continue
                except OSError:
                    pass
            missing.append((kode, rec["url"]))
        if a.max_details:
            missing = missing[: a.max_details]
        LOG.info("decisions in listing: %d | detail pages to fetch: %d",
                 len(rows), len(missing))

        async def job(item: tuple[str, str]) -> None:
            kode, url = item
            await asyncio.sleep(random.uniform(a.delay_min, a.delay_max))
            html, meta = await fetcher.get(url, is_valid_detail, label=f"detail {kode}")
            if html is None:
                self._log({"kind": "detail", "kode": kode, "url": url,
                           "skipped": True, **(meta or {})})
                self._bump("skipped")
                return
            self.store.safe_write(self.detail_path(kode), html)
            self._log({"kind": "detail", "kode": kode, "url": url,
                       "file": str(self.detail_path(kode)), **meta})
            n = self._bump("details")
            if n % 25 == 0:
                LOG.info("detail pages saved: %d/%d (last: %s, %d chars, %s)",
                         n, len(missing), kode, len(html), meta.get("proxy"))

        await run_bounded(missing, job, max(a.workers, 2))
        return {"ok": True, "details": self._counter["details"], "missing": len(missing)}

    # -- phase 3: offline parse -------------------------------------------
    def parse_all(self) -> dict[str, Any]:
        rows = self.local_rows()
        pdf: dict[str, str] = {}
        zipu: dict[str, str] = {}
        details: list[dict[str, Any]] = []

        for kode in sorted(rows):
            rec = rows[kode]
            rec.setdefault("pdf", None)
            rec.setdefault("zip", None)

        for path in self.local_detail_files():
            kode = path.stem.removeprefix("detail_")
            try:
                html = self.store.read_text(path)
            except OSError:
                continue
            rec = rows.setdefault(kode, {"kode": kode,
                                         "url": f"{BASE}/direktori/putusan/{kode}.html",
                                         "title": "", "listing_pages": []})
            for kind, url, fname in parse_detail(html):
                if kind == "pdf":
                    pdf[url] = fname
                elif kind == "zip":
                    zipu[url] = fname
                if not rec.get(kind):
                    rec[kind] = url
                rec[f"{kind}_name"] = fname
            if not rec.get("nomor"):
                rec["nomor"] = parse_nomor(html)

        # Fallback: list judul listing juga membawa "Putusan ... Nomor <num>".
        for rec in rows.values():
            rec.setdefault("nomor", "")
            if not rec["nomor"]:
                rec["nomor"] = nomor_from_text(rec.get("title", ""))

        for kode in sorted(rows):
            rec = rows[kode]
            if rec.get("pdf") or rec.get("zip"):
                details.append(rec)

        stats = {
            "year": self.year,
            "listing_files": len(self.local_listing_files()),
            "listing_rows": len(rows),
            "detail_files": len(self.local_detail_files()),
            "decisions_with_links": len(details),
            "pdf_urls": len(pdf),
            "zip_urls": len(zipu),
        }
        return {"pdf": pdf, "zip": zipu, "details": details,
                "rows": rows, "stats": stats}

    def write_results(self, parsed: dict[str, Any]) -> dict[str, str]:
        pdf, zipu, details, stats = (
            parsed["pdf"], parsed["zip"], parsed["details"], parsed["stats"])
        stamp = time.strftime("%Y-%m-%d %H:%M")
        paths: dict[str, str] = {}

        pdf_path = self.out / f"putusan3_{self.year}_pdf_urls.txt"
        self.store.write_text(pdf_path,
            f"# putusan3.mahkamahagung.go.id — PDF URLs, decision year {self.year} "
            f"(harvested {stamp})\n" + "".join(f"{u}\n" for u in sorted(pdf)),
        )
        paths["pdf_urls"] = str(pdf_path)

        zip_path = self.out / f"putusan3_{self.year}_zip_urls.txt"
        self.store.write_text(zip_path,
            f"# putusan3.mahkamahagung.go.id — ZIP URLs, decision year {self.year} "
            f"(harvested {stamp})\n" + "".join(f"{u}\n" for u in sorted(zipu)),
        )
        paths["zip_urls"] = str(zip_path)

        detail_path = self.out / f"putusan3_{self.year}_detail_urls.txt"
        self.store.write_text(detail_path,
            f"# putusan3.mahkamahagung.go.id — decision detail URLs, year {self.year} "
            f"(harvested {stamp})\n"
            + "".join(f"{parsed['rows'][k]['url']}\n" for k in sorted(parsed["rows"])),
        )
        paths["detail_urls"] = str(detail_path)

        jsonl_path = self.out / f"putusan3_{self.year}_details.jsonl"
        self.store.write_text(
            jsonl_path,
            "".join(json.dumps(rec, ensure_ascii=False) + "\n" for rec in details),
        )
        paths["details_jsonl"] = str(jsonl_path)

        # CSV inventory: nomor, title, url_pdf, url_zip — one row per decision.
        csv_path = self.out / f"putusan3_{self.year}_links.csv"
        buf = io.StringIO()
        writer = csv.writer(buf, lineterminator="\n")  # write_text handles OS EOL
        writer.writerow(["nomor", "title", "url_pdf", "url_zip"])
        for kode in sorted(parsed["rows"]):
            rec = parsed["rows"][kode]
            writer.writerow([
                rec.get("nomor", ""),
                rec.get("title", ""),
                rec.get("pdf", None) or "",
                rec.get("zip", None) or "",
            ])
        self.store.write_text(csv_path, buf.getvalue())
        paths["links_csv"] = str(csv_path)

        paths["summary"] = str(self.summary_path)
        self.store.safe_write(
            self.summary_path, json.dumps(stats, indent=2, ensure_ascii=False))
        return paths
