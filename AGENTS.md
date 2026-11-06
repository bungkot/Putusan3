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

# Restrukturisasi selesai: logika hidup di putusan3/{domain,infrastructure,application,presentation}
# Semua entry point lama tetap jalan (shim re-export).

# Tahap 1: kumpulkan proxy Indonesia → snapshot teks pool
python main.py save data/pools/proxy_pool.txt

# Workflow tahap 1 (rekomendasi): konduktor siklus [harvest → refresh-pool → merge → resume]
# default: pool data/pools/proxy_pool.txt, hasil data/harvest/<year>/ — path eksplisit opsional
python scripts/putusan3_conductor.py --year 2026

# Alternatif entry point modern
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
| Harvest halaman (single pass) | `python scripts/putusan3_pages.py --year 2026 --workers 3 --timeout 20000 --max-attempts 24` |
| **Tahap-2 detail (resumable, detached-ok)** | `python scripts/putusan3_harvest.py --year 2026 --details-only --workers 4` |
| Parse offline → CSV inventaris | `python scripts/putusan3_harvest.py --year 2026 --parse-only` |
| Harvest via modern CLI | `python -m putusan3 pages\|conduct --year 2026 ...` |
| Smoke test 2 halaman | `python scripts/putusan3_pages.py --year 2026 --max-pages 2` |
| Full harvest CLI lama (listing+detail) | `python scripts/putusan3_harvest.py --year 2022 --scope courts` |
| Save pool snapshot | `python main.py save data/pools/proxy_pool.txt` |
| Load pool from text | `python main.py load data/pools/proxy_pool.txt` |
| Restore pool + scrape (instant) | `PROXY_LOAD_PATH=data/pools/proxy_pool.txt python main.py test-all` |
| Run legacy generic scraper | `python main.py` |
| Cek kemajuan konduktor | `(Get-ChildItem data/harvest/2026/pages -Filter list_*.html).Count` + `Get-Content logs/conductor_run.log -Tail 5` |
| Tests | `python -m pytest tests/ -q` |
| Lint | `ruff check .` |
| Type check | `mypy .` |

