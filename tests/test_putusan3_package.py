"""Tests for the putusan3 clean-architecture package itself.

Covers the four layers without any network access:
- domain:     parser/URL parity with the legacy scripts/putusan3_harvest shim
- infra:      page_store (safe_write / is_usable / listing_path / manifest_log)
- presentation: `python -m putusan3 pages --help` dispatch + CLI arg parsing
- application:  harvest_pages_service.build_parser / PagesRun offline helpers
"""
from __future__ import annotations

import ast
import asyncio
import csv
import json
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import putusan3.presentation.harvest_cli as harvest_cli  # noqa: E402 (phase-orch tests)
import putusan3.presentation.legacy_cli as legacy_cli  # noqa: E402
from putusan3.application import conductor_service, harvest_pages_service
from putusan3.application.async_tasks import run_bounded
from putusan3.application.harvest_pages_service import (  # noqa: E402
    PagesRun,
    build_parser,
)
from putusan3.application.harvest_service import Harvest
from putusan3.domain import pagination_rules as pr  # noqa: E402
from putusan3.infrastructure import page_store as ps  # noqa: E402
from putusan3.infrastructure.factories import SubprocessCommandRunner
from putusan3.infrastructure.page_store import LocalPageStore
from putusan3.infrastructure.playwright_engine import Fetcher
from putusan3.infrastructure.pool_loader import FilePoolLoader
from putusan3.infrastructure.rotator import ProxyRotator
from putusan3.infrastructure.visitor import ProxyVisitor, ScrapingResult
from putusan3.presentation.harvest_cli import build_parser as build_harvest_parser

# ---------------------------------------------------------------------------
# Shared fixtures: a minimal but realistic direktori listing page
# ---------------------------------------------------------------------------

BASE = pr.BASE

LISTING_HTML = (
    "<html><body><div id='tabs-1'>"
    f'<strong><a href="{BASE}/direktori/putusan/kodeaa01.html">'
    "Putusan Nomor 1/2026</a></strong>"
    f'<strong><a href="{BASE}/direktori/putusan/kodebb02.html">'
    "Putusan Nomor 2/2026</a></strong>"
    "</div><div id='tabs-2'>"
    f'<strong><a href="{BASE}/direktori/putusan/ignoredx.html">'
    "Jangan diambil</a></strong>"
    "</div>"
    '<span data-ci-pagination-page="499">499</span>'
    "</body></html>"
)

CACHED_BYTES = 5_000  # threshold used by service logic for valid cached pages
_big_html = "x" * CACHED_BYTES + LISTING_HTML


# ---------------------------------------------------------------------------
# Domain: pure parsers + URL builders
# ---------------------------------------------------------------------------

def test_listing_url_page1_has_no_page_suffix():
    assert pr.year_listing_url(2026) == (f"{BASE}/direktori/index/tahunjenis/"
                                         "putus/tahun/2026.html")


def test_listing_url_page_n_includes_path_segment():
    assert pr.year_listing_url(2026, 17).endswith(
        "/direktori/index/tahunjenis/putus/tahun/2026/page/17.html")


def test_court_listing_url_roundtrip():
    assert pr.court_listing_url("pn-demo", 2026).endswith("tahun/2026.html")
    assert pr.court_listing_url("pn-demo", 2026, 3).endswith("page/3.html")


def test_parse_listing_drops_side_tabs_and_dedupes():
    rows = pr.parse_listing(LISTING_HTML)
    kodes = [r[0] for r in rows]
    assert kodes == ["kodeaa01", "kodebb02"]  # tabs-2 ("ignoredx") cut off
    assert rows[0][1] == f"{BASE}/direktori/putusan/kodeaa01.html"
    assert rows[0][2] == "Putusan Nomor 1/2026"


def test_parse_last_page_uses_pagination_marker():
    assert pr.parse_last_page(LISTING_HTML) == 499
    assert pr.parse_last_page("<html>x</html>") is None


