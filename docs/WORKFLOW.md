# Workflow Putusan3 — Tahap 1 (Direktori → HTML halaman)

> Tahap ini SENGAJA sederhana: kumpulkan halaman listing HTML saja.
> Parsing/PDF/ZIP/CSV baru menyusul di tahap berikutnya.

## Ringkasan alur

1. **Mendata proxy Indonesia → simpan ke teks**
   ```powershell
   python main.py save data/pools/proxy_pool.txt
   ```
   Fetch 5 sumber → validasi (konektivitas, anonimitas, transparansi, GeoIP-egress
   Indonesia) → tulis snapshot `ip:port` satu per baris ke
   `data/pools/proxy_pool.txt`.

2. **Akses `putusan3.mahkamahagung.go.id/direktori` tahun 2026 via proxy pool**
   Route yang dipakai (server-rendered, lolos Cloudflare via Chromium+Chrome UA):
   ```
   /direktori/index/tahunjenis/putus/tahun/2026.html          → halaman 1
   /direktori/index/tahunjenis/putus/tahun/2026/page/N.html   → halaman N
   ```

3. **Menyimpulkan jumlah pagination**
   Halaman 1 mengandung widget pagination (`data-ci-pagination-page`).
   Titik terakhir ("Last") = jumlah halaman; situs membatasi maksimal 499
   halaman (~9.980 putusan). Jumlah aktual tersimpan juga sebagai
   `pagination.json` di folder output (lihat langkah 4).

4. **Simpan HTML tiap halaman ke `data/harvest/<year>/`**
   ```powershell
   # Satu pass (resumable; biasanya dapat 15-30 halaman sebelum pool habis)
   python scripts/putusan3_pages.py --year 2026

   # REKOMENDASI: konduktor yang muter [harvest → refresh-pool-fresh → merge → resume]
   # sampai SEMUA halaman (situs: max 499) tersimpan — jalankan, biarkan selesai sendiri
   python scripts/putusan3_conductor.py --year 2026
   ```
   Default sudah mengarah ke direktori khusus: pool `data/pools/proxy_pool.txt`,
   hasil `data/harvest/2026/` (path eksplisit `--pool`/`--out-dir` tetap bisa).
   Hasil di `data/harvest/<year>/`:
   ```
   pages/list_0001.html ... list_0499.html   HTML tiap halaman listing
   pagination.json                           {year, base_url, last_page, capped_at_site_limit, pages_on_disk,...}
   manifest.jsonl                            log tiap request (url, proxy, status, latency)
   diag/reject_200_last.html                 (bila ada) body 200-invalid untuk diagnosis
   ```
   Resumable: file `list_*.html` yang sudah valid tidak di-fetch ulang — jalankan
   lagi untuk melanjutkan. Aturan: satu Chromium, satu BrowserContext per proxy,
   retry+rotasi, delay acak 1–3 s, proxy mati langsung skip, proxy lancar dipakai
   sticky sampai gagal.

   > Implementasi kini hidup di paket `putusan3/` (Ports & Adapters);
   > `scripts/putusan3_pages.py` & `scripts/putusan3_conductor.py` hanyalah
   > launcher tipis. Entry point modern yang ekuivalen:
   > ```powershell
   > python -m putusan3 pages   --year 2026
   > python -m putusan3 conduct --year 2026
   > ```
   > Detail arsitektur: `docs/ARCHITECTURE.md`.

   Opsi lain dari engine yang sama (`scripts/putusan3_harvest.py`):
   | Tujuan | Perintah tambahan |
   |--------|-------------------|
   | Smoke test 3 halaman | tambah `--max-pages 3` |
   | Dari halaman tertentu | tambah `--from-page K` |
   | Browser tampil | tambah `--headed` |

## Menjalankan detached (Windows) — biarkan konduktor berjalan sendiri

```powershell
# kerjakan di root repo; tulis log ke logs/
Start-Process -FilePath python `
  -ArgumentList 'scripts\putusan3_conductor.py','--year','2026' `
  -WorkingDirectory (Get-Location) -WindowStyle Hidden `
  -RedirectStandardOutput 'logs\conductor_run.log' `
  -RedirectStandardError 'logs\conductor_run.err'
```

Cek kemajuan kapan saja (tidak perlu mengganggu proses):
```powershell
(Get-ChildItem data\harvest\2026\pages -Filter list_*.html).Count
Get-Content logs\conductor_run.log -Tail 5
```

## Tahap berikutnya (SELESAI — tahap 2 hidup sejak 2026-10-27)

Dari HTML listing → detail HTML tiap putusan → ekstraksi `nomor`+link
`/direktori/download_file/<md5>/(pdf|zip)/<kode>` → CSV inventaris offline,
semuanya sudah bekerja (lihat README "The Harvest Workflow"):

```powershell
python -m putusan3 harvest --year 2026 --details-only   # tahap 2: detail HTML (resumable)
python -m putusan3 harvest --year 2026 --parse-only     # offline -> CSV/txt/jsonl
```

Hasil: `data/harvest/<year>/putusan3_<year>_links.csv`
(kolom `nomor,title,url_pdf,url_zip`) + `_pdf_urls.txt`/`_zip_urls.txt`/
`_detail_urls.txt`/`_details.jsonl`/`summary.json`.
