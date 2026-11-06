# Architecture

Dokumen ini menjelaskan arsitektur sistem: layer, modul, alur data, dan trade-off. Ditulis ulang setelah restrukturisasi clean-architecture (2026-10-25) — logika kini hidup di satu tempat: paket `putusan3/`.

## Layout: Ports & Adapters

```
putusan3/
├── config.py                              # kopi verbatim dari config lama (path-free)
├── domain/                                           # PURE — tanpa I/O, tanpa playwright
│   └── pagination_rules.py               # konstanta+regex situs, URL builders,
│                                         # parser (parse_listing, parse_last_page,
│                                         # is_challenge, is_valid_*), CHROME_UA dll
├── infrastructure/                                   # ADAPTERS — efek samping dunia nyata
│   ├── playwright_engine.py              # load_pool/load_courts_file (teks pool),
│   │                                     # Fetcher (1 Chromium, 1 Context/proxy,
│   │                                     # sticky-success, fast-skip mati, watchdog),
│   │                                     # run_pool (worker pool terbatas)
│   ├── page_store.py                     # safe_write/is_usable/manifest_log
│   ├── sources.py                        # ← proxy/sources.py (5 sumber proxy gratis)
│   ├── validator.py                      # ← proxy/validator.py (validasi+GeoIP batch)
│   ├── rotator.py                        # ← proxy/rotator.py (round-robin+health)
│   ├── visitor.py                        # ← scraper/visitor.py (ProxyVisitor umum)
│   └── results_store.py                  # ← scraper/results.py (ekspor JSON/CSV)
├── application/                                      # USE-CASES — orkestrasi layer di atas
│   ├── ports.py                           # kontrak fetcher, storage, pool loader, process runner
│   ├── async_tasks.py                     # bounded worker runner tanpa adapter
│   ├── harvest_pages_service.py          # tahap 1: tahun → HTML tiap halaman listing
│   ├── harvest_service.py                # Harvest: listing+detail+parse offline (penuh)
│   └── conductor_service.py              # siklus [harvest → refresh-pool → merge → resume]
└── presentation/                                     # CLI
    ├── cli.py                            # python -m putusan3 pages|conduct
    ├── bootstrap.py                      # composition root: bind port ke adapter konkret
    ├── harvest_cli.py                    # build_parser/run/main CLI harvest lama
    └── legacy_cli.py                     # CLI proxy generik (launcher main.py)
```

## Dependency inversion

Use case di `application/` hanya bergantung pada `domain/` dan kontrak di
`application/ports.py`. Ia tidak mengimpor Playwright, filesystem adapter,
proxy source, atau subprocess implementation. `FetcherPort`, `FetcherFactoryPort`,
`PageStorePort`, `PoolLoaderPort`, dan `CommandRunnerPort` memungkinkan use case
diuji memakai fake/in-memory adapter tanpa jaringan, browser, atau proses nyata.

Adapter konkret berada di `infrastructure/`: `PlaywrightFetcherFactory`,
`LocalPageStore`, `FilePoolLoader`, dan `SubprocessCommandRunner`. Composition
root di `presentation/bootstrap.py` mengikat adapter tersebut sebelum memanggil
use case. `presentation/harvest_cli.py` adalah entry point kompatibilitas dan
melakukan komposisi yang sama untuk alur harvest lama.

```text
presentation/bootstrap ──> application use cases ──> application ports
          │                         │
          └── infrastructure adapters ──> implement application ports
domain <──────────────── application
```

Dengan demikian arah source dependency tidak pernah mengarah dari application
ke infrastructure. Tes `test_application_layer_does_not_import_infrastructure`
menjaga batas ini dari regresi.

### Kompatibilitas (shim pasca-restrukturisasi)

Semua entry point lama tetap berjalan tanpa perubahan perilaku:

