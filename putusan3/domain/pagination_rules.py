"""Pure rules & parsers of the putusan3 direktori listings.

Domain layer: framework-free. No playwright, no I/O. Extracted verbatim
from scripts/putusan3_harvest.py (byte-equal logic, same signatures).

Note: browser-profile constants (CHROME_UA / STEALTH_INIT_SCRIPT /
VIEWPORTS / DIRECT_KEY) live here next to the site rules so that they have
a single source of truth; only the infrastructure engine reads them.
"""
from __future__ import annotations

import html as html_mod
import re

__version__ = "3.0.0"

DEFAULT_YEAR = 2026           # default decision year (matches the modern CLIs)
DEFAULT_POOL = "data/pools/proxy_pool.txt"   # text snapshot; subdirectories auto-created
BASE = "https://putusan3.mahkamahagung.go.id"
SITE_MAX_PAGES = 499          # hard pagination cap enforced by the website
ROWS_PER_PAGE = 20            # rows on a direktori listing page

CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
STEALTH_INIT_SCRIPT = (
    "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
)
VIEWPORTS = [
    {"width": 1920, "height": 1080},
    {"width": 1536, "height": 864},
    {"width": 1440, "height": 900},
]

DIRECT_KEY = "__direct__"

# A decision row on a listing page:
#   <strong><a href=".../direktori/putusan/<kode>.html">Putusan ...</a></strong>
ROW_DETAIL_RE = re.compile(
    r'<strong>\s*<a href="' + re.escape(BASE)
    + r'/direktori/putusan/([a-z0-9]+)\.html"[^>]*>(.*?)</a>\s*</strong>',
    re.S | re.I,
)
# Download links (md5 segment is unpredictable, hence the detail hop):
#   .../direktori/download_file/<md5>/(pdf|zip)/<kode>
DOWNLOAD_RE = re.compile(
    r'href="(?:' + re.escape(BASE) + r')?(/direktori/download_file/[0-9a-f]+/'
    r'(pdf|zip)/[a-z0-9]+)"[^>]*>(.*?)</a>',
    re.S | re.I,
)
TAG_RE = re.compile(r"<[^>]+>")
PAGE_LINK_RE = re.compile(r'data-ci-pagination-page="(\d+)"')
PAGE_HREF_RE = re.compile(r"/page/(\d+)\.html")
COURT_LINK_RE = re.compile(
    r'href="(?:' + re.escape(BASE) + r')?(?:/pengadilan/profil/pengadilan/'
    r'|/direktori/index/pengadilan/)([a-z0-9-]+)(?:\.html|/)',
    re.I,
)

# Decision number, as printed in listing/detail titles, e.g.:
#   "Putusan MAHKAMAH AGUNG Nomor 7598 K/PID.SUS/2026"
#   "Putusan PN DENPASAR Nomor 1208/Pdt.G/2022/PN Dps"
# The number ends at a tag boundary (double space after tag-strip) or at one
# of these words that start the next paragraph of the page.
NOMOR_STOP_RE = re.compile(
    r"\s+(?:tentang|dalam|tahun|dengan|atas|mengenai|klasifikasi|tanggal)\b",
    re.I,
)
NOMOR_HEAD_RE = re.compile(r"\bNomor\b\s*(?:Putusan\b\s*)?", re.I)


def nomor_from_text(text: str) -> str:
    """Decision number after the 'Nomor' marker, whitespace-normalised."""
    txt = html_mod.unescape(TAG_RE.sub("  ", text))
    m = NOMOR_HEAD_RE.search(txt)
    if not m:
        return ""
    tail = txt[m.end():].lstrip()
    field = tail.split("  ", 1)[0]          # boundary is a tag gap
    field = NOMOR_STOP_RE.split(field, 1)[0]  # boundary is a stopword
    field = field.strip().strip(".,;:")
    # A real decision number always contains digits; plain prose that merely
    # says the word "nomor" ("...tanpa nomor sama sekali...") must not match.
    return field if field and any(ch.isdigit() for ch in field) else ""


def parse_nomor(html: str) -> str:
    """Nomor putusan from a detail (or listing) page body."""
    return nomor_from_text(html)

