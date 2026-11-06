# Project State Snapshot

> Snapshot: 2026-10-28 (QA pass #3 'finishing': backlog engine + hygiene)
> Proyek: Indonesian Proxy Scraper (Putusan3) — Python + Playwright, proxy rotasi Indonesia gratis

## Kondisi data terkini (2026-10-28)

- Tahap-1 tahun 2026 **lengkap**: 499/499 halaman listing, 9.337 putusan (summary.json).
- Tahap-2 detail **idle & resumable di 266/9.337** detail HTML; TIDAK ada proses
  python aktif ditemukan saat snapshot. Lanjutkan dengan:
  `python scripts/putusan3_harvest.py --year 2026 --details-only --workers 4`
  (log `logs/details_run.log|.err`), lalu `--parse-only` untuk CSV inventaris.

## QA pass #3 'finishing' (2026-10-28)

Semua diverifikasi: pytest 95/95 default (+8 integration), ruff bersih, mypy
bersih. Backup pra-perubahan: `backup/putusan3_pre_final_pass_20261028_*.zip`.

1. **Backlog ditutup**: `ProxyVisitor.visit()` kini menerima `strategy=`
   (`round_robin` default, `random`, `best_latency`) via `_pick_proxy()` —
   vokabulari identik dengan `ProxyPoolManager.get_proxy()`.
2. **Backlog ditutup**: timeout fetch daftar sumber proxy bukan lagi pinjaman
   `VALIDATION_TIMEOUT` — variabel `SOURCE_FETCH_TIMEOUT` (detik, default
   15 = VALT/1000) di `putusan3.config`, dipakai `ProxySource.timeout`.
3. **Sumber proxy dedup**: `ProxyscrapeSource`/`ProxyListDownloadSource`/
   `GitHubRawSource` (fetch() identik 3×) kini subclasses dari
   `PlainListSource`; nama kelas tetap diekspor (shim & test aman).
4. **Hygiene**: `validator.__main__` import self-package (bukan `proxy.sources`);
   no-op `data["status_code"] = data.get("status_code")` di `results_store`
   dihapus; `visitor.stop()` browser/playwright close dibungkus try/except;
   `import json` pindah ke level modul.
5. **Ketahanan tulis**: `pagination.json` (dibaca konduktor lintas-proses),
   `courts.json` (2 lokasi), `summary.json` kini ditulis via `safe_write`.
6. **Kemasan**: `putusan3/py.typed` + `[tool.setuptools.package-data]` — paket
   punya penanda typed untuk konsumen wheel.
7. **Konfig tooling**: `opencode.json` → v3.0.0, task pool → `data/pools/`,
   skills → path modul `putusan3/`, `lint` → `mypy putusan3`, mojibake diperbaiki.
8. **Docs sync**: ARCHITECTURE (hapus `listing_path` basi, Extension Points
   strategy), WORKFLOW (bagian tahap-2 "belum digarap" → sudah hidup), README
   typo "Details-only used", AGENTS (row tahap-2 di Key Commands, env baru,
   typo "harvst").

Regresi baru: dispatch strategy `visit()` (3 test), default
`SOURCE_FETCH_TIMEOUT`.

## Tahap-2: detail harvest 2026 (2026-10-27)

- Listing 2026 **lengkap di disk**: 499/499 halaman, **9.337 putusan** (parsisi `local_rows`).
- **Fitur CSV baru** (`putusan3_<year>_links.csv`, kolom `nomor,title,url_pdf,url_zip`):
  - `domain.parse_nomor()/nomor_from_text()` — ekstrak nomor setelah marker "Nomor",
    batas: ganda-spasi (batas tag) / stopword (tentang, dsb); wajib mengandung digit.
  - `Harvest.parse_all()` mengisi `rec["nomor"]` (detail body → fallback title listing);
    `Harvest.write_results()` menulis CSV (csv+StringIO, `lineterminator="\n"` —
    `write_text` Windows translate jadi `\r\n`; JANGAN `"\r\n"` eksplisit → dobel CRLF).
  - CLI `--parse-only` & final print menampilkan baris `CSV inventory : N rows -> ...`.
- **BUG LATEN DITEMUKAN & DIPERBAIKI**: flag `--details-only` dulu TIDAK PERNAH
  meng-fetch detail — blok fetch hanya dijalankan bila `not details_only`, jadi
  flag tersebut diam-diam turun kelas jadi `--parse-only` (terbukti: run "details"
  selesai 12 detik, 0 detail tersimpan). Kini `harvest_cli.run()` punya cabang
  eksplisit: details-only → `fetch_details` via Fetcher. 2 test regresi ditambahkan.
- **Run hidup berjalan** (PID berubah per sesi; log `logs/details_run.log|.err`):
  `python scripts/putusan3_harvest.py --year 2026 --details-only --workers 4`
  — resumable per file detail; CSV + summary ditulis ulang di akhir tiap run.
  Monitor: `Get-Content logs\details_run.log -Tail 5` + hitung
  `(Get-ChildItem data\harvest\2026\details -Filter detail_*.html).Count`.
- **CLI modern kini lengkap**: `python -m putusan3 pages|conduct|harvest` —
  rute `harvest` baru menjembatani harvest_cli (details-only/parse-only/CSV) ke
  CLI satu pintu; `harvest_cli.main(argv)` kini menerima argv. Dead code sweep
  final: `page_store.listing_path` dihapus (duplikat naming; test kini cek via
  service), vulture `--min-confidence 80` bersih. README.md di-rewrite penuh ke
  workflow dua-tahap + CSV. QA gate: 91/91 default + 8 integration, ruff bersih,
  mypy bersih.
- QA gate: 88/88 default + 8 integration, ruff bersih, mypy bersih.

## QA pass #2 (2026-10-27)

Paket perbaikan pasca-review QA senior (semua diverifikasi: pytest 84/84 default
+ 8 integration, ruff bersih, mypy bersih):

1. **Dep bug**: pin `httpx>=0.28.0` (requirements + pyproject). `validator.py`
   memakai argumen `proxy=` yang hanya ada di 0.28+; pin lama `>=0.25.0`
   memumbahkan install lama ke crash runtime.
2. **Atomik**: `rotator.save_pool()` kini pakai `safe_write` (`.part` → rename) —
   konduktor mem-merge pool tiap putaran, tulis penuh dulu rawan korupsi.
3. **Validator**: seed shuffle dinamis (dulu `Random(42)` — subset uji statis);
   `_get_real_ip` diberi lock (dulu bisa fetch ganda saat race).
4. **Results store**: eksportir JSON/CSV serial via `threading.Lock`
   (read-modify-write dulu bisa saling menimpa).
5. **Default satu sumber**: `DEFAULT_YEAR=2026` & `DEFAULT_POOL` hanya di
   `putusan3.domain.pagination_rules`; `VALIDATION_TEST_URL` default
   `http://httpbin.org/get` di `putusan3.config` — override manual konduktor
   dihapus; default legacy CLI `load`/`save` dari `"proxies.txt"` /
   `"data/pools/proxies.txt"` → `DEFAULT_POOL`.
6. **Konduktor**: target kelulusan kini dari `pagination.json`
   (`expected_last_page()`, cap 499) — tahun <499 halaman tidak lagi terjebak
   30 putaran refresh; tetangga: `pages_complete(..., last_page=)`.
7. **Breadcrumb**: `PagesRun` kini menangkap meta fetch halaman-1 ke
   `pagination.json["first_page_meta"]` (dulu selalu `{}`); `_page_path`
   dipublikasikan jadi `page_path`.
8. **Dead code/stale**: `run_pool` duplikat di `playwright_engine` dihapus
   (shim `scripts/putusan3_harvest.py` ekspor `run_bounded` sebagai `run_pool`);
   `LOG` duplikat di `pagination_rules` dihapus; UA visitor Chrome 119/120
   yang basi diganti — list kini dikawinkan ahli dengan `CHROME_UA` (Chrome/131,
   proven lolos Cloudflare); `visit_with_retry` tak lagi memperlakukan
   `max_retries=0` sebagai falsy-default; `_visit_locks` dibersihkan saat
   context proxy ditutup.
9. **Hygiene**: `test_visitor_context_manager` (Peluncur Chromium asli) kini
   `@pytest.mark.integration`; artefak root `proxy_pool_r7.txt` dipindah ke
   `data/pools/archive/proxy_pool_r7_root_snapshot_20261027.txt`;
   `.gitignore` + `/proxy_pool*.txt`; default screenshot
   `scripts/putusan3_access.py` → `diagnostics/`.

Regresi baru ditambahkan: `expected_last_page` (4 kasus), penangkapan
`first_page_meta`, kontrak `write_pagination` baru.

## Status terkini (2026-10-26)

- **Reorganisasi workspace SELESAI (2026-10-26 siang)**: root dibersihkan; **semua hasil run kini di direktori khusus** — pool `data/pools/` (utama `proxy_pool.txt`, batch segar `proxy_pool_rN.txt`, lama di `archive/`), hasil harvest `data/harvest/<year>/`, log konduktor `logs/`, artefak diagnostik `diagnostics/`, arsip kode `backup/`. Cache/build (`__pycache__`, `.pytest_cache`, `.ruff_cache`, `.mypy_cache`, `build/`, `*.egg-info`) dihapus. Default code path diperbarui: `DEFAULT_POOL=data/pools/proxy_pool.txt`, out-dir default `data/harvest/<year>` (semua service), refresh-batch konduktor menulis ke `data/pools/`, `save_pool()` & `save` CLI auto-mkdir. `.gitignore` baru.
- **Conductor restart di layout baru**: distop (resumable), di-launch ulang detached dengan `--pool data\pools\proxy_pool.txt --out-dir data\harvest\2026`, log ke `logs/conductor_run.log` — resume terverifikasi: langsung **139/499 halaman** tersimpan saat round-1 deteksi.
- **QA pass 'release-ready' SELESAI (2026-10-26)**: `ruff check .` bersih, `mypy putusan3` bersih (41 error → 0), wheel build terverifikasi (`pip wheel .` → `putusan3-3.0.0-py3-none-any.whl`), pyproject dikoreksi (`packages.find where=["."]`, `pytest pythonpath=["."]`, `ruff src=["."]` — semula stale `["src"]`), `README.md` di-rewrite penuh ke generasi `putusan3/`. Bugfix perilaku: (1) `ScrapingResult.extracted` — hasil `extract_fn` dulu dihitung lalu dibuang diam-diam, kini dibawa pulang; (2) `harvest_service._fetch_listing_page` — `rows` di-reuse lintas tipe int/list; (3) `harvest_service.fetch_year_scope` — unpack `html, _ = 3-tuple` akan ValueError saat eksekusi; (4) `python -m putusan3 conduct ...` dulu TypeError karena `conductor_service.main()` tak menerima argv — kini `main(argv: list[str] | None = None)`.
- **Clean-architecture restructure SELESAI**: logika kini di satu tempat `putusan3/{domain,infrastructure,application,presentation}`; semua jalur lama (`main.py`, `proxy/*`, `scraper/*`, `scripts/*`, `config.py`) jadi shim re-export.
- **Backup aman**: `backup/putusan3_code_backup_20261025_2136.zip` (pra-restrukturisasi), `backup/putusan3_after_restructure_20261025_2159.zip`, `backup/putusan3_after_qa_pass_20261026_1135.zip`, `backup/backup_pre_refactor_20261025_185226.zip` (pindahan dari root), `backup/putusan3_pre_di_refactor_20261027_0653.zip`, `backup/putusan3_after_qa_pass2_20261027_1521.zip` (pasca QA pass #2 — snapshot bersih: kode+config+docs saja, tanpa data/logs/artefak root).

### Entanglement engine (perilaku final, terverifikasi hidup)

| Situasi | Perilaku |
|---------|----------|
| Proxy mati (`ERR_TIMED_OUT`, reset) | skip cepat (~≤20 s), langsung proxy berikutnya |
| Proxy lolos validasi | **sticky** — dipakai terus sampai gagal/challenge |
| Challenge Cloudflare | `goto` + tunggu marker listing ≤10 s sebelum validasi |
| 200 tapi invalid | body di-dump ke `out/diag/reject_200_last.html` |
| 429 | jeda 5–8 s sebelum rotasi |
| Pool habis | fail-fast "No live proxies left"; konduktor refresh pool baru lalu resume |

## Snapshot SEBELUM restrukturisasi (2026-10-24, masih valid untuk legacy scraper)
> Catatan path: sejak reorganisasi 2026-10-26, pool utama di `data/pools/proxy_pool.txt`,
> screenshot pindah ke `diagnostics/screenshot_putusan3_direktori.png`,
> dan hasil harvest ke `data/harvest/<year>/`. Perintah di bawah adalah jejak
> verifikasi hidup (path lama saat itu).

## Status: SEMUA FITUR TERVERIFIKASI HIDUP

Pipeline **fetch → validate → rotate → scrape → export** end-to-end berjalan;
akses target utama `putusan3.mahkamahagung.go.id` terbukti tembus Cloudflare.
Suite tes: **36/36 lulus** (`pytest tests/ -q`).

## Capabilities (implemented & live-verified)

| Fitur | Status | Bukti hidup |
|-------|--------|-------------|
| Fetch 5 sumber proxy (dedupe, graceful degradation) | ✅ | 3.4k+ proxy unik/run; 1 sumber 502 ditangani tanpa crash |
| Validasi (latency, anonymity, transparen­cy, GeoIP) | ✅ | batch ip-api.comPOST `/batch` chunked ≤100 IP; nested fallback per-IP |
| Dua dimensi geo: `origin_country_code` (exit) vs `proxy_country_code` (identitas) | ✅ | proxy MY/Eg yang " menyamar Indonesia" via transparent proxy otomatis difilter |
| Deteksi transparansi (`origin == real client IP`) | ✅ | real client `103.157.58.88` (ID) darurat bukti |
| Rotator round-robin + health + retirement consecutive-failure | ✅ | 3 terbukti pensiun/jalan normal |
| Auto-refresh (min_pool_size=5, interval 300s) + periodic task | ✅ | periodic task start tanpa initial fetch (mode load) |
| Playwright stealth (Chrome UA, wd-undefined, locale id-ID, tz Jakarta) | ✅ | header `Accept-Language: id-ID` di httpbin |
| Retry backoff + delay acak 2–10s (rate limiting) | ✅ | log visit retry melintasi banyak pool |
| Ekspor result JSON/CSV (`RESULTS_PATH`) | ✅ | `test_results.json` — sukses+gagal sama-sama terpred |
| Persistensi pool teks (`save`/`load`, `PROXY_SAVE_PATH`, `PROXY_LOAD_PATH`) | ✅ | `proxy_pool.txt` restore <1s tanpa jaringan; visit sukses via pool loaded |
| Akses target putusan3 via Chromium | ✅ | HTTP 200, title "Direktori Putusan", 157.2k chars, screenshot |

## Artifacts di workspace

| File | Isi |
|------|-----|
| `proxy_pool.txt` | pool valid (1 header `#` + `ip:port` per baris) |
| `screenshot_putusan3_direktori.png` | bukti akses sukses target utama (full-page) |

## Perintah CLI yang tervalidasi

```
python main.py save proxy_pool.txt   # validasi → tulis snapshot teks (exit 1 bila 0 pemenang, normal)
python main.py load proxy_pool.txt   # isi pool dari teks (tanpa jaringan)
PROXY_LOAD_PATH=proxy_pool.txt python main.py test     # pool dari teks → scrape instan
PROXY_SAVE_PATH=proxy_pool.txt python main.py test-all # setiap refresh auto-simpan
```

### Script independen (tanpa dependensi opencode)

`scripts/putusan3_access.py` — single-file, hanya `playwright`:
`python scripts/putusan3_access.py --screenshot out.png --html out.html --json result.json`
Exit 0 = "ACCESS VERIFIED", exit 1 = gagal semua kandidat. **Live-verified**: HTTP 200
"direktori" via `103.189.251.19:8080` (43.8 s, 136.8k chars, `putusan3_direktori_live.png`).

## Bug fix: `save` mengabaikan `PROXY_LOAD_PATH` (fixed 2026-10-24)

`initialize()` dulu hanya memproses `PROXY_LOAD_PATH` di dalam `if refresh:`,
padahal `save` berjalan dengan `refresh=False` → pool kosong → exit 1 nondeterministik.
Kini loading pool `PROXY_LOAD_PATH` terjadi **apa pun nilai `refresh`**, dan
`save` menambah `allow_empty=(command == "save")` agar tidak digate pool-sehat.
Terverifikasi: `PROXY_LOAD_PATH=proxy_pool.txt python main.py save` → exit 0 "Saved 6 proxies"; `load` → exit 0.

## Konfigurasi yang terbukti bekerja (untuk akses putusan3)

```powershell
$env:VALIDATION_TEST_URL = 'http://httpbin.org/get'   # /get (echo headers) untuk cek anonimitas; HTTP bukan HTTPS (proxy gratis kebanyakan http-only)
$env:MAX_PROXIES_TO_TEST = '100'
$env:MAX_RETRIES         = '6'
$env:RESULTS_PATH        = 'test_results.json'        # opsional
```

## Insight penting (disimpan agar tidak hilang)

1. **`putusan3.mahkamahagung.go.id` di belakang Cloudflare** — HTTP client murni (`httpx`) dapat **403** bahkan tanpa proxy; Chrome UA via Chromium **lolos tanpa framework anti-bot**
2. **Proxy free churn cepat** — proxy tervalidasi bisa mati dalam beberapa detik/menit (<50% sudah masih hidup saat browser test); rancangan sempat salah asumsi dengan httpx prefilter
3. **Proxy-list.download 502 sejak beberapa hari** — sumber lain menutupi; graceful degradation bekerja
4. **Proxy transparan menipu filter geo** — proxy Egypt/MY bisa dirappe "Indonesia" jika geo dicek ke *egress origin* (byte mereka menutupi memakai IP komputer yang memang ID); solusi: `exclude_transparent` + geo dua-dimensi
5. **Semua sebagian teks bisa dikonsumsi** — `ip:port` per baris cukup untuk `curl -x`, browser, satu sama lain

## Sisa backlog (opsional, belum digarap)

- Swap ip-api.com → geo DB lokal (maxmind mmdb) untuk produksi
- [Ditutup QA #3 2026-10-28] `ProxyVisitor.visit(strategy=)` — kini didukung
- [Ditutup QA #3 2026-10-28] Timeout fetch sumber — kini `SOURCE_FETCH_TIMEOUT`
- [Tersirat selesai tahap 1–2] Scraper spesifik putusan3 — hidup di
  `putusan3.domain.pagination_rules` + `Fetcher` (listing, detail, CSV)
