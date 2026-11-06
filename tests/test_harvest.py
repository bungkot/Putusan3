"""Tests for the putusan3 harvester (offline parsing + URL building only).

No network and no browser are used here: every test works on HTML fixtures or
temporary directories.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import putusan3_harvest as harvest  # noqa: E402

BASE = "https://putusan3.mahkamahagung.go.id"

ROW = (
    '<div class="spost clearfix"><div class="entry-c">'
    '<strong><a href="{base}/direktori/putusan/{kode}.html">'
    "Putusan PN DENPASAR Nomor {nomor}/Pdt.G/2022/PN Dps</a></strong>"
    "</div></div>"
)

LISTING_HTML = (
    '<section id="content">'
    + ROW.format(base=BASE, kode="zaf1a51427791598836c313531353430", nomor="1208")
    + ROW.format(base=BASE, kode="zaf1a50729abcac088d2313334323430", nomor="73")
    + "</section>"
    # Sidebar starts here; these decisions must NOT be counted as list rows.
    + '<div id="tabs-2"><div class="spost clearfix">'
    + ROW.format(base=BASE, kode="zaed4ba6ad33e7428c86313635373536", nomor="487")
    + "</div></div>"
    + '<div class="pagging"><a data-ci-pagination-page="2">2</a>'
    + '<a data-ci-pagination-page="499">Last</a></div>'
)

DETAIL_HTML = (
    "<html><body><h1>Putusan</h1>"
    '<a href="' + BASE + '/direktori/download_file/0d64b6103d0f5832d6e382bfbe9a3f08/'
    'zip/zaecc0824f980c62affa313531373235">31_P/HUM/2022.zip</a>'
    '<a href="' + BASE + '/direktori/download_file/0d64b6103d0f5832d6e382bfbe9a3f08/'
    'pdf/zaecc0824f980c62affa313531373235">31_P/HUM/2022.pdf</a>'
    + "x" * 13_000
    + "</body></html>"
)


class TestParsing:
    def test_parse_listing_excludes_side_tabs(self):
        rows = harvest.parse_listing(LISTING_HTML)
        assert [r[0] for r in rows] == [
            "zaf1a51427791598836c313531353430",
            "zaf1a50729abcac088d2313334323430",
        ]

    def test_parse_listing_urls_and_titles(self):
        rows = harvest.parse_listing(LISTING_HTML)
        assert rows[0][1] == (
            BASE + "/direktori/putusan/zaf1a51427791598836c313531353430.html"
        )
        assert "Nomor 1208/Pdt.G/2022" in rows[0][2]

    def test_parse_last_page_uses_highest_advertised_page(self):
        assert harvest.parse_last_page(LISTING_HTML) == 499

    def test_parse_last_page_none_without_pagination(self):
        assert harvest.parse_last_page("<html><body>nothing</body></html>") is None

    def test_parse_detail_finds_pdf_and_zip(self):
        links = harvest.parse_detail(DETAIL_HTML)
        kinds = {kind for kind, _url, _name in links}
        assert kinds == {"pdf", "zip"}
        assert all("/direktori/download_file/" in url for _k, url, _n in links)
        pdf_name = next(name for kind, _url, name in links if kind == "pdf")
        assert pdf_name == "31_P/HUM/2022.pdf"

    def test_parse_detail_deduplicates(self):
        assert len(harvest.parse_detail(DETAIL_HTML + DETAIL_HTML)) == 2

    def test_parse_detail_relative_links_are_absolutised(self):
        html = (
            '<a href="/direktori/download_file/aa/pdf/zaecc0824f980c62affa313531373235">x</a>'
        )
        links = harvest.parse_detail(html)
        assert links[0][1].startswith(BASE + "/direktori/download_file/")

    def test_parse_courts(self):
        html = (
            '<a href="' + BASE + '/pengadilan/profil/pengadilan/pn-denpasar.html">PN</a>'
            '<a href="' + BASE + '/direktori/index/pengadilan/pa-bandung/tahunjenis/'
            'putus/tahun/2022.html">PA</a>'
        )
        assert harvest.parse_courts(html) == ["pn-denpasar", "pa-bandung"]

    def test_page_is_empty(self):
        assert harvest.page_is_empty("<div>Data Tidak Ditemukan</div>")
        assert not harvest.page_is_empty(LISTING_HTML)


class TestChallengeDetection:
    def test_403_is_challenge(self):
        assert harvest.is_challenge("<html>Whatever</html>", 403)

    def test_cloudflare_title_is_challenge(self):
        assert harvest.is_challenge("<title>Just a moment...</title>", 200)

    def test_recaptcha_shell_is_challenge(self):
        html = "<div>Pencarian belum dapat diproses. Centang kotak 'Saya bukan robot'</div>"
        assert harvest.is_challenge(html, 200)

    def test_valid_listing_is_not_challenge(self):
        assert not harvest.is_challenge(LISTING_HTML, 200)
        assert harvest.is_valid_listing(LISTING_HTML + "x" * 4_000)

    def test_valid_listing_rejects_challenge(self):
        assert not harvest.is_valid_listing("<title>Just a moment...</title>", 200)
        assert not harvest.is_valid_listing("<html>short</html>", 200)

    def test_valid_detail(self):
        assert harvest.is_valid_detail(DETAIL_HTML)
        assert not harvest.is_valid_detail("<html>nope</html>")


class TestUrls:
    def test_year_listing_urls(self):
        assert harvest.year_listing_url(2022) == (
            BASE + "/direktori/index/tahunjenis/putus/tahun/2022.html"
        )
        assert harvest.year_listing_url(2022, 2) == (
            BASE + "/direktori/index/tahunjenis/putus/tahun/2022/page/2.html"
        )
        # Regression: the search.html route is captcha-gated; never use it.
        assert "search.html" not in harvest.year_listing_url(2022, 7)

    def test_court_listing_urls(self):
        assert harvest.court_listing_url("pn-denpasar", 2022) == (
            BASE + "/direktori/index/pengadilan/pn-denpasar/tahunjenis/putus/tahun/2022.html"
        )
        assert harvest.court_listing_url("pn-denpasar", 2022, 3) == (
            BASE + "/direktori/index/pengadilan/pn-denpasar/tahunjenis/putus/tahun/2022/page/3.html"
        )

    def test_ensure_absolute(self):
        assert harvest.ensure_absolute("/x/y") == BASE + "/x/y"
        assert harvest.ensure_absolute("http://a/b") == "http://a/b"


class TestOfflineHarvest:
    def _harvest(self, out: Path) -> harvest.Harvest:
        args = harvest.build_parser().parse_args(
            ["--year", "2022", "--out-dir", str(out), "--parse-only"]
        )
        return harvest.Harvest(args)

    def test_parse_all_and_write_results(self, tmp_path):
        h = self._harvest(tmp_path)
        h.pages_dir.mkdir(parents=True, exist_ok=True)
        h.details_dir.mkdir(parents=True, exist_ok=True)
        (h.pages_dir / "list_0001.html").write_text(LISTING_HTML, encoding="utf-8")
        (h.details_dir / "detail_zaf1a51427791598836c313531353430.html").write_text(
            DETAIL_HTML, encoding="utf-8"
        )

        parsed = h.parse_all()
        stats = parsed["stats"]
        assert stats["listing_rows"] == 2
        assert stats["detail_files"] == 1
        assert stats["pdf_urls"] == 1
        assert stats["zip_urls"] == 1
        assert stats["decisions_with_links"] == 1

        paths = h.write_results(parsed)
        pdf_lines = Path(paths["pdf_urls"]).read_text(encoding="utf-8").splitlines()
        assert pdf_lines[0].startswith("#")
        assert len(pdf_lines) == 2
        assert pdf_lines[1].endswith("/pdf/zaecc0824f980c62affa313531373235")

        detail_lines = Path(paths["detail_urls"]).read_text(encoding="utf-8").splitlines()
        assert len(detail_lines) == 3  # header + 2 decisions

        jsonl = Path(paths["details_jsonl"]).read_text(encoding="utf-8").splitlines()
        assert len(jsonl) == 1

    def test_parse_only_returns_3_when_no_links(self, tmp_path):
        h = self._harvest(tmp_path)
        h.pages_dir.mkdir(parents=True, exist_ok=True)
        (h.pages_dir / "list_0001.html").write_text(LISTING_HTML, encoding="utf-8")
        parsed = h.parse_all()
        assert parsed["pdf"] == {} and parsed["zip"] == {}
        assert parsed["stats"]["listing_rows"] == 2

    def test_old_captcha_page_is_not_a_valid_listing(self, tmp_path):
        captcha = "<html><title>Direktori Putusan</title>" + "x" * 60_000 + \
                  "<div>Pencarian belum dapat diproses. Centang kotak 'Saya bukan robot'</div></html>"
        assert not harvest.is_valid_listing(captcha)

    def test_local_rows_deduplicate_across_scopes(self, tmp_path):
        h = self._harvest(tmp_path)
        h.pages_dir.mkdir(parents=True, exist_ok=True)
        (h.pages_dir / "list_0001.html").write_text(LISTING_HTML, encoding="utf-8")
        courts = h.courts_dir / "pn-denpasar"
        courts.mkdir(parents=True, exist_ok=True)
        (courts / "list_0001.html").write_text(LISTING_HTML, encoding="utf-8")
        rows = h.local_rows()
        assert len(rows) == 2
        assert len(rows["zaf1a51427791598836c313531353430"]["listing_pages"]) == 2


class TestSafeWrite:
    def test_safe_write_creates_parent_and_replaces(self, tmp_path):
        target = tmp_path / "a" / "b" / "file.html"
        harvest.safe_write(target, "one")
        assert target.read_text(encoding="utf-8") == "one"
        harvest.safe_write(target, "two")
        assert target.read_text(encoding="utf-8") == "two"
        assert not target.with_suffix(".html.part").exists()
