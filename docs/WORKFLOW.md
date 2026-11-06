# Workflow Putusan3 — Stage 1 (pages) & Stage 2 (details + CSV inventory)

> Stage 1 and 2 are both LIVE (stage 2 since 2026-10-27). Stage 1 collects the
> listing HTML pages; stage 2 fetches each decision's detail HTML and the
> offline parse turns it all into the CSV inventory (`nomor,title,url_pdf,url_zip`).

## Flow summary

1. **Enumerate Indonesian proxies → save to text**
   ```powershell
   python main.py save data/pools/proxy_pool.txt
   ```
   Fetch the 4 default proxy-source URLs → validate (connectivity, anonymity,
   transparency, Indonesian GeoIP-egress) → write an `ip:port` snapshot, one per line, to
   `data/pools/proxy_pool.txt`.

2. **Access `putusan3.mahkamahagung.go.id/direktori` for year 2026 via the proxy pool**
   Route in use (server-rendered, passes Cloudflare via Chromium+Chrome UA):
   ```
   /direktori/index/tahunjenis/putus/tahun/2026.html          → page 1
   /direktori/index/tahunjenis/putus/tahun/2026/page/N.html   → page N
   ```

3. **Infer the pagination count**
   Page 1 contains the pagination widget (`data-ci-pagination-page`).
   The last point ("Last") = the number of pages; the site caps it at 499
   pages (~9,980 putusan). The actual count is also saved as
   `pagination.json` in the output folder (see step 4).

4. **Save the HTML of every page to `data/harvest/<year>/`**
   ```powershell
   # Single pass (resumable; typically gets 15-30 pages before the pool runs out)
   python scripts/putusan3_pages.py --year 2026

   # RECOMMENDED: the conductor that cycles [harvest → refresh-pool-fresh → merge → resume]
   # until ALL pages (site: max 499) are saved — start it and let it finish on its own
   python scripts/putusan3_conductor.py --year 2026
   ```
   Defaults already point to dedicated directories: pool `data/pools/proxy_pool.txt`,
   results `data/harvest/2026/` (explicit `--pool`/`--out-dir` paths still work).
   Results in `data/harvest/<year>/`:
   ```
   pages/list_0001.html ... list_0499.html   HTML of each listing page
   pagination.json                           {year, base_url, last_page, capped_at_site_limit, pages_on_disk,...}
   manifest.jsonl                            log of every request (url, proxy, status, latency)
   diag/reject_200_last.html                 (if present) 200-invalid body for diagnosis
   ```
   Resumable: already-valid `list_*.html` files are not re-fetched — run it
   again to continue. Rules: one Chromium, one BrowserContext per proxy,
   retry+rotation, random delay 1–3 s, dead proxies skipped immediately,
   healthy proxies kept sticky until they fail.

   > The implementation now lives in the `putusan3/` package (Ports & Adapters);
   > `scripts/putusan3_pages.py` & `scripts/putusan3_conductor.py` are just
   > thin launchers. Equivalent modern entry points:
   > ```powershell
   > python -m putusan3 pages   --year 2026
   > python -m putusan3 conduct --year 2026
   > ```
   > Architecture details: `docs/ARCHITECTURE.md`.

   Other options from the same engine (`scripts/putusan3_harvest.py`):
   | Purpose | Additional flag |
   |---------|-----------------|
   | Smoke test 3 pages | add `--max-pages 3` |
   | From a specific page | add `--from-page K` |
   | Show the browser | add `--headed` |

## Running detached (Windows) — let the conductor run on its own

```powershell
# work from the repo root; write logs to logs/
Start-Process -FilePath python `
  -ArgumentList 'scripts\putusan3_conductor.py','--year','2026' `
  -WorkingDirectory (Get-Location) -WindowStyle Hidden `
  -RedirectStandardOutput 'logs\conductor_run.log' `
  -RedirectStandardError 'logs\conductor_run.err'
```

Check progress at any time (no need to interrupt the process):
```powershell
(Get-ChildItem data\harvest\2026\pages -Filter list_*.html).Count
Get-Content logs\conductor_run.log -Tail 5
```

## Next phase (DONE — phase 2 is live since 2026-10-27)

Stage 1 output → detail HTML of each putusan → extraction of `nomor`+link
`/direktori/download_file/<md5>/(pdf|zip)/<kode>` → offline inventory CSV,
everything already works (see README "The Harvest Workflow"):

```powershell
python -m putusan3 harvest --year 2026 --details-only   # phase 2: detail HTML (resumable)
python -m putusan3 harvest --year 2026 --parse-only     # offline -> CSV/txt/jsonl
```

Output: `data/harvest/<year>/putusan3_<year>_links.csv`
(columns `nomor,title,url_pdf,url_zip`) + `_pdf_urls.txt`/`_zip_urls.txt`/
`_detail_urls.txt`/`_details.jsonl`/`summary.json`.
