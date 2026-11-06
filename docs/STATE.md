# Project State Snapshot

> Snapshot: 2026-11-07 (QA pass #4 'docs + harness audit' — no project-code changes)
> Project: Indonesian Proxy Scraper (Putusan3) — Python + Playwright, free rotating Indonesian proxies

## Current data condition (2026-11-07 re-check)

- Stage-1 for year 2026 remains **complete**: 499/499 listing pages, 9,337 court
  decisions (summary.json).
- Stage-2 details are **still idle at 266/9,337** — unchanged since the 2026-10-28
  snapshot (7 days without progress); `logs/details_run.log` shows the run ended
  early. It is resumable at any time:
  `python scripts/putusan3_harvest.py --year 2026 --details-only --workers 4`
  (logs at `logs/details_run.log|.err`), then `--parse-only` for the CSV inventory.

## QA pass #4 'docs + harness audit' (2026-11-07)

NO project-code changes — only docs, `opencode.json`, and the test harness.
Gate verified before and after: pytest **100/100 default** (+8 integration),
ruff clean, mypy clean (26 source files).

1. **Harness model bug**: all 4 `opencode.json` agent models referenced
   `opencode/nemotron-3-ultra-free`, which does not exist on the provider →
   `opencode/nemotron-3.5-super-vl-free` (verified via model search).
2. **Harness platform bugs**: `lint` task `ruff check . && mypy putusan3` — `&&`
   is unsupported in Windows PowerShell 5.1 and ruff/mypy were not on PATH at
   all → `python -m ruff check .; python -m mypy putusan3`; `fast-test` used
   Unix `VAR=val cmd` env-assignment syntax (dead in PowerShell) → `$env:`
   syntax. New tasks: `integration-test`, `harvest-parse` (offline CSV).
3. **Test-harness coverage holes closed**: `application/async_tasks.run_bounded`,
   `infrastructure/factories.SubprocessCommandRunner`, and
   `presentation/legacy_cli` (the `load` command) had **zero direct tests** —
   5 new deterministic regressions added (queue exhaustion, failure swallow,
   workers=0 floor, exit-code passthrough, offline load CLI).
4. **Docs drift fixed**: WORKFLOW still said "Phase 1 … parsing will follow the
   next phase" although phase 2 is live since 2026-10-27 → retitled to
   "Stage 1 & Stage 2"; ARCHITECTURE's layout tree omitted
   `infrastructure/pool_loader.py` and `infrastructure/factories.py` → added;
   "5 free proxy sources" (AGENTS/STATE/ARCHITECTURE) corrected to the real
   default of **4** lists in `config.PROXY_SOURCES` (README's "4" was already
   right — verified via `create_sources()`); README's stale "91 tests" → 100.
5. **Aging environment note**: ruff/mypy are installed but not on PATH on this
   Windows machine — always use the `python -m ruff` / `python -m mypy` forms
   (also updated in AGENTS Key Commands).

## QA pass #3 'finishing' (2026-10-28)

All verified: pytest 95/95 default (+8 integration), ruff clean, mypy
clean. Pre-change backup: `backup/putusan3_pre_final_pass_20261028_*.zip`.

1. **Backlog closed**: `ProxyVisitor.visit()` now accepts `strategy=`
   (`round_robin` default, `random`, `best_latency`) via `_pick_proxy()` —
   vocabulary identical to `ProxyPoolManager.get_proxy()`.
2. **Backlog closed**: the proxy source list fetch timeout is no longer borrowed
   from `VALIDATION_TIMEOUT` — variable `SOURCE_FETCH_TIMEOUT` (seconds, default
   15 = VALT/1000) in `putusan3.config`, used by `ProxySource.timeout`.
3. **Proxy source dedup**: `ProxyscrapeSource`/`ProxyListDownloadSource`/
   `GitHubRawSource` (identical fetch() 3×) are now subclasses of
   `PlainListSource`; class names are still exported (shim & test safe).
4. **Hygiene**: `validator.__main__` imports self-package (not `proxy.sources`);
   the no-op `data["status_code"] = data.get("status_code")` in `results_store`
   was removed; `visitor.stop()` browser/playwright close is wrapped in
   try/except; `import json` moved to module level.
5. **Write resilience**: `pagination.json` (read by the conductor across
   processes), `courts.json` (2 locations), `summary.json` are now written via
   `safe_write`.
6. **Packaging**: `putusan3/py.typed` + `[tool.setuptools.package-data]` — the
   package now carries a typed marker for wheel consumers.
7. **Tooling configuration**: `opencode.json` → v3.0.0, task pool → `data/pools/`,
   skills → `putusan3/` module paths, `lint` → `mypy putusan3`, mojibake fixed.
8. **Docs sync**: ARCHITECTURE (removed stale `listing_path`, Extension Points
   strategy), WORKFLOW (stage-2 section "not yet worked on" → now live), README
   typo "Details-only used", AGENTS (stage-2 row in Key Commands, new env vars,
   typo "harvst").

New regressions added: `visit()` strategy dispatch (3 tests), default
`SOURCE_FETCH_TIMEOUT`.

## Stage-2: 2026 detail harvest (2026-10-27)

- 2026 listings are **complete on disk**: 499/499 pages, **9,337 decisions**
  (source: `local_rows`).
- **New CSV feature** (`putusan3_<year>_links.csv`, columns `nomor,title,url_pdf,url_zip`):
  - `domain.parse_nomor()/nomor_from_text()` — extract the number after the
    "Nomor" marker; boundaries: double space (tag boundary) / stopword
    (tentang, etc.); must contain digits.
  - `Harvest.parse_all()` fills `rec["nomor"]` (detail body → listing title fallback);
    `Harvest.write_results()` writes the CSV (csv+StringIO, `lineterminator="\n"` —
    `write_text` on Windows translates it to `\r\n`; do NOT use explicit `"\r\n"`
    → double CRLF).
  - CLI `--parse-only` & the final print show the line `CSV inventory : N rows -> ...`.
- **LATENT BUG FOUND & FIXED**: the `--details-only` flag previously NEVER
  fetched details — the fetch block only ran when `not details_only`, so the
  flag silently downgraded to `--parse-only` (proven: a "details" run finished
  in 12 seconds with 0 details saved). Now `harvest_cli.run()` has an explicit
  branch: details-only → `fetch_details` via the Fetcher. 2 regression tests added.
- **Live run in progress** (PID changes per session; logs `logs/details_run.log|.err`):
  `python scripts/putusan3_harvest.py --year 2026 --details-only --workers 4`
  — resumable per detail file; CSV + summary are rewritten at the end of each run.
  Monitor: `Get-Content logs\details_run.log -Tail 5` + count
  `(Get-ChildItem data\harvest\2026\details -Filter detail_*.html).Count`.
- **Modern CLI now complete**: `python -m putusan3 pages|conduct|harvest` —
  the new `harvest` route bridges harvest_cli (details-only/parse-only/CSV) into
  the single-door CLI; `harvest_cli.main(argv)` now accepts argv. Final dead-code
  sweep: `page_store.listing_path` removed (duplicate naming; tests now check
  via the service), vulture `--min-confidence 80` clean. README.md fully rewritten to
  the two-stage workflow + CSV. QA gate: 91/91 default + 8 integration, ruff clean,
  mypy clean.
- QA gate: 88/88 default + 8 integration, ruff clean, mypy clean.

## QA pass #2 (2026-10-27)

Package of fixes from the senior QA review (all verified: pytest 84/84 default
+ 8 integration, ruff clean, mypy clean):

1. **Dependency bug**: pin `httpx>=0.28.0` (requirements + pyproject). `validator.py`
   uses the `proxy=` argument that only exists in 0.28+; the old pin `>=0.25.0`
   sent older installs into runtime crashes.
2. **Atomic**: `rotator.save_pool()` now uses `safe_write` (`.part` → rename) —
   the conductor merges the pool every round; full writes were corruption-prone.
3. **Validator**: dynamic shuffle seed (previously `Random(42)` — a static test
   subset); `_get_real_ip` got a lock (previously could double-fetch on race).
4. **Results store**: JSON/CSV exporter serialized via `threading.Lock`
   (read-modify-write could previously overwrite each other).
5. **Single source of defaults**: `DEFAULT_YEAR=2026` & `DEFAULT_POOL` now live only in
   `putusan3.domain.pagination_rules`; `VALIDATION_TEST_URL` defaults to
   `http://httpbin.org/get` in `putusan3.config` — the conductor's manual override
   was removed; legacy CLI `load`/`save` defaults changed from `"proxies.txt"` /
   `"data/pools/proxies.txt"` → `DEFAULT_POOL`.
6. **Conductor**: the pass target now comes from `pagination.json`
   (`expected_last_page()`, capped at 499) — years with <499 pages no longer get
   stuck in 30 refresh rounds; neighbor: `pages_complete(..., last_page=)`.
7. **Breadcrumb**: `PagesRun` now captures the page-1 fetch meta into
   `pagination.json["first_page_meta"]` (previously always `{}`); `_page_path`
   was made public as `page_path`.
8. **Dead code/stale**: the duplicate `run_pool` in `playwright_engine` was removed
   (shim `scripts/putusan3_harvest.py` exports `run_bounded` as `run_pool`);
   the duplicate `LOG` in `pagination_rules` was removed; the stale visitor UA
   Chrome 119/120 was replaced — the list is now mated to `CHROME_UA` (Chrome/131,
   proven to pass Cloudflare); `visit_with_retry` no longer treats
   `max_retries=0` as falsy-default; `_visit_locks` is cleaned up when a proxy
   context is closed.
9. **Hygiene**: `test_visitor_context_manager` (real Chromium launcher) is now
   `@pytest.mark.integration`; the root artifact `proxy_pool_r7.txt` was moved to
   `data/pools/archive/proxy_pool_r7_root_snapshot_20261027.txt`;
   `.gitignore` + `/proxy_pool*.txt`; the default screenshot path of
   `scripts/putusan3_access.py` → `diagnostics/`.

New regressions added: `expected_last_page` (4 cases), the capture of
`first_page_meta`, the new `write_pagination` contract.

## Current status (2026-10-26)

- **Workspace reorganization COMPLETE (2026-10-26 midday)**: the root was cleaned; **all run outputs now live in dedicated directories** — pool in `data/pools/` (main `proxy_pool.txt`, fresh batches `proxy_pool_rN.txt`, old ones in `archive/`), harvest results in `data/harvest/<year>/`, conductor logs in `logs/`, diagnostic artifacts in `diagnostics/`, code archives in `backup/`. Caches/builds (`__pycache__`, `.pytest_cache`, `.ruff_cache`, `.mypy_cache`, `build/`, `*.egg-info`) were deleted. The default code paths were updated: `DEFAULT_POOL=data/pools/proxy_pool.txt`, default out-dir `data/harvest/<year>` (all services), the conductor's refresh batch writes to `data/pools/`, `save_pool()` & the `save` CLI auto-mkdir. New `.gitignore`.
- **Conductor restarted on the new layout**: stopped (resumable), relaunched detached with `--pool data\pools\proxy_pool.txt --out-dir data\harvest\2026`, logging to `logs/conductor_run.log` — resume verified: immediately **139/499 pages** saved at round-1 detection.
- **QA pass 'release-ready' COMPLETE (2026-10-26)**: `ruff check .` clean, `mypy putusan3` clean (41 errors → 0), wheel build verified (`pip wheel .` → `putusan3-3.0.0-py3-none-any.whl`), pyproject corrected (`packages.find where=["."]`, `pytest pythonpath=["."]`, `ruff src=["."]` — previously stale `["src"]`), `README.md` fully rewritten to the `putusan3/` generation. Behavior bugfixes: (1) `ScrapingResult.extracted` — the `extract_fn` result was previously computed then silently discarded, now it is carried home; (2) `harvest_service._fetch_listing_page` — `rows` was reused across int/list types; (3) `harvest_service.fetch_year_scope` — unpacking `html, _ = 3-tuple` would raise ValueError at execution; (4) `python -m putusan3 conduct ...` previously raised TypeError because `conductor_service.main()` did not accept argv — now `main(argv: list[str] | None = None)`.
- **Clean-architecture restructure COMPLETE**: the logic now lives in one place, `putusan3/{domain,infrastructure,application,presentation}`; all old paths (`main.py`, `proxy/*`, `scraper/*`, `scripts/*`, `config.py`) became shim re-exports.
- **Backups safe**: `backup/putusan3_code_backup_20261025_2136.zip` (pre-restructure), `backup/putusan3_after_restructure_20261025_2159.zip`, `backup/putusan3_after_qa_pass_20261026_1135.zip`, `backup/backup_pre_refactor_20261025_185226.zip` (moved from the root), `backup/putusan3_pre_di_refactor_20261027_0653.zip`, `backup/putusan3_after_qa_pass2_20261027_1521.zip` (post QA pass #2 — clean snapshot: code+config+docs only, without data/logs/root artifacts).

### Entanglement engine (final behavior, live-verified)

| Situation | Behavior |
|---------|----------|
| Dead proxy (`ERR_TIMED_OUT`, reset) | fast skip (~≤20 s), immediately to the next proxy |
| Proxy passes validation | **sticky** — kept in use until it fails/challenges |
| Cloudflare challenge | `goto` + wait for the listing marker ≤10 s before validation |
| 200 but invalid | body dumped to `out/diag/reject_200_last.html` |
| 429 | pause 5–8 s before rotation |
| Pool exhausted | fail-fast "No live proxies left"; the conductor refreshes the pool then resumes |

## Snapshot BEFORE the restructuring (2026-10-24, still valid for the legacy scraper)
> Path note: since the 2026-10-26 reorganization, the main pool lives at `data/pools/proxy_pool.txt`,
> the screenshot moved to `diagnostics/screenshot_putusan3_direktori.png`,
> and the harvest results to `data/harvest/<year>/`. The commands below are the
> trail of live verification (with the old paths as they were then).

## Status: ALL FEATURES LIVE-VERIFIED

The pipeline **fetch → validate → rotate → scrape → export** runs end-to-end;
access to the main target `putusan3.mahkamahagung.go.id` is proven to get
through Cloudflare. Test suite: **36/36 passing** (`pytest tests/ -q`).

## Capabilities (implemented & live-verified)

| Feature | Status | Live evidence |
|-------|--------|-------------|
| Fetch 4 default proxy-source lists (dedupe, graceful degradation) | ✅ | 3.4k+ unique proxies/run; 1 source's 502 handled without crashing |
| Validation (latency, anonymity, transparency, GeoIP) | ✅ | batch ip-api.com POST `/batch` chunked ≤100 IPs; per-IP nested fallback |
| Two geo dimensions: `origin_country_code` (exit) vs `proxy_country_code` (identity) | ✅ | MY/EG proxies "pretending to be Indonesian" via transparent proxy are auto-filtered |
| Transparency detection (`origin == real client IP`) | ✅ | real client `103.157.58.88` (ID) emerged as evidence |
| Round-robin rotator + health + consecutive-failure retirement | ✅ | 3 proven retired / running normally |
| Auto-refresh (min_pool_size=5, interval 300s) + periodic task | ✅ | periodic task starts without an initial fetch (load mode) |
| Playwright stealth (Chrome UA, wd-undefined, locale id-ID, tz Jakarta) | ✅ | header `Accept-Language: id-ID` at httpbin |
| Retry backoff + random delay 2–10s (rate limiting) | ✅ | visit retry logs span many pools |
| Result JSON/CSV export (`RESULTS_PATH`) | ✅ | `test_results.json` — successes and failures both captured |
| Text pool persistence (`save`/`load`, `PROXY_SAVE_PATH`, `PROXY_LOAD_PATH`) | ✅ | `proxy_pool.txt` restore <1s without network; successful visit via the loaded pool |
| putusan3 target access via Chromium | ✅ | HTTP 200, title "Direktori Putusan", 157.2k chars, screenshot |

## Artifacts in the workspace

| File | Contents |
|------|-----|
| `proxy_pool.txt` | valid pool (1 `#` header + `ip:port` per line) |
| `screenshot_putusan3_direktori.png` | proof of successful access to the main target (full-page) |

## Validated CLI commands

```
python main.py save proxy_pool.txt   # validate → write text snapshot (exit 1 if 0 winners, normal)
python main.py load proxy_pool.txt   # populate the pool from text (no network)
PROXY_LOAD_PATH=proxy_pool.txt python main.py test     # pool from text → instant scrape
PROXY_SAVE_PATH=proxy_pool.txt python main.py test-all # each refresh auto-saves
```

### Standalone script (no opencode dependency)

`scripts/putusan3_access.py` — single-file, only `playwright`:
`python scripts/putusan3_access.py --screenshot out.png --html out.html --json result.json`
Exit 0 = "ACCESS VERIFIED", exit 1 = all candidates failed. **Live-verified**: HTTP 200
"direktori" via `103.189.251.19:8080` (43.8 s, 136.8k chars, `putusan3_direktori_live.png`).

## Bug fix: `save` ignored `PROXY_LOAD_PATH` (fixed 2026-10-24)

`initialize()` previously only processed `PROXY_LOAD_PATH` inside `if refresh:`,
while `save` runs with `refresh=False` → empty pool → nondeterministic exit 1.
Now loading the `PROXY_LOAD_PATH` pool happens **regardless of the value of `refresh`**, and
`save` adds `allow_empty=(command == "save")` so it is not gated on a healthy pool.
Verified: `PROXY_LOAD_PATH=proxy_pool.txt python main.py save` → exit 0 "Saved 6 proxies"; `load` → exit 0.

## Configuration proven to work (for putusan3 access)

```powershell
$env:VALIDATION_TEST_URL = 'http://httpbin.org/get'   # /get (echo headers) for the anonymity check; HTTP not HTTPS (free proxies are mostly http-only)
$env:MAX_PROXIES_TO_TEST = '100'
$env:MAX_RETRIES         = '6'
$env:RESULTS_PATH        = 'test_results.json'        # optional
```

## Key insights (preserved so they are not lost)

1. **`putusan3.mahkamahagung.go.id` sits behind Cloudflare** — pure HTTP clients (`httpx`) get **403** even without a proxy; a Chrome UA via Chromium **passes without any anti-bot framework**
2. **Free proxies churn fast** — validated proxies can die within seconds/minutes (<50% were still alive by browser-test time); the design once made a wrong assumption with an httpx prefilter
3. **Proxy-list.download 502 for several days** — other sources cover it; graceful degradation works
4. **Transparent proxies fool the geo filter** — Egypt/MY proxies could parade as "Indonesia" if geo is checked against the *egress origin* (they byte-cover using the dev computer's IP, which is indeed ID); solution: `exclude_transparent` + two-dimensional geo
5. **All partial text is consumable** — `ip:port` per line is enough for `curl -x`, browsers, and each other

## Remaining backlog (optional, not yet worked on)

- Swap ip-api.com → local geo DB (maxmind mmdb) for production
- [Closed QA #3 2026-10-28] `ProxyVisitor.visit(strategy=)` — now supported
- [Closed QA #3 2026-10-28] Source fetch timeout — now `SOURCE_FETCH_TIMEOUT`
- [Implied finished by stages 1–2] The putusan3-specific scraper — lives in
  `putusan3.domain.pagination_rules` + `Fetcher` (listing, detail, CSV)
