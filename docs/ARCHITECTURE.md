# Architecture

This document describes the system architecture: layers, modules, data flow, and trade-offs. It was rewritten after the clean-architecture restructuring (2026-10-25) — the logic now lives in a single place: the `putusan3/` package.

## Layout: Ports & Adapters

```
putusan3/
├── config.py                              # copied verbatim from the old config (path-free)
├── domain/                                           # PURE — no I/O, no playwright
│   └── pagination_rules.py               # site constants+regex, URL builders,
│                                         # parsers (parse_listing, parse_last_page,
│                                         # is_challenge, is_valid_*), CHROME_UA etc.
├── infrastructure/                                   # ADAPTERS — real-world side effects
│   ├── playwright_engine.py              # load_pool/load_courts_file (pool text),
│   │                                     # Fetcher (1 Chromium, 1 Context/proxy,
│   │                                     # sticky-success, fast-skip dead, watchdog),
│   │                                     # run_pool (bounded worker pool)
│   ├── page_store.py                     # safe_write/is_usable/manifest_log
│   ├── pool_loader.py                    # FilePoolLoader: read pool text for services
│   ├── factories.py                      # PlaywrightFetcherFactory, SubprocessCommandRunner
│   ├── sources.py                        # 4 default proxy lists (config.PROXY_SOURCES)
│   │                                     # + GeoNode/FreeProxyListNet (usable when configured)
│   ├── validator.py                      # validation + batch GeoIP
│   ├── rotator.py                        # round-robin + health
│   ├── visitor.py                        # generic ProxyVisitor
│   └── results_store.py                  # JSON/CSV export
├── application/                                      # USE-CASES — orchestration layer on top
│   ├── ports.py                           # contracts for fetcher, storage, pool loader, process runner
│   ├── async_tasks.py                     # bounded worker runner, no adapters
│   ├── harvest_pages_service.py          # stage 1: year → HTML for each listing page
│   ├── harvest_service.py                # Harvest: listing+detail+offline parse (full)
│   └── conductor_service.py              # cycle [harvest → refresh-pool → merge → resume]
└── presentation/                                     # CLI
    ├── cli.py                            # python -m putusan3 pages|conduct
    ├── bootstrap.py                      # composition root: bind ports to concrete adapters
    ├── harvest_cli.py                    # build_parser/run/main of the legacy harvest CLI
    └── legacy_cli.py                     # generic proxy CLI (main.py launcher)
```

## Dependency inversion

Use cases in `application/` depend only on `domain/` and on the contracts in
`application/ports.py`. They do not import Playwright, filesystem adapters,
proxy sources, or subprocess implementations. `FetcherPort`, `FetcherFactoryPort`,
`PageStorePort`, `PoolLoaderPort`, and `CommandRunnerPort` allow the use cases
to be tested with fake/in-memory adapters without any network, browser, or real
processes.

The concrete adapters live in `infrastructure/`: `PlaywrightFetcherFactory`,
`LocalPageStore`, `FilePoolLoader`, and `SubprocessCommandRunner`. The composition
root in `presentation/bootstrap.py` binds those adapters before invoking the
use cases. `presentation/harvest_cli.py` is a compatibility entry point and
performs the same composition for the legacy harvest flow.

```text
presentation/bootstrap ──> application use cases ──> application ports
          │                         │
          └── infrastructure adapters ──> implement application ports
domain <──────────────── application
```

Thus the source dependency direction never points from application to
infrastructure. The test `test_application_layer_does_not_import_infrastructure`
guards this boundary against regressions.

### Compatibility (post-restructuring shims)

All legacy entry points keep working without any behavior change:

| Legacy path | Now |
|------------|----------|
| `main.py` (proxy scraper CLI) | thin launcher into `presentation/legacy_cli.py` |
| `config.py`, `proxy/*`, `scraper/*` | re-export shims to `putusan3.config` / `putusan3.infrastructure.*` |
| `scraper/visitor.py` | `sys.modules` alias → `putusan3.infrastructure.visitor` (so that `mock.patch("scraper.visitor.X")` keeps working) |
| `scripts/putusan3_harvest.py` | full re-export shim (legacy tests: `harvest.parse_*`, `harvest.Harvest`, `harvest.safe_write`) |
| `scripts/putusan3_pages.py`, `scripts/putusan3_conductor.py` | thin launchers → composition root in `putusan3.presentation.bootstrap` |
| `python -m putusan3 pages ...` | new entry point (presentation/cli.py) |

## Stage 1 Flow (harvesting a year's listing pages)

```
            ┌──────────────────────────────────────────────────────────────┐
            │ conductor (application/conductor_service.py)                 │
            │   loop: pages → refresh-pool (main.py save) → merge → resume │
            └──────────────┬───────────────────────────────────────────────┘
                           │ isolated subprocess
                           ▼
        ┌── harvest_pages_service (application) ────────────────────────┐
        │  PoolLoaderPort → FetcherFactoryPort → FetcherPort            │
        │  page 1 → pagination.json (last_page, capped_at_site_limit)   │
        │  pages 2..last in parallel (run_pool, workers=3)              │
        │  each page: cached? → skip : fetch → safe_write               │
        └───────┬───────────────────────────────┬──────────────────────┘
                │ domain                        │ infrastructure
                ▼                               ▼
  putusan3/domain/pagination_rules   infrastructure adapter implements ports
  - year_listing_url(year, page)     - Fetcher.get(): max_attempts ≤24, sticky
  - parse_last_page (data-ci-…)      - fast-skip dead proxies (~≤20s/attempt)
  - is_valid_listing                 - goto + wait_for_selector marker (10 s)
  - pagination 499 = site cap        - 200-invalid → dump diag/reject_200_last.html
                                     - 429 → pause 5–8 s; retire after 2 consecutive failures
                                     ▼
                      putusan3/infrastructure/page_store
                      - list_NNNN.html (atomic: .part → replace)
                      - manifest.jsonl (JSONL ts+meta)
                      - pagination.json
```