## Architecture Notes (verified, current state)
- **Layout**: `putusan3/{domain,infrastructure,application,presentation}` Ports & Adapters; shims kompatibilitas untuk semua jalur lama — lihat `docs/ARCHITECTURE.md`
- **Domain (pure)**: `putusan3/domain/pagination_rules.py` — konstanta situs (`BASE`, `SITE_MAX_PAGES=499`, regex), URL builders, parser (`parse_listing`, `parse_last_page`, `is_challenge`, `is_valid_listing`, ...)
- **Infrastruktur**: `playwright_engine.py` (Fetcher: context per proxy, sticky-success, fast-skip proxy mati, watchdog `timeout+18s`, dump 200-invalid), `page_store.py` (`safe_write`, `is_usable`, `manifest_log`), `sources.py`/`validator.py`/`rotator.py` (dari proxy/*), `visitor.py`/`results_store.py` (dari scraper/*)
- **Use-cases**: `application/harvest_pages_service.py` (tahap-1 pages), `harvest_service.py` (Harvest lengkap listing+detail+parse), `conductor_service.py` (siklus sampai 499 halaman)
- **Presentation**: `python -m putusan3 pages|conduct|harvest` + CLI harvest lama di `presentation/harvest_cli.py`; `scripts/*` & `main.py` tinggal launcher tipis
- **Pool proxy utama**: `data/pools/proxy_pool.txt` (teks `ip:port`, header `#` diperbolehkan); konduktor otomatis merge batch segar (`data/pools/proxy_pool_rN.txt`) ke head-nya
- **Semua output di direktori khusus**: hasil harvest `data/harvest/<year>/`, pool `data/pools/`, log konduktor `logs/`, artefak diagnostik `diagnostics/` — root repo tetap bersih (kode + config saja)

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
├── putusan3/                   # ★ paket clean-architecture (Ports & Adapters)
│   ├── config.py               # settings env-driven (RESULTS_PATH, PROXY_SAVE_PATH,...)
│   ├── domain/
│   │   └── pagination_rules.py # konstanta/regex situs, URL builders, parsers (pure)
│   ├── infrastructure/
│   │   ├── playwright_engine.py# Fetcher, load_pool, run_pool (sticky+fast-skip+watchdog)
│   │   ├── page_store.py       # safe_write, is_usable, manifest_log
│   │   ├── sources.py          # 5 sumber proxy gratis + normalize_proxy_line()
│   │   ├── validator.py        # validasi latency/anonymity/transparency/GeoIP-batch
│   │   ├── rotator.py          # rotasi round-robin + health + retirement + save/load
│   │   ├── visitor.py          # ProxyVisitor umum (context per proxy, extractors)
│   │   └── results_store.py    # ekspor hasil JSON/CSV
│   ├── application/
│   │   ├── ports.py                    # protokol fetcher/storage/pool/process yang dibutuhkan use case
│   │   ├── async_tasks.py              # runner worker bounded, tanpa dependensi adapter
│   │   ├── harvest_pages_service.py # tahap-1: tahun → HTML halaman listing (resumable)
│   │   ├── harvest_service.py       # Harvest: listing+detail+parse offline (lengkap)
│   │   └── conductor_service.py     # siklus [harvest → refresh-pool → merge → resume]
│   └── presentation/
│       ├── cli.py              # python -m putusan3 pages|conduct
│       ├── bootstrap.py        # composition root: wire ports ke adapter
│       ├── legacy_cli.py       # legacy generic proxy scraper CLI
│       └── harvest_cli.py      # CLI harvest lama (build_parser/run/main)
├── main.py                     # launcher kompatibilitas → presentation.legacy_cli
├── proxy/                      # shim re-export → putusan3.infrastructure.{sources,validator,rotator}
├── scraper/                    # shim re-export → putusan3.infrastructure.{visitor,results_store}
├── scripts/
│   ├── putusan3_pages.py       # launcher tipis → application.harvest_pages_service
│   ├── putusan3_conductor.py   # launcher tipis → application.conductor_service
│   ├── putusan3_harvest.py     # shim kompatibilitas harvest CLI lama (dipakai tests)
│   └── putusan3_access.py      # probe akses satu-file (diagnostik)
├── data/                         # ★ SEMUA hasil run hidup di sini
│   ├── pools/                    # proxy_pool.txt utk pool + proxy_pool_rN.txt batch segar (+ archive/)
│   └── harvest/<year>/           # pages/list_NNNN.html + pagination.json + manifest.jsonl + diag/
├── logs/                         # log siklus konduktor (conductor_run.log/.err)
├── diagnostics/                  # screenshot, probe akses, sampel HTML
├── tests/
│   ├── test_proxy.py              # sumber/validator/rotator (via shim proxy.*)
│   ├── test_scraper.py            # visitor/extractors/export (via shim scraper.*)
│   ├── test_harvest.py            # parser/URL/Harvest offline (via shim scripts/putusan3_harvest)
│   └── test_putusan3_package.py   # paket: domain/page_store/CLI/routes (tanpa jaringan)
├── backup/                     # snapshot zip kode (pre & pasca restrukturisasi)
└── docs/
    ├── ARCHITECTURE.md         # arsitektur baru + alur + trade-off
    ├── WORKFLOW.md             # langkah tahap-1 operasional
    └── STATE.md                # snapshot status terverifikasi (baca dulu!)
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
- [x] 99/99 tests passing — 91/91 default run (`pytest tests/ -q`, tanpa jaringan) + 8 test `integration` (`pytest -m integration`, butuh internet/Chromium); 39 di antaranya `tests/test_putusan3_package.py` — terakhir diverifikasi 2026-10-27 (pasca tahap-2 CSV/CLI)
- [x] **QA pass #2 2026-10-27**: ruff + mypy tetap bersih; bugfix `httpx` pin (kini `>=0.28.0`, cocok dengan `validator.py` yang memakai argumen `proxy=`), validator seed dinamis (bukan `Random(42)`), `save_pool` rotator jadi atomik (`safe_write`), lock `_get_real_ip` + lock eksportir results, UA visitor unifikasi `CHROME_UA` domain (Chrome/131 proven), `visit_with_retry` tak lagi menganggap `max_retries=0` falsy, duplikat `run_pool` engine dihapus (shim → `run_bounded`), dead code domain (`LOG` duplikat) dihapus, default ter-unifikasi (`DEFAULT_YEAR=2026`, `DEFAULT_POOL`, `VALIDATION_TEST_URL=http://` di config — override konduktor manual dihapus, default legacy CLI `proxies.txt`→`data/pools/proxy_pool.txt`), conduction target kini membaca `last_page` dari `pagination.json` (`expected_last_page` — tahun pendek tak lagi menghabiskan 30 putaran), `pagination.json.first_page_meta` kini terisi (regression test), test Chromium diberi marker `integration`, artefak root `proxy_pool_r7.txt` dipindah ke `data/pools/archive/`, `.gitignore` + pola `/proxy_pool*.txt`, default screenshot `putusan3_access.py` → `diagnostics/`
- [x] **QA pass 'release-ready' 2026-10-26**: `ruff check .` bersih (248 autodifikasi naik ke 0), `mypy putusan3` bersih (41→0), wheel build OK (`pip wheel .` → `putusan3-3.0.0-*.whl`), pyproject diperbaiki (`packages.find where=["."]`, `pytest pythonpath=["."]`, `ruff src=["."]`), README di-rewrite; bugfix: 3× F841 (termasuk `ScrapingResult.extracted` yang dulu dibuang diam-diam), `%d rows` list-formatting di `harvest_service`, unpack 3-tuple di `fetch_year_scope`, dispatch `python -m putusan3 conduct` yang dulu TypeError (`conductor_service.main(argv)` kini menerima argv)
- [x] **QA pass #3 'finishing' 2026-10-28**: pytest 95/95 default (+8 integration), ruff & mypy bersih. Perbaikan: `validator.__main__` import self-package (bukan `proxy.sources`), no-op baris `_to_row` dihapus, 3 kelas sumber identik dipadatkan lewat `PlainListSource` (nama kelas tetap), item backlog ditutup — `ProxyVisitor.visit(strategy=round_robin|random|best_latency)` + timeout fetch daftar sumber sendiri `SOURCE_FETCH_TIMEOUT` (tidak lagi meminjam `VALIDATION_TIMEOUT`), artefak JSON (`pagination.json`/`courts.json`/`summary.json`) kini `safe_write` atomik, `putusan3/py.typed` + package-data wheel, `visitor.stop()` guard exception, `opencode.json` v3.0 (task → `data/pools/`, skills → path `putusan3/`), docs sync (ARCHITECTURE/WORKFLOW/STATE/README typo). Backup: `backup/putusan3_pre_final_pass_20261028_*.zip`
- [x] Restructure verified: `python scripts/putusan3_{pages,conductor,harvest}.py --help` + `python -m putusan3` + `python main.py load data/pools/proxy_pool.txt` semua jalan

## References
- Playwright Python: https://playwright.dev/python/
- Free proxy APIs: https://geonode.com/free-proxy-list, https://api.proxyscrape.com
- Indonesian IP ranges: https://www.ip2location.com/indonesia-ip-address-ranges