def test_challenge_markers_and_validity():
    assert pr.is_challenge("<html>Just a moment...</html>")
    assert not pr.is_challenge(LISTING_HTML)
    # too-short or challenge pages are not usable listings
    assert not pr.is_valid_listing("Just a moment", 200)
    assert pr.is_valid_listing(_big_html, 200)
    assert pr.is_valid_listing(_big_html, 403) is False


def test_domain_constants():
    assert pr.SITE_MAX_PAGES == 499
    assert pr.ROWS_PER_PAGE == 20
    # results live in dedicated directories, never at the repo root
    assert pr.DEFAULT_POOL == "data/pools/proxy_pool.txt"


# ---------------------------------------------------------------------------
# Domain parity with the legacy shim (scripts/putusan3_harvest)
# ---------------------------------------------------------------------------

@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_parity_with_legacy_harvest_module():
    legacy = __import__("putusan3_harvest")
    pairs = {
        "year_listing_url": (2026, 2),
        "court_listing_url": ("pn-demo", 2026, 2),
        "ensure_absolute": ("/direktori/putusan/kodeaa01.html",),
        "is_challenge": (LISTING_HTML, 200),
        "parse_listing": (LISTING_HTML,),
        "parse_last_page": (LISTING_HTML,),
        "is_valid_listing": (_big_html, 200),
        "parse_detail": (LISTING_HTML,),
    }
    for fn_name, args in pairs.items():
        legacy_fn = getattr(legacy, fn_name)
        domain_fn = getattr(pr, fn_name)
        assert domain_fn(*args) == legacy_fn(*args), fn_name


# ---------------------------------------------------------------------------
# Infrastructure: page_store
# ---------------------------------------------------------------------------

def test_safe_write_is_atomic_and_leaves_no_part_file(tmp_path: Path):
    target = tmp_path / "pages" / "sub" / "list_0001.html"
    ps.safe_write(target, LISTING_HTML)
    assert target.read_text(encoding="utf-8") == LISTING_HTML
    assert list(tmp_path.rglob("*.part")) == []


def test_safe_write_overwrites_existing(tmp_path: Path):
    target = tmp_path / "a.html"
    ps.safe_write(target, "v1")
    ps.safe_write(target, "v2")
    assert target.read_text(encoding="utf-8") == "v2"


def test_is_usable_threshold(tmp_path: Path):
    target = tmp_path / "a.html"
    target.write_text("x" * 10)
    assert ps.is_usable(target, 10) is True
    assert ps.is_usable(target, 11) is False
    assert ps.is_usable(tmp_path / "missing.html", 0) is False


def test_listing_path_padded_names(tmp_path: Path):
    # The canonical `list_NNNN.html` naming, checked through the services.
    assert PagesRun(build_parser().parse_args(
        ["--out-dir", str(tmp_path / "o")]), LocalPageStore()).page_path(1) \
        .name == "list_0001.html"
    assert PagesRun(build_parser().parse_args(
        ["--out-dir", str(tmp_path / "o")]), LocalPageStore()).page_path(499) \
        .name == "list_0499.html"


def test_manifest_log_appends_timestamped_jsonl(tmp_path: Path):
    mf = tmp_path / "manifest.jsonl"
    ps.manifest_log(mf, {"kind": "page", "page": 1})
    ps.manifest_log(mf, {"kind": "page", "page": 2, "ts": "OVERRIDE"})
    lines = mf.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    rec1, rec2 = (json.loads(x) for x in lines)
    assert rec1["page"] == 1 and len(rec1["ts"]) == 19  # YYYY-mmdd... stamp
    assert rec2["ts"] == "OVERRIDE"  # explicit ts wins


# ---------------------------------------------------------------------------
# Presentation: python -m putusan3 dispatch (no network)
# ---------------------------------------------------------------------------