CHALLENGE_MARKERS = (
    "just a moment",
    "access denied",
    "attention required",
    "saya bukan robot",
    "pencarian belum dapat diproses",
    "cf-challenge",
    "checking your browser",
)
EMPTY_MARKERS = ("data tidak ditemukan", "tidak ada data")


def year_listing_url(year: int, page: int = 1) -> str:
    if page <= 1:
        return f"{BASE}/direktori/index/tahunjenis/putus/tahun/{year}.html"
    return f"{BASE}/direktori/index/tahunjenis/putus/tahun/{year}/page/{page}.html"


def court_listing_url(slug: str, year: int, page: int = 1) -> str:
    base = f"{BASE}/direktori/index/pengadilan/{slug}/tahunjenis/putus/tahun/{year}"
    if page <= 1:
        return f"{base}.html"
    return f"{base}/page/{page}.html"


def ensure_absolute(url: str) -> str:
    if url.startswith("http://") or url.startswith("https://"):
        return url
    if url.startswith("/"):
        return BASE + url
    return f"{BASE}/{url}"


def is_challenge(html: str, status: int = 200) -> bool:
    if status in (403, 429, 503):
        return True
    head = html[:8000].lower()
    return any(marker in head for marker in CHALLENGE_MARKERS)


def main_region(html: str) -> str:
    """Keep only the decision list; drop the 'Penting'/'Terpopuler' side tabs.

    The listing lives in the `#tabs-1` panel ("Putusan") of the page's tabbed
    widget. `#tabs-2` ("Penting") and `#tabs-3` ("Terpopuler") contain unrelated
    decisions, so everything from `#tabs-2` onward is cut off.
    """
    for marker in ('id="tabs-2"', "id='tabs-2'", 'id="tabs-3"', "id='tabs-3'"):
        idx = html.find(marker)
        if idx > 0:
            return html[:idx]
    return html


def parse_listing(html: str) -> list[tuple[str, str, str]]:
    """Return [(kode, detail_url, title)] for the main listing region only."""
    region = main_region(html)
    rows: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for m in ROW_DETAIL_RE.finditer(region):
        kode, title = m.group(1), m.group(2)
        if kode in seen:
            continue
        seen.add(kode)
        rows.append((
            kode,
            f"{BASE}/direktori/putusan/{kode}.html",
            html_mod.unescape(TAG_RE.sub(" ", title)).strip(),
        ))
    return rows


def parse_detail(html: str) -> list[tuple[str, str, str]]:
    """Return [(kind, download_url, filename)] with kind in {'pdf','zip'}."""
    out: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for m in DOWNLOAD_RE.finditer(html):
        url, kind, fname = ensure_absolute(m.group(1)), m.group(2).lower(), m.group(3)
        if url in seen:
            continue
        seen.add(url)
        out.append((kind, url, html_mod.unescape(TAG_RE.sub("", fname)).strip()))
    return out


def parse_last_page(html: str) -> int | None:
    """Highest page number advertised by the pagination widget."""
    nums = [int(x) for x in PAGE_LINK_RE.findall(html)]
    nums += [int(x) for x in PAGE_HREF_RE.findall(html)]
    return max(nums) if nums else None


def page_is_empty(html: str) -> bool:
    low = html.lower()
    return any(marker in low for marker in EMPTY_MARKERS)


def is_valid_listing(html: str, status: int = 200) -> bool:
    if status != 200 or is_challenge(html, status):
        return False
    if len(html) < 3_000:
        return False
    return bool(ROW_DETAIL_RE.search(main_region(html))) or page_is_empty(html) \
        or parse_last_page(html) is not None


def is_valid_detail(html: str, status: int = 200) -> bool:
    if status != 200 or is_challenge(html, status):
        return False
    if len(html) < 12_000:
        return False
    low = html.lower()
    return ("/direktori/putusan/" in html) or ("download_file" in low) or page_is_empty(html)


def parse_courts(html: str) -> list[str]:
    """Court slugs from a court index or from any direktori page."""
    out: list[str] = []
    seen: set[str] = set()
    for m in COURT_LINK_RE.finditer(html):
        slug = m.group(1).lower()
        if slug in seen or slug in {"pengadilan", "profil"}:
            continue
        seen.add(slug)
        out.append(slug)
    return out
