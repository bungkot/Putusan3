# AGENTS.md — Python Playwright + Rolling Indonesian Proxy Scraper / Putusan3 Harvest

## Project Overview
Python application using Playwright to visit websites through rotating Indonesian proxies sourced from free proxy lists. **Status: fully implemented and live-verified.** Target application: https://putusan3.mahkamahagung.go.id which sits behind **Cloudflare** — plain HTTP clients get a 403 challenge; Chromium with a Chrome user agent passes without any bot framework.

Two generations of code coexist cleanly (2026-10-25 restructure):
1. **Legacy scraper** (`main.py` + shims `proxy/*`, `scraper/*`) — generic proxy pool tools.
2. **`putusan3/` package (clean architecture, Ports & Adapters)** — the real harvest workflow: all logic now lives here; legacy paths are thin re-export shims.

See `docs/STATE.md` for the verified state snapshot and `docs/ARCHITECTURE.md` for internals.

## Quick Start
```bash
# Install dependencies
pip install -r requirements-dev.txt
playwright install chromium

# Restructure complete: logic now lives in putusan3/{domain,infrastructure,application,presentation}
# All legacy entry points still work (shim re-export).

# Stage 1: collect Indonesian proxies → text pool snapshot
python main.py save data/pools/proxy_pool.txt

# Stage-1 workflow (recommended): conductor cycle [harvest → refresh-pool → merge → resume]
# default: pool data/pools/proxy_pool.txt, output data/harvest/<year>/ — explicit paths optional
python scripts/putusan3_conductor.py --year 2026

# Modern alternative entry points
python -m putusan3 pages --year 2026
python -m putusan3 conduct --year 2026
```

## Key Commands
| Task | Command |
|------|---------|
| Install deps (dev) | `pip install -r requirements-dev.txt` |
| Install deps (runtime) | `pip install -r requirements.txt` |
| Install browsers | `playwright install chromium` |
| **Harvest full-year pages (conductor, resumable)** | `python scripts/putusan3_conductor.py --year 2026 --pool data/pools/proxy_pool.txt --out-dir data/harvest/2026` |
| Harvest pages (single pass) | `python scripts/putusan3_pages.py --year 2026 --workers 3 --timeout 20000 --max-attempts 24` |
| **Stage-2 detail (resumable, detached-ok)** | `python scripts/putusan3_harvest.py --year 2026 --details-only --workers 4` |
| Parse offline → CSV inventory | `python scripts/putusan3_harvest.py --year 2026 --parse-only` |
| Harvest via modern CLI | `python -m putusan3 pages\|conduct --year 2026 ...` |
| Smoke test 2 pages | `python scripts/putusan3_pages.py --year 2026 --max-pages 2` |
| Full harvest legacy CLI (listing+detail) | `python scripts/putusan3_harvest.py --year 2022 --scope courts` |
| Save pool snapshot | `python main.py save data/pools/proxy_pool.txt` |
| Load pool from text | `python main.py load data/pools/proxy_pool.txt` |
| Restore pool + scrape (instant) | `PROXY_LOAD_PATH=data/pools/proxy_pool.txt python main.py test-all` |
| Run legacy generic scraper | `python main.py` |
| Check conductor progress | `(Get-ChildItem data/harvest/2026/pages -Filter list_*.html).Count` + `Get-Content logs/conductor_run.log -Tail 5` |
| Tests | `python -m pytest tests/ -q` |
| CI | `.github/workflows/ci.yml` — qa (offline) + integration + proxy gate (ID egress) + target gate (putusan3, no challenge); verification-only, never harvests |
| Lint | `python -m ruff check .` |
| Type check | `python -m mypy putusan3` |