def test_cli_help_smoke_runs_without_network():
    proc = subprocess.run(
        [sys.executable, "-m", "putusan3", "pages", "--help"],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0
    assert "--year" in proc.stdout and "out-dir" in proc.stdout


def test_cli_conduct_help_smoke_runs_without_network():
    # Regression: conductor_service.main used to take no argv at all, so
    # `python -m putusan3 conduct --help` crashed on the dispatch call.
    proc = subprocess.run(
        [sys.executable, "-m", "putusan3", "conduct", "--help"],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0
    assert "--year" in proc.stdout and "max-rounds" in proc.stdout


def test_cli_usage_when_no_command():
    from putusan3.presentation import cli
    argv_backup = sys.argv
    try:
        sys.argv = ["putusan3"]
        assert cli.main() == 1
    finally:
        sys.argv = argv_backup


def test_cli_help_exits_successfully(capsys):
    from putusan3.presentation.cli import route_command

    assert route_command(["--help"]) == 0
    assert "pages|conduct" in capsys.readouterr().out


def test_cli_routes_pages_and_conduct(monkeypatch):
    calls = []

    import putusan3.presentation.cli as cli

    def fake_pages(argv):
        calls.append(("pages", argv))
        return 3

    def fake_conduct(argv):
        calls.append(("conduct", argv))
        return 4

    monkeypatch.setattr(
        "putusan3.presentation.bootstrap.pages_main", fake_pages)
    monkeypatch.setattr(
        "putusan3.presentation.bootstrap.conductor_main", fake_conduct)
    assert cli.main.__module__  # import sanity

    # directly exercise the routing found in cli.main via its internals:
    # cli reads sys.argv, so set it.
    argv_backup = sys.argv
    try:
        sys.argv = ["putusan3", "pages", "--year", "2026"]
        assert cli.main() == 3
        sys.argv = ["putusan3", "conduct", "--year", "2026"]
        assert cli.main() == 4
    finally:
        sys.argv = argv_backup
    assert calls[0] == ("pages", ["--year", "2026"])
    assert calls[1] == ("conduct", ["--year", "2026"])


def test_cli_routes_harvest(monkeypatch):
    seen: dict = {}

    import putusan3.presentation.cli as cli

    def fake_harvest_main(argv):
        seen["argv"] = argv
        return 9

    monkeypatch.setattr(
        "putusan3.presentation.harvest_cli.main", fake_harvest_main)
    argv_backup = sys.argv
    try:
        sys.argv = ["putusan3", "harvest", "--year", "2022", "--parse-only"]
        assert cli.main() == 9
    finally:
        sys.argv = argv_backup
    assert seen["argv"] == ["--year", "2022", "--parse-only"]


# ---------------------------------------------------------------------------
# Application: arg parsing + offline runners
# ---------------------------------------------------------------------------

def test_build_parser_defaults():
    args = build_parser().parse_args([])
    assert args.year == 2026
    assert args.workers == 4
    assert args.timeout == 45_000
    assert args.max_pages == 0
    assert args.direct is False


def test_default_out_dir_is_data_harvest_year():
    # Results must land in the data/ tree, not the repo root.
    args = build_parser().parse_args([])
    run_ = PagesRun(args, LocalPageStore())
    assert run_.out.as_posix() == "data/harvest/2026"
    assert run_.pages_dir.is_dir()


def test_build_parser_smoke_profile():
    args = build_parser().parse_args(
        ["--year", "2022", "--max-pages", "3", "--direct"])
    assert args.year == 2022 and args.max_pages == 3 and args.direct


def test_pagesrun_paths_and_pagination_writing(tmp_path: Path):
    args = build_parser().parse_args(
        ["--year", "2026", "--out-dir", str(tmp_path / "out")])
    run_ = PagesRun(args, LocalPageStore())
    assert run_.pages_dir.is_dir()
    assert run_.page_path(7) == run_.pages_dir / "list_0007.html"

    run_.first_page_meta = {"proxy": "1.2.3.4:8080"}
    run_.write_pagination(499)
    data = json.loads((run_.out / "pagination.json").read_text(encoding="utf-8"))
    assert data["year"] == 2026
    assert data["last_page"] == 499
    assert data["capped_at_site_limit"] is True
    assert data["first_page_meta"] == {"proxy": "1.2.3.4:8080"}


def test_pagesrun_relates_capped_flag(tmp_path: Path):
    args = build_parser().parse_args(
        ["--year", "2026", "--out-dir", str(tmp_path / "out2")])
    run_ = PagesRun(args, LocalPageStore())
    run_.write_pagination(120)
    data = json.loads((run_.out / "pagination.json").read_text(encoding="utf-8"))
    assert data["capped_at_site_limit"] is False


def test_conductor_expected_last_page_reads_pagination_json(tmp_path: Path):
    store = LocalPageStore()
    # No pagination.json yet -> fall back to the site cap.
    assert conductor_service.expected_last_page(tmp_path, store) == \
        conductor_service.SITE_MAX_PAGES
    # Real pagination discovered on page 1 -> that is the completion target.
    (tmp_path / "pagination.json").write_text(
        json.dumps({"last_page": 50}), encoding="utf-8")
    assert conductor_service.expected_last_page(tmp_path, store) == 50
    # Never above the site cap; tolerate corrupt JSON.
    (tmp_path / "pagination.json").write_text(
        json.dumps({"last_page": 9999}), encoding="utf-8")
    assert conductor_service.expected_last_page(tmp_path, store) == \
        conductor_service.SITE_MAX_PAGES
    (tmp_path / "pagination.json").write_text("{corrupt", encoding="utf-8")
    assert conductor_service.expected_last_page(tmp_path, store) == \
        conductor_service.SITE_MAX_PAGES


@pytest.mark.asyncio
async def test_pagesrun_captures_first_page_meta(tmp_path: Path):
    # Regression: pagination.json's first_page_meta used to stay {} forever.
    args = build_parser().parse_args(
        ["--year", "2026", "--out-dir", str(tmp_path / "out"),
         "--delay-min", "0", "--delay-max", "0"])
    run_ = PagesRun(args, LocalPageStore())

    listing = (
        '<html><body>' + "x" * 4_000 +
        '<strong><a href="https://putusan3.mahkamahagung.go.id/direktori/'
        'putusan/aa2026.html">Putusan uji</a></strong>'
        '<a data-ci-pagination-page="3" href="#">3</a></body></html>'
    )

    class FakeFetcher:
        async def get(self, _url, _validator=None, label=""):
            return listing, {"proxy": "9.9.9.9:8080", "status": 200}

    assert await run_.fetch_page(FakeFetcher(), 1) is True
    run_.write_pagination(3)
    data = json.loads((run_.out / "pagination.json").read_text(encoding="utf-8"))
    assert data["first_page_meta"]["proxy"] == "9.9.9.9:8080"
    assert data["first_page_meta"]["status"] == 200


@pytest.mark.asyncio
async def test_fetcher_leases_preferred_proxy_exclusively():
    fetcher = Fetcher(
        None, ["1.2.3.4:8080", "5.6.7.8:8080"],
        headed=False, channel=None, timeout_ms=1000, dead_after=2,
    )
    first = await fetcher._lease()
    assert first == "1.2.3.4:8080"
    fetcher._preferred = first

    second = await fetcher._lease()
    assert second == "5.6.7.8:8080"
    assert second != first

    await fetcher._release(first)
    await fetcher._release(second)
    assert await fetcher._lease() == first


@pytest.mark.asyncio
async def test_fetcher_parses_proxy_credentials_for_playwright():
    captured = {}

    class FakeContext:
        async def add_init_script(self, _script):
            pass

    class FakeBrowser:
        async def new_context(self, **kwargs):
            captured.update(kwargs)
            return FakeContext()

    fetcher = Fetcher(
        None, ["user:pass@1.2.3.4:8080"],
        headed=False, channel=None, timeout_ms=1000, dead_after=2,
    )
    fetcher._browser = FakeBrowser()

    await fetcher._context("user:pass@1.2.3.4:8080")

    assert captured["proxy"] == {
        "server": "http://1.2.3.4:8080",
        "bypass": "localhost,127.0.0.1",
        "username": "user",
        "password": "pass",
    }


@pytest.mark.asyncio
async def test_proxy_visitor_serializes_visits_per_proxy(monkeypatch):
    visitor = ProxyVisitor(ProxyRotator())
    active = 0
    max_active = 0

    async def fake_visit(_url, proxy, _wait_for, _extract_fn):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.01)
        active -= 1
        return ScrapingResult(url="https://example.com", proxy=proxy, success=True)

    monkeypatch.setattr(visitor, "_visit_with_proxy", fake_visit)
    await asyncio.gather(*(
        visitor.visit("https://example.com", proxy="1.2.3.4:8080")
        for _ in range(3)
    ))
    assert max_active == 1


@pytest.mark.asyncio
async def test_proxy_visitor_propagates_task_cancellation(monkeypatch):
    visitor = ProxyVisitor(ProxyRotator())

    async def cancelled(*_args, **_kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr(visitor, "visit_with_retry", cancelled)
    with pytest.raises(asyncio.CancelledError):
        await visitor.visit_multiple(["https://example.com"])


@pytest.mark.asyncio
async def test_harvest_does_not_trust_stale_invalid_page_one(tmp_path: Path):
    args = build_harvest_parser().parse_args(
        ["--year", "2022", "--out-dir", str(tmp_path),
         "--delay-min", "0", "--delay-max", "0"])
    harvest = Harvest(args, store=LocalPageStore(), pool_loader=FilePoolLoader())
    page_one = harvest.listing_path("year", 1)
    page_one.write_text("x" * 5_000, encoding="utf-8")

    class FailedFetcher:
        async def get(self, *_args, **_kwargs):
            return None, {"error": "unreachable"}

    result = await harvest.fetch_year_scope(FailedFetcher())
    assert result == {"scope": "year", "ok": False, "last_page": None}


_BASE = pr.BASE

_LISTING_CSV_HTML = (
    "<html><body>"
    f'<strong><a href="{_BASE}/direktori/putusan/aa22csv1.html">'
    "Putusan PN DENPASAR Nomor 1208/Pdt.G/2022/PN Dps</a></strong>"
    f'<strong><a href="{_BASE}/direktori/putusan/aa22csv2.html">'
    "Putusan PT DKI Nomor 77/PID.B/2022</a></strong>"
    "</body></html>"
)

# Detail page: known download-link markup + nomor statement in the body.
_DETAIL_CSV_HTML = (
    '<html><body><h1>Putusan</h1>'
    '<h2>Nomor 99/PID.BA/2022</h2>'
    '<a href="' + _BASE + '/direktori/download_file/0d64b6103d0f5832d6e382bf/'
    'pdf/aa22csv1">surat_2022.pdf</a>'
    '<a href="' + _BASE + '/direktori/download_file/0d64b6103d0f5832d6e382bf/'
    'zip/aa22csv1">lampiran_2022.zip</a>'
    "</body></html>"
)


def test_parse_nomor_extracts_decision_number():
    assert pr.parse_nomor(
        "<b>Putusan</b> MAHKAMAH AGUNG Nomor 7598 K/PID.SUS/2026"
    ) == "7598 K/PID.SUS/2026"
    assert pr.parse_nomor(
        "Putusan PN DENPASAR Nomor 1208/Pdt.G/2022/PN Dps"
    ) == "1208/Pdt.G/2022/PN Dps"
    assert pr.parse_nomor("Nomor Putusan 45/K/Bia/1990 tentang sesuatu") == "45/K/Bia/1990"
    assert pr.parse_nomor("tanpa nomor sama sekali") == ""


# ---------------------------------------------------------------------------
# CLI phase orchestration: --details-only must actually fetch detail pages
# (regression: it used to skip the fetch phase and silently degrade to
# --parse-only), while --listing-only must not.
# ---------------------------------------------------------------------------


class _FakeCM:
    def __init__(self, fetcher):
        self._fetcher = fetcher

    async def __aenter__(self):
        return self._fetcher

    async def __aexit__(self, *_exc):
        return False


ParseCounter = {"fetch_details": 0, "year_scope": 0}


def _fake_harvest_factory(calls: dict):
    class FakeHarvest:
        def __init__(self, args, *, store, pool_loader):
            self.args = args
            self.out = store.out if hasattr(store, "out") else Path(args.out_dir)
            self._counter = {"skipped": 0}

        async def fetch_details(self, fetcher):
            calls["fetch_details"] = calls.get("fetch_details", 0) + 1
            calls["fetched_with"] = fetcher

        async def fetch_year_scope(self, fetcher):
            calls["year_scope"] = calls.get("year_scope", 0) + 1
            if self.args.listing_only:
                return
            await self.fetch_details(fetcher)

        def parse_all(self):
            return {"pdf": {}, "zip": {}, "details": [], "rows": {},
                    "stats": {"year": 2026, "listing_files": 0, "listing_rows": 0,
                              "detail_files": 0, "decisions_with_links": 0,
                              "pdf_urls": 0, "zip_urls": 0}}

        def write_results(self, _parsed):
            return {"pdf_urls": "", "zip_urls": "", "detail_urls": "",
                    "details_jsonl": "", "links_csv": "", "summary": ""}

    return FakeHarvest


def test_details_only_actually_fetches_details(monkeypatch, tmp_path):
    calls: dict = {}
    fetcher = object()
    monkeypatch.setattr(harvest_cli, "Harvest", _fake_harvest_factory(calls))
    monkeypatch.setattr(
        harvest_cli, "PlaywrightFetcherFactory",
        lambda: type("F", (), {"open": staticmethod(
            lambda pool, *, options, diagnostic_directory=None: _FakeCM(fetcher))})())

    # CI has no data/pools/proxy_pool.txt (gitignored) — always pass an
    # explicit pool so the run doesn't bail out before the fake fetcher.
    pool_file = tmp_path / "pool.txt"
    pool_file.write_text("# fake\n203.0.113.7:8080\n", encoding="utf-8")
    args = harvest_cli.build_parser().parse_args(
        ["--year", "2022", "--out-dir", str(tmp_path), "--details-only",
         "--pool", str(pool_file)])
    rc = asyncio.run(harvest_cli.run(args))

    assert rc == 3                      # empty parse, but fetch happened
    assert calls["fetch_details"] == 1
    assert calls["fetched_with"] is fetcher


def test_listing_only_skips_detail_phase(monkeypatch, tmp_path):
    calls: dict = {}
    monkeypatch.setattr(harvest_cli, "Harvest", _fake_harvest_factory(calls))
    monkeypatch.setattr(
        harvest_cli, "PlaywrightFetcherFactory",
        lambda: type("F", (), {"open": staticmethod(
            lambda pool, *, options, diagnostic_directory=None: _FakeCM(object()))})())

    pool_file = tmp_path / "pool.txt"
    pool_file.write_text("# fake\n203.0.113.7:8080\n", encoding="utf-8")
    args = harvest_cli.build_parser().parse_args(
        ["--year", "2022", "--out-dir", str(tmp_path), "--listing-only",
         "--pool", str(pool_file)])
    asyncio.run(harvest_cli.run(args))

    assert calls["year_scope"] == 1     # listing still fetched
    assert "fetch_details" not in calls  # detail phase skipped


def test_write_results_links_csv(tmp_path: Path):
    """CSV inventory: columns nomor,title,url_pdf,url_zip, one row per decision

    (kode with a detail page: nomor from the detail body + pdf/zip URLs;
    kode without: nomor falls back to the listing title, empty links)."""
    args = build_harvest_parser().parse_args(
        ["--year", "2022", "--out-dir", str(tmp_path)])
    harvest = Harvest(args, store=LocalPageStore(), pool_loader=FilePoolLoader())
    (harvest.pages_dir / "list_0001.html").write_text(
        _LISTING_CSV_HTML, encoding="utf-8")
    (harvest.details_dir / "detail_aa22csv1.html").write_text(
        _DETAIL_CSV_HTML, encoding="utf-8")

    parsed = harvest.parse_all()
    paths = harvest.write_results(parsed)

    with open(paths["links_csv"], encoding="utf-8", newline="") as fh:
        rows = list(csv.reader(fh))
    assert rows[0] == ["nomor", "title", "url_pdf", "url_zip"]
    # Descending kode sort: aa22csv1, then aa22csv2
    assert rows[1][0] == "99/PID.BA/2022"          # from the detail body
    assert rows[1][1] == "Putusan PN DENPASAR Nomor 1208/Pdt.G/2022/PN Dps"
    assert rows[1][2].endswith("/direktori/download_file/"
                               "0d64b6103d0f5832d6e382bf/pdf/aa22csv1")
    assert rows[1][3].endswith("/direktori/download_file/"
                               "0d64b6103d0f5832d6e382bf/zip/aa22csv1")
    assert rows[2][0] == "77/PID.B/2022"            # fallback: listing title
    assert rows[2][2] == "" and rows[2][3] == ""


def test_conductor_only_reports_complete_valid_pages(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(conductor_service, "SITE_MAX_PAGES", 2)
    pages_dir = tmp_path / "pages"
    pages_dir.mkdir()
    (pages_dir / "list_0001.html").write_text(_big_html, encoding="utf-8")
    (pages_dir / "list_backup.html").write_text(_big_html, encoding="utf-8")
    store = LocalPageStore()
    assert conductor_service.count_pages(tmp_path, store) == 1
    assert not conductor_service.pages_complete(tmp_path, store)

    (pages_dir / "list_0002.html").write_text(_big_html, encoding="utf-8")
    assert conductor_service.pages_complete(tmp_path, store)


def test_config_source_fetch_timeout_defaults_to_validation_timeout(monkeypatch):
    # Backlog closed: the proxy-source fetch no longer borrows VALIDATION_TIMEOUT
    # implicitly; SOURCE_FETCH_TIMEOUT (seconds) defaults to that same value but
    # can be tuned independently.
    #
    # Config freezes os.environ at import time, and CI exports
    # SOURCE_FETCH_TIMEOUT explicitly — values already read at collection.
    # So simulate fresh imports under controlled env instead of trusting
    # the ambient module state.
    import importlib

    from putusan3 import config as _config_module
    try:
        with monkeypatch.context() as ctx:
            # Fresh import without SOURCE_FETCH_TIMEOUT → falls back to
            # VALIDATION_TIMEOUT/ms → seconds.
            ctx.delenv("SOURCE_FETCH_TIMEOUT", raising=False)
            fresh = importlib.reload(_config_module)
            assert (fresh.Config.SOURCE_FETCH_TIMEOUT
                    == fresh.Config.VALIDATION_TIMEOUT / 1000.0)
            assert fresh.Config.SOURCE_FETCH_TIMEOUT > 0
            # Explicitly set → tuned independently of VALIDATION_TIMEOUT.
            ctx.setenv("SOURCE_FETCH_TIMEOUT", "7.5")
            fresh = importlib.reload(_config_module)
            assert fresh.Config.SOURCE_FETCH_TIMEOUT == 7.5
    finally:
        # Env is restored; re-evaluate the module one last time so it is
        # frozen again for the ambient environment (real env, .env, CI).
        importlib.reload(_config_module)


def test_application_layer_does_not_import_infrastructure():
    application_dir = Path(__file__).resolve().parents[1] / "putusan3" / "application"
    for source in application_dir.rglob("*.py"):
        tree = ast.parse(source.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert not (node.module or "").startswith("putusan3.infrastructure"), source
            elif isinstance(node, ast.Import):
                assert all(not alias.name.startswith("putusan3.infrastructure")
                           for alias in node.names), source


@pytest.mark.asyncio
async def test_pages_use_case_runs_with_injected_ports(tmp_path):
    class FakePoolLoader:
        def load_pool(self, _path):
            return ["1.2.3.4:8080"]

        def load_courts_file(self, _path):
            return []

    class FakeFetcher:
        async def get(self, _url, _validator, label=""):
            return _big_html, {"proxy": "1.2.3.4:8080", "label": label}

    class FakeFetcherFactory:
        @asynccontextmanager
        async def open(self, _pool, *, options, diagnostic_directory=None):
            assert options.timeout_ms == 45_000
            assert diagnostic_directory == tmp_path / "out" / "diag"
            yield FakeFetcher()

    args = build_parser().parse_args([
        "--year", "2026",
        "--pool", str(tmp_path / "pool.txt"),
        "--out-dir", str(tmp_path / "out"),
        "--max-pages", "1",
        "--delay-min", "0",
        "--delay-max", "0",
    ])
    code = await harvest_pages_service.run(
        args,
        store=LocalPageStore(),
        pool_loader=FakePoolLoader(),
        fetcher_factory=FakeFetcherFactory(),
    )
    assert code == 0
    assert (tmp_path / "out" / "pages" / "list_0001.html").is_file()
    assert (tmp_path / "out" / "pagination.json").is_file()


def test_conductor_uses_injected_runner_and_absolute_refresh_path(tmp_path, monkeypatch):
    monkeypatch.setattr(conductor_service, "SITE_MAX_PAGES", 2)

    class FakeRunner:
        commands = []

        def run(self, command, *, cwd, env=None):
            self.commands.append((command, cwd, env))
            if command[1].endswith("main.py"):
                Path(command[-1]).write_text("# fresh\n8.8.8.8:8080\n", encoding="utf-8")
                return 0
            return 1

    args = conductor_service.parse_args([
        "--year", "2026", "--max-rounds", "1", "--test-n", "1",
        "--test-conc", "1", "--pool", "data/pools/main.txt",
        "--out-dir", "data/harvest/2026",
    ])
    runner = FakeRunner()
    result = conductor_service.run(
        args,
        store=LocalPageStore(),
        command_runner=runner,
        project_root=tmp_path,
        environment={},
    )
    refresh_command = runner.commands[1][0]
    refresh_target = Path(refresh_command[-1])

    assert result == 2
    assert refresh_target.is_absolute()
    assert refresh_target.parent == tmp_path / "data" / "pools"
    assert "8.8.8.8:8080" in (tmp_path / "data" / "pools" / "main.txt").read_text(
        encoding="utf-8")


# ---------------------------------------------------------------------------
# Untouched-module regressions (QA harness audit 2026-11-07):
# run_bounded / SubprocessCommandRunner / legacy_cli had zero direct tests.
# ---------------------------------------------------------------------------

def test_run_bounded_processes_every_item():
    """run_bounded must feed all queued items to the job (worker pool > 1)."""
    seen: list[int] = []

    async def job(item: int) -> None:
        seen.append(item)

    asyncio.run(run_bounded(range(25), job, 4))
    assert sorted(seen) == list(range(25))


def test_run_bounded_swallows_job_failures(caplog):
    """A failing job must not cancel the remaining queue or raise."""
    seen: list[int] = []

    async def job(item: int) -> None:
        if item == 1:
            raise RuntimeError("boom")
        seen.append(item)

    asyncio.run(run_bounded([0, 1, 2, 3], job, 2))
    assert seen == [0, 2, 3]
    assert any("boom" in rec.getMessage() or rec.exc_info for rec in caplog.records)


def test_run_bounded_floor_workers_to_one():
    """workers < 1 must still execute the queue (gather of ≥1 worker)."""
    seen: list[int] = []

    async def job(item: int) -> None:
        seen.append(item)

    asyncio.run(run_bounded(["a"], job, 0))
    assert seen == ["a"]


def test_subprocess_command_runner_returns_exit_code(tmp_path):
    """The process adapter maps subprocess.call verbatim (0 on success)."""
    runner = SubprocessCommandRunner()
    rc = runner.run(
        [sys.executable, "-c", "print(1)"],
        cwd=tmp_path,
    )
    assert rc == 0
    rc_fail = runner.run(
        [sys.executable, "-c", "raise SystemExit(3)"],
        cwd=tmp_path,
    )
    assert rc_fail == 3


def test_legacy_cli_load_command_offline(tmp_path, capsys, monkeypatch):
    """`python main.py load FILE` fills the pool from text with no network."""
    pool_file = tmp_path / "pool.txt"
    pool_file.write_text("# header\n8.8.8.8:8080\n1.2.3.4:9090\n", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["main.py", "load", str(pool_file)])
    monkeypatch.delenv("PROXY_SAVE_PATH", raising=False)

    exit_code = asyncio.run(legacy_cli.main())

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "Loaded 2 proxies from" in out


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
