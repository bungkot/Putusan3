# Indonesian Proxy Toolkit & Supreme Court Data Harvester

> ⚠️ **DISCLAIMER — EDUCATION & LEARNING ONLY**
>
> This project was built **strictly for learning, research, and educational
> purposes** — to understand data-harvesting techniques, proxy rotation,
> Cloudflare, and Playwright. This code is **not intended to damage systems or
> to be abused** in any way:
>
> - Do **not** use it for attacks, abuse, DoS/DDoS, credential/data theft, or
>   any form of sabotage against any website or system.
> - Scrape politely: respect rate limits, use random delays, don't rampage,
>   and keep the target site healthy and online.
> - Comply with applicable law (in Indonesia: the ITE Law and Supreme Court
>   regulations), website terms of service, `robots.txt`, and good research
>   ethics.
> - **Written permission is required**: before targeting someone else's
>   system, data collection must be backed by **written permission or consent**
>   from the system's owner/operator. Acting without permission is entirely at
>   your own risk.

Python + Playwright toolkit with two layers:

1. **Indonesian proxy engine** — fetch from 4 free proxy lists, validate via GeoIP
   (egress country = `ID`), rotate with health tracking, keep the pool as a plain
   text snapshot (`ip:port` lines).
2. **putusan3 harvester** — uses that pool to farm the Indonesian Supreme Court
   decision website [`putusan3.mahkamahagung.go.id`](https://putusan3.mahkamahagung.go.id),
   which sits behind **Cloudflare**. Plain HTTP clients are rejected with 403;
   Chromium with a Chrome user agent passes. Stage 1 saves every listing page of a
   decision year as HTML; stage 2 visits every listed decision and records its
   PDF/ZIP links into a **CSV inventory** (`nomor,title,url_pdf,url_zip`) — and the
   parse stage itself runs fully offline.

Everything the tools *produce* stays out of your face: **all results, pools and
logs live in dedicated directories** (`data/`, `logs/`, `diagnostics/`) — the repo
root stays clean with only code + config.

> [MIT License](LICENSE) © 2026 Hendra Dwi Saputra — see [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for full internals,
> [docs/WORKFLOW.md](docs/WORKFLOW.md) for the operational runbook, and
> [docs/STATE.md](docs/STATE.md) for the current verified state.

## Features

- 🇮🇩 **Indonesian filtering** — validated by *egress* GeoIP (`ip-api.com` batched
  lookup) + transparency detection; transparent proxies that echo your real IP
  never poison the pool
- 🕸️ **Cloudflare-aware Playwright** — Chromium with Chrome UA, `navigator.webdriver`
  patch, `id-ID` locale, Jakarta timezone, valid-listing fingerprint checks
- 🔁 **Smart rotation** — sticky-success proxying (reuse a working proxy until it
  fails), fast-skip of dead proxies, consecutive-failure retirement, hard watchdog
- 💾 **Text pool snapshots** — `ip:port` lines that the tool, `curl -x`, or any
  browser can consume; restore instantly without revalidating
- ♻️ **Resumable harvest** — listing pages land on disk as
  `data/harvest/<year>/pages/list_NNNN.html`, detail pages as
  `data/harvest/<year>/details/detail_<kode>.html`; reruns skip whatever is
  already usable (files are re-validated, not just size-checked)
- 📄 **CSV decision inventory** — `nomor,title,url_pdf,url_zip`, one row per
  decision; `nomor` is parsed from the detail page (falling back to the listing
  title) by the pure domain parser `parse_nomor()`
- 📁 **Tidy by design** — outputs under `data/harvest/<year>/`, pools under
  `data/pools/`, cycle logs under `logs/`, evidence under `diagnostics/`
- 🏗️ **Clean architecture** — `putusan3/{domain,infrastructure,application,presentation}`
  (Ports & Adapters); every legacy entry point keeps working through shims

## Requirements

- Python 3.10+
- Playwright Chromium: `playwright install chromium`
- Internet access (proxy sources + GeoIP endpoint are external)
- Runtime dependencies: `pip install .` or `pip install -r requirements.txt`
  (`httpx>=0.28.0`, `playwright>=1.40`, `beautifulsoup4`, `lxml`, `python-dotenv`)
- Development dependencies: `pip install -r requirements-dev.txt`

## The Harvest Workflow (two stages + offline parse)

Everything targets `data/` by default — pool `data/pools/proxy_pool.txt`,
output `data/harvest/<year>`:

```bash
# 0. One-time install
pip install -r requirements-dev.txt
playwright install chromium

# 1. (optional) Build an Indonesian proxy pool snapshot first, or let the
#    harvester/legacy CLI validate a fresh batch itself.
python main.py save data/pools/proxy_pool.txt

# 2. STAGE-1 — listing pages of the year (resumable, capped at the site limit)
python -m putusan3 conduct --year 2026        # cycle: fetch → refresh pool → merge → resume

# 3. STAGE-2 — visit every decision of the year, save detail HTML (resumable)
python -m putusan3 harvest --year 2026 --details-only

# 4. PARSE — offline: nomor + title + PDF/ZIP URLs -> CSV (+ txt/jsonl side outputs)
python -m putusan3 harvest --year 2026 --parse-only
```

Steps 2–4 can also run detached (survives closing the terminal), e.g. on Windows:

```powershell
Start-Process -FilePath python -WorkingDirectory 'C:\path\to\Putusan3' -WindowStyle Hidden `
  -RedirectStandardOutput 'logs\conductor_run.log' -RedirectStandardError 'logs\conductor_run.err' `
  -ArgumentList '-u','scripts/putusan3_conductor.py','--year','2026'
```

(The conductor logs to `logs/conductor_run.*`; the same recipe works for stage-2
with `scripts/putusan3_harvest.py --year 2026 --details-only`, pointing the log
redirects at `logs\details_run.*`.)

Detached runs are checked with one-liners:

```powershell
(Get-ChildItem data/harvest/2026/pages -Filter list_*.html).Count      # stage-1 progress
(Get-ChildItem data/harvest/2026/details -Filter detail_*.html).Count # stage-2 progress
Get-Content logs/details_run.err -Tail 5                              # live rotation log
```

Where the results go after any run:

```
data/
├── pools/
│   ├── proxy_pool.txt          # main pool snapshot (`ip:port` lines)
│   ├── proxy_pool_rN.txt       # fresh validation batches (one per conductor round)
│   └── archive/                # superseded snapshots
└── harvest/
    └── 2026/
        ├── pages/list_0001.html ... list_0499.html   # stage-1: one listing page per file
        ├── details/detail_<kode>.html                # stage-2: one decision per file
        ├── courts/                                   # only with --scope courts
        ├── pagination.json      # last page, cap flag, fetched/skipped counts
        ├── putusan3_2026_links.csv    # ★ the CSV inventory (see below)
        ├── putusan3_2026_pdf_urls.txt / _zip_urls.txt / _detail_urls.txt
        ├── putusan3_2026_details.jsonl
        ├── summary.json         # parse statistics
        ├── manifest.jsonl       # timestamped fetch log (JSON lines)
        └── diag/                # dumps of 200-OK-but-invalid responses (rare)
logs/
├── conductor_run.log|.err      # conductor stdout/stderr (cycle progress)
├── details_run.log|.err        # stage-2 detached runs
diagnostics/                    # screenshots, access probes, sample HTML
```

## The CSV Inventory (`putusan3_<year>_links.csv`)

Written by the offline parse stage and re-written on every rerun. One row per
decision known from the listing HTML; URL columns fill in as detail HTML lands
on disk:

| Column | Meaning | Source |
|--------|---------|--------|
| `nomor` | Decision number, e.g. `7598 K/PID.SUS/2026` | parsed from the detail page body via `parse_nomor()`, fallback: the listing title |
| `title` | Listing title as shown on the directory | listing HTML |
| `url_pdf` | Absolute PDF download URL (empty when known/still missing) | detail HTML (`/direktori/download_file/...`) |
| `url_zip` | Absolute ZIP download URL (empty when known/still missing) | detail HTML |

Note: the directory advertises **499 pages as the hard pagination cap** for the
overall `tahun` scope (`capped_at_site_limit` in `pagination.json`); 2026 lists
**9,337 decisions** behind those 499 pages. Full-timeline coverage per year needs
`--scope courts` (per-court listings). The detail stage is the long one — it is
resumable, so rerun the same command until counts stop growing.

## The Modern CLI (recommended)

One package, three commands — all defaults already follow the new layout:

```bash
python -m putusan3 pages --year 2026        # stage-1: one resumable pass
python -m putusan3 conduct --year 2026      # stage-1 cycle loop until all pages on disk
python -m putusan3 harvest --year 2026 ...  # full harvester (listing + details + parse + CSV)
```

| Command | Description |
|---------|-------------|
| `pages [--year ...]` | Stage-1: save listing pages of one year as HTML (resumable) |
| `conduct [--year ...]` | Stage-1 cycle loop until every page is on disk |
| `harvest --details-only` | Stage-2: visit missing detail pages from listings on disk |
| `harvest --parse-only` | Offline parse → CSV inventory + txt/jsonl outputs |
| `harvest --scope courts` | Complete coverage: one listing series per court |
| `harvest --listing-only` | Listing fetch only (skip the detail hop) |
| `putusan3 ...` | The console script — installed by `pip install .` |

Key flags (shared by `pages`/`conduct`; `harvest` adds the stage flags above):

| Flag | Default | Description |
|------|---------|-------------|
| `--year` | `2026` | Decision year to harvest |
| `--pool` | `data/pools/proxy_pool.txt` | Proxy text file (`ip:port` per line, `#` comments ok) |
| `--out-dir` | `data/harvest/<year>` | Output folder (auto-created) |
| `--workers` | `4` | Concurrent fetchers (one proxy each) |
| `--timeout` | `45000` | Per-request timeout (ms) |
| `--dead-after` | `2` | Consecutive failures before a proxy rests |
| `--max-attempts` | auto | Attempts per URL (auto: `2 x pool size`, min 4) |
| `--from-page` / `--max-pages` | `1` / all | Start page / max pages (`--max-pages 3` = smoke test) |
| `--direct` | off | Also try *without* a proxy |
| `--headed` / `--channel chrome\|msedge` | off | Debug the browser / use an installed channel |

Thin launchers (compose the same use-cases — handy for detached `Start-Process`):

| Launcher | Forwards to |
|----------|-------------|
| `python scripts/putusan3_pages.py ...` | the `pages` command |
| `python scripts/putusan3_conductor.py ...` | the `conduct` command |
| `python scripts/putusan3_harvest.py ...` | the `harvest` command |
| `python scripts/putusan3_access.py [--screenshot diagnostics/out.png]` | single-file access/proxy probe |

Run semantics worth knowing:

- **Resumable** — cached `list_NNNN.html` / `detail_<kode>.html` files are
  re-validated (fingerprint + size) and skipped; only invalid/missing pages are
  refetched. Restarting the run (or the whole machine) never loses progress.
- **Pagination is read, not assumed** — page 1 advertises the real last page
  (`data-ci-pagination-page`); 2026 currently reports **499 pages = the site's
  hard cap**.
- **Details-only runs require a browser round-trip** — as of 2026-10-27 the flag
  opens the fetcher and records each detail page; previously it silently
  degraded to `--parse-only` (regression-tested now).

## The Legacy CLI (generic proxy scraper)

The generic scraper (`main.py`) still works and is handy for pool hygiene:

| Command | Description |
|---------|-------------|
| `python main.py test [url]` | Single test visit (exit code `1` on failure — CI friendly) |
| `python main.py test-all` | Test all `TARGET_URLS` concurrently |
| `python main.py continuous [interval]` | Scrape all targets every N seconds (default 60) |
| `python main.py list-proxies` | Print pool stats and per-proxy health |
| `python main.py save [file]` | Save the validated pool to text (default `data/pools/proxy_pool.txt`) |
| `python main.py load [file]` | Fill the pool from text — instant, no network |

Environment variables (all optional, `.env` auto-loaded, OS vars win —
see `.env.example`): `TARGET_URLS`, `PROXY_REFRESH_INTERVAL`, `MAX_CONCURRENT`,
`REQUEST_TIMEOUT`, `VALIDATION_TEST_URL` (default `http://` — most free proxies
are HTTP-only), `MAX_PROXIES_TO_TEST`, `MIN_POOL_SIZE`, `HEADLESS`, `BROWSER_TYPE`,
`USE_GEOIP_FILTER`, `RESULTS_PATH`, `PROXY_SAVE_PATH`, `PROXY_LOAD_PATH`,
`LOG_LEVEL`, `PROXY_SOURCES`.

For the harvest engine itself the pool is everything: point `--pool` at a text
file and it manages rotation, retirement, and merging fresh batches itself.

## Project Structure

```
├── putusan3/                     # ★ the package (single source of truth)
│   ├── domain/                   # pure site rules & parsers (no I/O)
│   │   └── pagination_rules.py   # URL builders, regex parsers, fingerprint
│   │                             #   + nomor/CSV-grade decision-number parsing
│   ├── infrastructure/           # adapters: browser, storage, proxies
│   │   ├── playwright_engine.py  # Fetcher: sticky-success + fast-skip + watchdog
│   │   ├── page_store.py         # atomic writes, resume checks, manifest
│   │   ├── sources / validator / rotator   # proxy pipeline
│   │   └── visitor / results_store         # generic visitor + JSON/CSV export
│   ├── application/              # use-cases
│   │   ├── ports.py              # storage, fetching, pool, and process contracts
│   │   ├── async_tasks.py        # bounded use-case worker runner
│   │   ├── harvest_pages_service.py   # stage-1 pages runner
│   │   ├── harvest_service.py         # listing+detail+offline parse + CSV
│   │   └── conductor_service.py       # [fetch -> refresh -> merge -> resume] loop
│   └── presentation/             # CLI entry points
│       ├── cli.py                # modern dispatch (python -m putusan3)
│       ├── bootstrap.py          # composes application ports and adapters
│       ├── harvest_cli.py        # `harvest` command (also the legacy script CLI)
│       └── legacy_cli.py         # generic proxy scraper CLI
├── proxy/  scraper/              # backwards-compatible shims -> putusan3.*
├── scripts/                      # thin launchers + diagnostics probe
├── main.py  config.py            # compatibility launcher/shim
├── pyproject.toml                # package metadata + dev tooling config
├── requirements.txt              # runtime dependencies
├── requirements-dev.txt          # editable install + test/lint/type-check tools
├── tests/                        # deterministic tests + opt-in network integrations
├── data/                         # ★ every run result lives here
├── logs/                         # detached runs' logs
├── diagnostics/                  # screenshots, probes, sample HTML
├── docs/                         # ARCHITECTURE / WORKFLOW / STATE
└── backup/                       # source snapshots (zip)
```

## Usage Examples

### Stage-1 harvest via the API

```python
from putusan3.presentation.bootstrap import pages_main

pages_main(["--year", "2026", "--max-pages", "3"])
```

### Stage-2 + CSV via the harvest API

```python
import asyncio
import argparse
from putusan3.application.harvest_service import Harvest
from putusan3.infrastructure.page_store import LocalPageStore
from putusan3.infrastructure.pool_loader import FilePoolLoader

args = argparse.Namespace(year=2026, out_dir=None, delay_min=1, delay_max=3,
                          workers=4, max_details=0)
harvest = Harvest(args, store=LocalPageStore(), pool_loader=FilePoolLoader())

parsed = harvest.parse_all()           # offline: nomor/title/pdf/zip per decision
paths = harvest.write_results(parsed)
print(paths["links_csv"])              # data/harvest/2026/putusan3_2026_links.csv
```

### Custom extraction (generic visitor)

```python
import asyncio
from proxy.rotator import ProxyRotator
from scraper.visitor import ProxyVisitor, extract_text

async def main():
    rotator = ProxyRotator()
    await rotator.initialize()
    result = await ProxyVisitor(rotator).visit(
        "https://example.com", extract_fn=lambda page: extract_text(page, "h1"))
    print(result.extracted)          # carried on ScrapingResult

asyncio.run(main())
```

### Proxy credentials

```python
result = await visit_with_proxy(url, "user:password@1.2.3.4:8080")
```

## Testing

```bash
pip install -r requirements-dev.txt
pytest tests/ -q                       # deterministic suite (no external network)
pytest tests/ -m integration -q        # optional live-network/proxy checks
pytest tests/test_proxy.py -q          # offline proxy logic and persistence
pytest tests/test_scraper.py -q        # visitor + extractors (mocked browser)
pytest tests/test_harvest.py -q        # parsers + URLs + offline harvest
pytest tests/test_putusan3_package.py  # package layers: domain/infra/CLI/use-case
ruff check .
mypy putusan3
```

The default test run (100 tests) excludes the 8 `integration`-marked tests; use
`pytest tests/ -m integration -q` when live-network checks (Chromium launch,
proxy validation) are desired. They are not part of the deterministic local QA
run. A 3.4k-proxy refresh is normal — expect <5% of fetched proxies to survive
Indonesian + anonymous validation.

## Troubleshooting

- **Uniform 403 on putusan3** — it is Cloudflare. Never prefilter proxies with
  raw HTTP against that target; access it via Chromium (the engine handles this).
- **Proxies die within seconds** — normal for free lists; the value is in retry
  with rotation + text snapshots (`python main.py save data/pools/proxy_pool.txt`,
  `--pool`).
- **No Indonesian proxies found** — GeoIP checks the *egress* country; expect
  few hits. `USE_GEOIP_FILTER=false` skips geo entirely (not recommended here).
- **SSL errors** — contexts run with `ignore_https_errors=True` by default.
- **A run gets slower over time** — check `python main.py list-proxies`; retired
  proxies need a fresh batch (the conductor writes one to `data/pools/` per round
  and merges it automatically).
- **CSV rows with empty `url_pdf`/`url_zip`** — that decision's detail page has
  not been fetched yet (or genuinely has no download link); rerun stage-2, then
  `--parse-only`.

MIT License.