## Architecture Notes (verified, current state)
- **Layout**: `putusan3/{domain,infrastructure,application,presentation}` Ports & Adapters; compatibility shims for all legacy paths — see `docs/ARCHITECTURE.md`
- **Domain (pure)**: `putusan3/domain/pagination_rules.py` — site constants (`BASE`, `SITE_MAX_PAGES=499`, regex), URL builders, parsers (`parse_listing`, `parse_last_page`, `is_challenge`, `is_valid_listing`, ...)
- **Infrastructure**: `playwright_engine.py` (Fetcher: context per proxy, sticky-success, fast-skip of dead proxies, watchdog `timeout+18s`, dump of invalid-200 responses), `page_store.py` (`safe_write`, `is_usable`, `manifest_log`), `sources.py`/`validator.py`/`rotator.py` (from proxy/*), `visitor.py`/`results_store.py` (from scraper/*)
- **Use-cases**: `application/harvest_pages_service.py` (stage-1 pages), `harvest_service.py` (full Harvest listing+detail+parse), `conductor_service.py` (cycle up to 499 pages)
- **Presentation**: `python -m putusan3 pages|conduct|harvest` + legacy harvest CLI in `presentation/harvest_cli.py`; `scripts/*` & `main.py` are now thin launchers
- **Main proxy pool**: `data/pools/proxy_pool.txt` (`ip:port` text, `#` header lines allowed); the conductor automatically merges fresh batches (`data/pools/proxy_pool_rN.txt`) into its head
- **All output in dedicated directories**: harvest results `data/harvest/<year>/`, pool `data/pools/`, conductor logs `logs/`, diagnostic artifacts `diagnostics/` — repo root stays clean (code + config only)

## Critical Implementation Details
1. **Proxy format**: text files use bare `ip:port` per line; Playwright contexts get `http://ip:port` automatically via `_create_proxy_settings()` (also supports `user:pass@ip:port`)
2. **Indonesian filter**: `is_indonesian` is based on the **egress country** (`origin_country_code`) — what the target sees — not the proxy's own IP; transparent proxies that echo the client's real IP would otherwise poison the pool (esp. when the dev machine itself is in Indonesia!)
3. **Cloudflare targets**: httpx/curl against `putusan3.mahkamahagung.go.id` → 403 even without a proxy; access via Chromium with a Google Chrome user agent passes — verify access in the browser, never prefilter against such targets with raw HTTP clients
4. **Rate limiting**: Respect target site; random delays 2–10s between requests
5. **Error handling**: Catch `PlaywrightError`, `TimeoutError`, proxy auth errors; rotate on failure; retired proxies get their cached page/context closed immediately
6. **Session persistence**: Reuse browser context per proxy to keep cookies/headers; reopen on proxy change

## Project Structure
```
├── main.py                     # Legacy generic-scraper CLI (works via shims)
├── config.py                   # Shim → putusan3.config
├── requirements.txt
├── requirements-dev.txt        # editable install + pytest/ruff/mypy
├── .env.example                # Template (loaded via python-dotenv)
├── putusan3/                   # ★ clean-architecture package (Ports & Adapters)
│   ├── config.py               # env-driven settings (RESULTS_PATH, PROXY_SAVE_PATH,...)
│   ├── domain/
│   │   └── pagination_rules.py # site constants/regex, URL builders, parsers (pure)
│   ├── infrastructure/
│   │   ├── playwright_engine.py# Fetcher, load_pool, run_pool (sticky+fast-skip+watchdog)
│   │   ├── page_store.py       # safe_write, is_usable, manifest_log
│   │   ├── sources.py          # 4 default proxy lists (config.PROXY_SOURCES) + normalize_proxy_line()
│   │   ├── validator.py        # latency/anonymity/transparency/GeoIP-batch validation
│   │   ├── rotator.py          # round-robin rotation + health + retirement + save/load
│   │   ├── visitor.py          # generic ProxyVisitor (context per proxy, extractors)
│   │   └── results_store.py    # JSON/CSV results export
│   ├── application/
│   │   ├── ports.py                    # fetcher/storage/pool/process protocols required by the use case
│   │   ├── async_tasks.py              # bounded worker runner, no adapter dependencies
│   │   ├── harvest_pages_service.py # stage-1: year → listing-page HTML (resumable)
│   │   ├── harvest_service.py       # Harvest: listing+detail+offline parse (full)
│   │   └── conductor_service.py     # cycle [harvest → refresh-pool → merge → resume]
│   └── presentation/
│       ├── cli.py              # python -m putusan3 pages|conduct
│       ├── bootstrap.py        # composition root: wire ports to adapters
│       ├── legacy_cli.py       # legacy generic proxy scraper CLI
│       └── harvest_cli.py      # legacy harvest CLI (build_parser/run/main)
├── main.py                     # compatibility launcher → presentation.legacy_cli
├── proxy/                      # shim re-export → putusan3.infrastructure.{sources,validator,rotator}
├── scraper/                    # shim re-export → putusan3.infrastructure.{visitor,results_store}
├── scripts/
│   ├── putusan3_pages.py       # thin launcher → application.harvest_pages_service
│   ├── putusan3_conductor.py   # thin launcher → application.conductor_service
│   ├── putusan3_harvest.py     # compatibility shim for legacy harvest CLI (used by tests)
│   └── putusan3_access.py      # single-file access probe (diagnostics)
├── data/                         # ★ ALL run results live here
│   ├── pools/                    # proxy_pool.txt for the pool + proxy_pool_rN.txt fresh batches (+ archive/)
│   └── harvest/<year>/           # pages/list_NNNN.html + pagination.json + manifest.jsonl + diag/
├── logs/                         # conductor cycle logs (conductor_run.log/.err)
├── diagnostics/                  # screenshots, access probes, HTML samples
├── tests/
│   ├── test_proxy.py              # sources/validator/rotator (via proxy.* shim)
│   ├── test_scraper.py            # visitor/extractors/export (via scraper.* shim)
│   ├── test_harvest.py            # parser/URL/offline Harvest (via scripts/putusan3_harvest shim)
│   └── test_putusan3_package.py   # package: domain/page_store/CLI/routes (no network)
├── backup/                     # code snapshot zips (pre & post restructure)
└── docs/
    ├── ARCHITECTURE.md         # new architecture + flow + trade-offs
    ├── WORKFLOW.md             # operational stage-1 steps
    └── STATE.md                # verified status snapshot (read first!)
```

## Common Pitfalls
- **Free proxies die fast** — validated proxies can die within seconds (<50% still alive at browser-test time); solidarity lies in retry-with-rotation, and in rebuilding the pool from text snapshots
- **Cloudflare-wrapped targets** — never gate proxies with a raw HTTP prefilter against those targets (you get uniform 403); test with Chromium
- **Transparent "elite" proxies** — add no Via/XFF headers yet echo the real client IP as origin; caught by `is_transparent`
- **proxy-list.download** was returning 502 for days — one dead source must never break a run (sources degrade to `[]`)
- **Playwright proxy auth** — use `user:pass@ip:port` strings; `_create_proxy_settings()` parses them
- **SSL errors** — many free proxies are HTTP-only; validate with an `http://` test URL and use `ignore_https_errors=True` in contexts
- **GeoIP false positives** — geo must be checked against the proxy's own IP **and** the egress origin (two different meanings; see validator)

## Environment Variables
```bash
TARGET_URLS="https://example.com,https://example2.com"  # Comma-separated
PROXY_REFRESH_INTERVAL=300  # Seconds between proxy list refresh
MIN_POOL_SIZE=5             # Refresh when healthy proxies drop below this
MAX_CONCURRENT=3            # Parallel browser contexts
REQUEST_TIMEOUT=60000       # ms (Playwright page timeout)

# Proxy pool validation
VALIDATION_TEST_URL="http://httpbin.org/get"  # /get echoes headers (anonymity check); prefer http:// for free proxies
SOURCE_FETCH_TIMEOUT=15.0   # seconds to fetch a proxy-source LIST (independent of VALIDATION_TIMEOUT)
MAX_PROXIES_TO_TEST=100     # Proxies validated per refresh batch

# Persistence
RESULTS_PATH=results.json     # Optional scrape-results export (.json/.csv)
PROXY_SAVE_PATH=data/pools/proxy_pool.txt # Upload pool snapshot after every refresh
PROXY_LOAD_PATH=data/pools/proxy_pool.txt # Restore pool at startup instead of fetching
```

## Testing Checklist
- [x] Proxy fetcher returns valid `ip:port` list (3.4k+ uniques, 1 dead source tolerated)
- [x] Validator correctly identifies working Indonesian proxies (egress geo + transparency)
- [x] Rotator skips dead proxies and refreshes list periodically (consecutive-failure retirement)
- [x] Scraper visits target via proxy — **live-verified**: `putusan3.mahkamahagung.go.id/direktori` HTTP 200 via pool proxy + Chrome UA (see `diagnostics/screenshot_putusan3_direktori.png`)
- [x] Graceful shutdown (browser contexts closed, results exported)
- [x] **QA pass #4 'docs + harness audit' 2026-11-07** (NO project-code changes): state re-verified (pytest 100/100 default +8 integration, ruff & mypy clean). Harness fixes in `opencode.json`: the 4 agent models `opencode/nemotron-3-ultra-free` do not exist on the provider → `opencode/nemotron-3.5-super-vl-free`; `lint` task `ruff check . && mypy putusan3` would fail on Windows PowerShell 5.1 (`&&` unsupported; tools not on PATH at all) → `python -m ruff check .; python -m mypy putusan3`; `fast-test` task used Unix `VAR=val cmd` env-assignment syntax (broken in PowerShell) → PowerShell `$env:` syntax; new tasks `integration-test` & `harvest-parse` (offline). Test harness: 5 new regression tests for previously **zero-covered** modules — `application/async_tasks.run_bounded` (queue exhaustion, failure swallow, workers=0 floor), `infrastructure/factories.SubprocessCommandRunner` (exit-code passthrough), `presentation/legacy_cli` `load` command (offline). Docs drift fixed: WORKFLOW "Phase 1 / parsing will follow" → two-stage reality; ARCHITECTURE layout gained `pool_loader.py` + `factories.py`; "5 free proxy sources" → the real default of **4** lists (README was the only correct one); README test count 91 → 100; Key Commands use `python -m` forms (ruff/mypy are not reliably on PATH on this Windows box)
- [x] 108/108 tests passing - 100/100 default run (`pytest tests/ -q`, no network) + 8 `integration` tests (`pytest -m integration`, requires internet/Chromium); 45 of them are in `tests/test_putusan3_package.py` - last verified 2026-11-07 (after docs+harness audit; was 99/99 pre-audit)
- [x] **QA pass #2 2026-10-27**: ruff + mypy remain clean; `httpx` pin bugfix (now `>=0.28.0`, matching `validator.py` which uses the `proxy=` argument), dynamic validator seed (instead of `Random(42)`), rotator `save_pool` made atomic (`safe_write`), `_get_real_ip` lock + results-exporter lock, visitor UA unified to domain `CHROME_UA` (Chrome/131 proven), `visit_with_retry` no longer treats `max_retries=0` as falsy, duplicate engine `run_pool` removed (shim → `run_bounded`), dead domain code (duplicate `LOG`) removed, defaults unified (`DEFAULT_YEAR=2026`, `DEFAULT_POOL`, `VALIDATION_TEST_URL=http://` in config — manual conductor override removed, legacy CLI default `proxies.txt`→`data/pools/proxy_pool.txt`), conduction target now reads `last_page` from `pagination.json` (`expected_last_page` — short years no longer burn 30 rounds), `pagination.json.first_page_meta` now populated (regression test), Chromium tests given the `integration` marker, root artifact `proxy_pool_r7.txt` moved to `data/pools/archive/`, `.gitignore` + `/proxy_pool*.txt` pattern, default screenshot of `putusan3_access.py` → `diagnostics/`
- [x] **QA pass 'release-ready' 2026-10-26**: `ruff check .` clean (248 auto-detections down to 0), `mypy putusan3` clean (41→0), wheel build OK (`pip wheel .` → `putusan3-3.0.0-*.whl`), pyproject fixed (`packages.find where=["."]`, `pytest pythonpath=["."]`, `ruff src=["."]`), README rewritten; bugfixes: 3× F841 (including `ScrapingResult.extracted` which was previously silently discarded), `%d rows` list-formatting in `harvest_service`, 3-tuple unpacking in `fetch_year_scope`, `python -m putusan3 conduct` dispatch which previously raised TypeError (`conductor_service.main(argv)` now accepts argv)
- [x] **QA pass #3 'finishing' 2026-10-28**: pytest 95/95 default (+8 integration), ruff & mypy clean. Fixes: `validator.__main__` imports the self-package (not `proxy.sources`), no-op `_to_row` line removed, 3 identical source classes collapsed via `PlainListSource` (class names kept), backlog items closed — `ProxyVisitor.visit(strategy=round_robin|random|best_latency)` + its own source-list fetch timeout `SOURCE_FETCH_TIMEOUT` (no longer borrowing `VALIDATION_TIMEOUT`), JSON artifacts (`pagination.json`/`courts.json`/`summary.json`) now atomic `safe_write`, `putusan3/py.typed` + wheel package-data, `visitor.stop()` exception guard, `opencode.json` v3.0 (task → `data/pools/`, skills → path `putusan3/`), docs sync (ARCHITECTURE/WORKFLOW/STATE/README typos). Backup: `backup/putusan3_pre_final_pass_20261028_*.zip`
- [x] Restructure verified: `python scripts/putusan3_{pages,conductor,harvest}.py --help` + `python -m putusan3` + `python main.py load data/pools/proxy_pool.txt` all run

## References
- Playwright Python: https://playwright.dev/python/
- Free proxy APIs: https://geonode.com/free-proxy-list, https://api.proxyscrape.com
- Indonesian IP ranges: https://www.ip2location.com/indonesia-ip-address-ranges