| Jalur lama | Sekarang |
|------------|----------|
| `main.py` (CLI scraper proxy) | launcher tipis ke `presentation/legacy_cli.py` |
| `config.py`, `proxy/*`, `scraper/*` | shim re-export ke `putusan3.config` / `putusan3.infrastructure.*` |
| `scraper/visitor.py` | alias `sys.modules` → `putusan3.infrastructure.visitor` (supaya `mock.patch("scraper.visitor.X")` tetap bekerja) |
| `scripts/putusan3_harvest.py` | shim re-export penuh (test lama: `harvest.parse_*`, `harvest.Harvest`, `harvest.safe_write`) |
| `scripts/putusan3_pages.py`, `scripts/putusan3_conductor.py` | launcher tipis → composition root di `putusan3.presentation.bootstrap` |
| `python -m putusan3 pages ...` | entry point baru (presentation/cli.py) |

## Alur Tahap 1 (harvest halaman listing tahun)

```
            ┌──────────────────────────────────────────────────────────────┐
            │ conductor (application/conductor_service.py)                 │
            │   loop: pages → refresh-pool (main.py save) → merge → resume │
            └──────────────┬───────────────────────────────────────────────┘
                           │ subprocess terisolasi
                           ▼
        ┌── harvest_pages_service (application) ────────────────────────┐
        │  PoolLoaderPort → FetcherFactoryPort → FetcherPort            │
        │  page 1 → pagination.json (last_page, capped_at_site_limit)   │
        │  pages 2..last paralel (run_pool, workers=3)                  │
        │  tiap halaman: cache? → skip : fetch → safe_write             │
        └───────┬───────────────────────────────┬──────────────────────┘
                │ domain                        │ infrastruktur
                ▼                               ▼
  putusan3/domain/pagination_rules   infrastructure adapter implements ports
  - year_listing_url(year, page)     - Fetcher.get(): max_attempts ≤24, sticky
  - parse_last_page (data-ci-…)      - fast-skip proxy mati (~≤20s/attempt)
  - is_valid_listing                 - goto + wait_for_selector marker (10 s)
  - pagination 499 = cap situs       - 200-invalid → dump diag/reject_200_last.html
                                     - 429 → jeda 5–8 s; retire 2 gagal beruntun
                                     ▼
                      putusan3/infrastructure/page_store
                      - list_NNNN.html (atomik: .part → replace)
                      - manifest.jsonl (JSONL ts+meta)
                      - pagination.json
```

### Kunci perilaku Fetcher (terverifikasi hidup 2026-10-25)

1. **Proxy mati → langsung skip** — `ERR_TIMED_OUT` dsb terdeteksi ~9 s; tidak ada perpanjangan waktu untuk membuktikan proxy mati. `finally:` selalu mengembalikan lease ke antrian kecuali proxy sudah direst.
2. **Proxy lancar → sticky** — proxy yang lolos `validator` dipakai berulang (log `(sticky)`) sampai gagal/challenge; baru rotasi.
3. **Watchdog keras** — `asyncio.wait_for(timeout + 18 s)` menjamin satu attempt tidak pernah menggantung lebih lama itu (goto 20 s + wait-sel 10 s + buffer).
4. **200-tapi-invalid → dump** — body disimpan `out/diag/reject_200_last.html` untuk diagnosis (jangan menebak markup).
5. **429 → jeda 5–8 s** sebelum rotasi; 403/503 = challenge `is_challenge` → skip cepat.
6. **Pool habis → "No live proxies left"** naik ke service; konduktor menyiapkan pool baru lalu resume.

## Alur legacy scraper proxy (masih hidup via shim)

`main.py` CLI (`test|test-all|continuous|list-proxies|save|load`) tetap bekerja: `ProxyPoolManager` → `fetch_all_proxies` (5 sumber httpx) → `validate_proxies` (anonymity/transparency/GeoIP-batch ≤100 IP/POST) → `get_best_proxies(ID, anonymous)` → `ProxyRotator` round-robin → `ProxyVisitor` (Playwright, context per proxy, stealth UA+locale id-ID+tz Jakarta) → `export_results` (RESULTS_PATH).