### Key Fetcher behaviors (live-verified 2026-10-25)

1. **Dead proxy → skip immediately** — `ERR_TIMED_OUT` and the like are detected within ~9 s; there is no extra time spent proving a proxy is dead. `finally:` always returns the lease to the queue unless the proxy has already been retired.
2. **Fast proxy → sticky** — a proxy that passes the `validator` is reused (log `(sticky)`) until it fails or hits a challenge; only then does rotation happen.
3. **Hard watchdog** — `asyncio.wait_for(timeout + 18 s)` guarantees a single attempt never hangs longer than that (goto 20 s + selector wait 10 s + buffer).
4. **200-but-invalid → dump** — the body is saved to `out/diag/reject_200_last.html` for diagnosis (never guess at markup).
5. **429 → pause 5–8 s** before rotating; 403/503 = challenge `is_challenge` → fast skip.
6. **Pool exhausted → "No live proxies left"** propagates up to the service; the conductor prepares a new pool and then resumes.

## Legacy proxy scraper flow (still alive via shims)

The `main.py` CLI (`test|test-all|continuous|list-proxies|save|load`) still works: `ProxyPoolManager` → `fetch_all_proxies` (5 httpx sources) → `validate_proxies` (anonymity/transparency/batch GeoIP ≤100 IPs/POST) → `get_best_proxies(ID, anonymous)` → `ProxyRotator` round-robin → `ProxyVisitor` (Playwright, context per proxy, stealth UA+locale id-ID+tz Jakarta) → `export_results` (RESULTS_PATH).

The full details of this flow (startup/visit/retry sequence) were moved unchanged — see the git-archived zip `backup/putusan3_code_backup_*.zip` if you need the pre-restructuring version.

## Core Data Types

| Type | Location | Contents |
|------|--------|--------|
| `ProxyValidationResult` | `infrastructure/validator.py` | `is_working`, `latency_ms`, `is_anonymous`, `is_transparent`, `is_indonesian` (based on egress), `origin_ip` + `origin_country_code`, `proxy_country_code`, `error`, `response_headers` |
| `ProxyHealth` | `infrastructure/rotator.py` | `successes`, `failures`, `consecutive_failures`, `avg_latency_ms`, `is_retired`, `validation_result` |
| `ScrapingResult` | `infrastructure/visitor.py` | `url`, `proxy`, `success`, `status_code`, `content`, `headers`, `cookies`, `latency_ms`, `error`, `timestamp` |

## Design Decisions & Trade-offs

- **Single source of logic** — the parsers/URL builders/regex in `domain` are used by the engine, services, CLIs, and tests; the application orchestration depends on ports instead of importing adapters.
- **Shims, not a rewrite** — the repo is not a git repository; re-export shims carry zero risk for all legacy consumers (36+ legacy tests pass unchanged).
- **Browser-profile constants in domain** — `CHROME_UA`/`STEALTH_INIT_SCRIPT`/`VIEWPORTS`/`DIRECT_KEY` sleep in `pagination_rules` in the name of a single source of truth; only the engine reads them. A pragmatic trade-off: domain should be pure, but verbatim > purity at this point.
- **Conductor cycle** — free Indonesian proxies are short-lived (pass validation → die within 3–15 minutes), so a single pass yields only 15–30 pages; the [harvest → refresh → merge → resume] cycle automatically closes the gap up to 499 pages. Each round is isolated: fresh browser, fresh pool, clean exit.
- **499 = site cap** — `last_page=499` with `capped_at_site_limit: true` is the site's limit, **not** the real end (~9,980 rulings). Full coverage is only achievable via `--scope courts`.
- **Resumable per file** — valid files (`is_usable` ≥5 KB + `is_valid_listing`) are skipped; invalid files are refetched; writes are atomic.
- **Retirement rule (rotator)** — `consecutive_failures >= MAX_PROXY_FAILURES`; a success resets the counter. Flaky proxies are not retired immediately.
- **Two-dimensional GeoIP** — `origin_country_code` (egress) drives `is_indonesian`; `proxy_country_code` is the proxy's own identity. Transparent proxies that leak the real IP are caught this way.
- **Validation ≠ browsing** — an httpx prefilter does not replace Chromium for Cloudflare-protected sites; never gate a pool against a 403 target with a plain HTTP client.

## Extension Points

- **New parser / markup change** — edit `putusan3/domain/pagination_rules.py` only; offline parser tests live in `tests/test_harvest.py`.
- **New use case (stage 2: parse details → CSV)** — add a service in `application/`, use `Fetcher` + `run_pool` + `page_store`; expose it via `presentation/cli.py` (`python -m putusan3 <cmd>`).
- **New proxy source** — subclass `ProxySource` in `infrastructure/sources.py`, register in
  `create_sources()`; add the URL to `config._DEFAULT_PROXY_SOURCES` (default: 4 lists —
  proxyscrape, proxy-list.download, 2× GitHub raw; GeoNode/FreeProxyListNet classes ship but
  are only active when their URL is configured, e.g. via `PROXY_SOURCES`).
- **Local GeoIP** — replace `_lookup_countries()` in `infrastructure/validator.py` (MaxMind mmdb).
- **Rotation strategy** — `get_proxy(strategy=)` and now also `ProxyVisitor.visit(strategy=)` support round_robin/random/best_latency.