Detail penuh alur ini (seq startup/visit/retry) dipindahkan tanpa perubahan — lihat git history zip `backup/putusan3_code_backup_*.zip` bila perlu versi sebelum restrukturisasi.

## Tipe Data Inti

| Tipe | Lokasi | Berisi |
|------|--------|--------|
| `ProxyValidationResult` | `infrastructure/validator.py` | `is_working`, `latency_ms`, `is_anonymous`, `is_transparent`, `is_indonesian` (berdasar egress), `origin_ip` + `origin_country_code`, `proxy_country_code`, `error`, `response_headers` |
| `ProxyHealth` | `infrastructure/rotator.py` | `successes`, `failures`, `consecutive_failures`, `avg_latency_ms`, `is_retired`, `validation_result` |
| `ScrapingResult` | `infrastructure/visitor.py` | `url`, `proxy`, `success`, `status_code`, `content`, `headers`, `cookies`, `latency_ms`, `error`, `timestamp` |

## Keputusan Desain & Trade-off

- **Logika satu sumber** — parser/URL/regex di `domain` dipakai engine, service, CLI, dan test; application orchestration memakai port alih-alih import adapter.
- **Shim, bukan rewrite** — repo bukan git; shim re-export berisiko nol untuk semua konsumen lama (36+ test lama lulus tanpa perubahan).
- **Browser-profile konstanta di domain** — `CHROME_UA`/`STEALTH_INIT_SCRIPT`/`VIEWPORTS`/`DIRECT_KEY` tidur di `pagination_rules` demi satu sumber kebenaran; hanya engine yang membaca. Trade-off pragmatis: domain harusnya pure, tapi verbatim > purity di titik ini.
- **Konduktor siklus** — proxy gratis Indonesia umur pendek (lulus validasi → mati 3–15 menit), jadi satu pass hanya 15–30 halaman; siklus [harvest → refresh → merge → resume] otomatis menutup gap sampai 499 halaman. Setiap putaran isolat: browser baru, pool baru, exit bersih.
- **499 = cap situs** — `last_page=499` dengan `capped_at_site_limit: true` adalah batas situs, **bukan** akhir sebenarnya (~9.980 putusan). Coverage penuh hanya via `--scope courts`.
- **Resumable per file** — file valid (`is_usable` ≥5 KB + `is_valid_listing`) di-skip; file tak valid di-refetch; tulis atomik.
- **Retirement rule (rotator)** — `consecutive_failures >= MAX_PROXY_FAILURES`; sukses mereset. Proksi flaky tidak langsung dipensiunkan.
- **GeoIP dua dimensi** — `origin_country_code` (egress) dasar `is_indonesian`; `proxy_country_code` identitas proxy. Proxy transparan yang mengusak real-IP terdeteksi.
- **Validasi ≠ browsing** — httpx prefilter tidak menggantikan Chromium untuk situs ber-Cloudflare; jangan pernah memfilter pool terhadap target 403 dengan HTTP client murni.

## Extension Points

- **Parser baru / markup berubah** — edit `putusan3/domain/pagination_rules.py` saja; test parser offline di `tests/test_harvest.py`.
- **Use-case baru (tahap 2: parse detail → CSV)** — tambah service di `application/`, pakai `Fetcher` + `run_pool` + `page_store`; expose via `presentation/cli.py` (`python -m putusan3 <cmd>`).
- **Sumber proxy baru** — subclass `ProxySource` di `infrastructure/sources.py`, daftar di `create_sources()`.
- **GeoIP lokal** — ganti `_lookup_countries()` di `infrastructure/validator.py` (MaxMind mmdb).
- **Strategi rotasi** — `get_proxy(strategy=)` dan kini juga `ProxyVisitor.visit(strategy=)` mendukung round_robin/random/best_latency.
