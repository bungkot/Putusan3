"""
Tests for scraper functionality.
"""
import json
import os
from unittest.mock import AsyncMock, Mock, patch

import pytest

from proxy.rotator import ProxyHealth, ProxyRotator
from scraper.visitor import (
    ProxyVisitor,
    ScrapingResult,
    visit_with_proxy,
)


class TestProxyVisitor:
    """Test ProxyVisitor class."""

    def test_visitor_initialization(self):
        """Test visitor can be created."""
        rotator = ProxyRotator()
        visitor = ProxyVisitor(rotator)
        assert visitor is not None
        assert visitor.headless is True

    @pytest.mark.asyncio
    @pytest.mark.integration  # launches a real Chromium; run with -m integration
    async def test_visitor_context_manager(self):
        """Test visitor as async context manager."""
        rotator = ProxyRotator()
        # Add a mock proxy
        health = ProxyHealth(proxy="1.2.3.4:8080")
        health.successes = 1
        rotator._proxies.append(health)
        rotator._proxy_map["1.2.3.4:8080"] = health

        async with ProxyVisitor(rotator, headless=True) as visitor:
            assert visitor._playwright is not None
            assert visitor._browser is not None

    @pytest.mark.asyncio
    async def test_get_random_user_agent(self):
        """Test user agent rotation."""
        rotator = ProxyRotator()
        visitor = ProxyVisitor(rotator)

        agents = set()
        for _ in range(20):
            agents.add(visitor._get_random_user_agent())

        # Should have multiple different user agents
        assert len(agents) > 1

    @pytest.mark.asyncio
    async def test_get_random_viewport(self):
        """Test viewport rotation."""
        rotator = ProxyRotator()
        visitor = ProxyVisitor(rotator)

        viewports = set()
        for _ in range(20):
            vp = visitor._get_random_viewport()
            viewports.add((vp["width"], vp["height"]))

        assert len(viewports) > 1


class TestRotationStrategies:
    """Backlog closed: `visit()` now accepts a rotation strategy."""

    @staticmethod
    def _visitor() -> tuple[ProxyVisitor, ProxyRotator]:
        rotator = ProxyRotator()
        return ProxyVisitor(rotator), rotator

    @pytest.mark.asyncio
    async def test_visit_passes_picked_proxy_to_visit_with_proxy(self, monkeypatch):
        visitor, rotator = self._visitor()
        monkeypatch.setattr(rotator, "get_random", lambda: ProxyHealth(proxy="rand:1"))
        seen: dict[str, str] = {}

        async def fake_visit_with_proxy(url, proxy, wait_for, extract_fn):
            seen["proxy"] = proxy
            return ScrapingResult(url=url, proxy=proxy, success=True)

        monkeypatch.setattr(visitor, "_visit_with_proxy", fake_visit_with_proxy)
        result = await visitor.visit("https://example.com", strategy="random")

        assert seen["proxy"] == "rand:1"
        assert result.success is True

    @pytest.mark.asyncio
    async def test_pick_proxy_strategies(self, monkeypatch):
        visitor, rotator = self._visitor()
        monkeypatch.setattr(rotator, "get_next", lambda: ProxyHealth(proxy="rr:1"))
        monkeypatch.setattr(rotator, "get_random", lambda: ProxyHealth(proxy="rand:1"))
        monkeypatch.setattr(rotator, "get_best_by_latency", lambda count=1: [ProxyHealth(proxy="lat:1")])

        assert visitor._pick_proxy("round_robin").proxy == "rr:1"
        assert visitor._pick_proxy("random").proxy == "rand:1"
        assert visitor._pick_proxy("best_latency").proxy == "lat:1"
        assert visitor._pick_proxy("anything-else").proxy == "rr:1"   # unknown -> round_robin

    @pytest.mark.asyncio
    async def test_pick_proxy_without_healthy_proxies(self):
        visitor, _rotator = self._visitor()          # empty pool
        assert visitor._pick_proxy("round_robin") is None
        assert visitor._pick_proxy("best_latency") is None


class TestScrapingResult:
    """Test ScrapingResult dataclass."""

    def test_scraping_result_creation(self):
        """Test creating ScrapingResult."""
        result = ScrapingResult(
            url="https://example.com",
            proxy="1.2.3.4:8080",
            success=True,
            status_code=200,
            content="<html>Test</html>",
            latency_ms=150.5,
        )

        assert result.url == "https://example.com"
        assert result.proxy == "1.2.3.4:8080"
        assert result.success is True
        assert result.status_code == 200
        assert result.latency_ms == 150.5


class TestExtractionHelpers:
    """Test extraction helper functions."""

    @pytest.mark.asyncio
    async def test_extract_text(self):
        """Test extract_text helper."""
        from scraper.visitor import extract_text

        # Mock page
        page = AsyncMock()
        element = AsyncMock()
        element.inner_text = AsyncMock(return_value="Hello World")
        page.query_selector = AsyncMock(return_value=element)

        result = await extract_text(page, "h1")
        assert result == "Hello World"

    @pytest.mark.asyncio
    async def test_extract_all_text(self):
        """Test extract_all_text helper."""
        from scraper.visitor import extract_all_text

        page = AsyncMock()
        el1 = AsyncMock()
        el1.inner_text = AsyncMock(return_value="Item 1")
        el2 = AsyncMock()
        el2.inner_text = AsyncMock(return_value="Item 2")
        page.query_selector_all = AsyncMock(return_value=[el1, el2])

        result = await extract_all_text(page, ".item")
        assert result == ["Item 1", "Item 2"]


class TestConvenienceFunctions:
    """Test convenience functions."""

    @pytest.mark.asyncio
    @patch("scraper.visitor.async_playwright")
    async def test_visit_with_proxy(self, mock_playwright):
        """Test visit_with_proxy function."""
        # Setup mock
        mock_p = AsyncMock()
        mock_browser = AsyncMock()
        mock_context = AsyncMock()
        mock_page = AsyncMock()
        mock_response = AsyncMock()
        mock_page.set_default_timeout = Mock()
        mock_page.set_default_navigation_timeout = Mock()
        mock_response.status = 200
        mock_response.headers = {"content-type": "text/html"}

        mock_page.goto = AsyncMock(return_value=mock_response)
        mock_page.content = AsyncMock(return_value="<html>Test</html>")
        mock_context.cookies = AsyncMock(return_value=[])
        mock_context.new_page = AsyncMock(return_value=mock_page)
        mock_browser.new_context = AsyncMock(return_value=mock_context)
        mock_p.chromium.launch = AsyncMock(return_value=mock_browser)
        mock_playwright.return_value.start = AsyncMock(return_value=mock_p)

        result = await visit_with_proxy(
            "https://httpbin.org/ip",
            "1.2.3.4:8080",
            headless=True,
        )

        assert result.success is True
        assert result.proxy == "1.2.3.4:8080"
        assert result.status_code == 200


class TestProxySettingsCreation:
    """Test Playwright proxy settings construction."""

    def test_plain_proxy(self):
        from scraper.visitor import ProxyVisitor

        visitor = ProxyVisitor(ProxyRotator())
        settings = visitor._create_proxy_settings("1.2.3.4:8080")
        assert settings["server"] == "http://1.2.3.4:8080"
        assert not settings.get("username")
        assert not settings.get("password")

    def test_proxy_with_auth(self):
        from scraper.visitor import ProxyVisitor

        visitor = ProxyVisitor(ProxyRotator())
        settings = visitor._create_proxy_settings("user:secret@1.2.3.4:8080")
        assert settings["server"] == "http://1.2.3.4:8080"
        assert settings["username"] == "user"
        assert settings["password"] == "secret"


class TestResultsExport:
    """Test the results export helpers (scraper/results.py)."""

    def _sample_results(self, count=2):
        from scraper.visitor import ScrapingResult

        return [
            ScrapingResult(
                url=f"https://example.com/{i}",
                proxy="1.2.3.4:8080",
                success=True,
                status_code=200,
                content="<html>x</html>",
                latency_ms=100.0 + i,
            )
            for i in range(count)
        ]

    def test_export_json_append(self, tmp_path):
        from scraper.results import export_results

        path = str(tmp_path / "results.json")
        assert export_results(self._sample_results(), path) == path
        assert export_results(self._sample_results(1), path) == path

        import json
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        assert len(data) == 3
        assert data[0]["url"] == "https://example.com/0"
        assert data[0]["success"] is True

    def test_export_csv_header_and_rows(self, tmp_path):
        from scraper.results import export_results

        path = str(tmp_path / "results.csv")
        assert export_results(self._sample_results(), path) == path
        assert export_results(self._sample_results(1), path) == path

        import csv
        with open(path, encoding="utf-8", newline="") as f:
            rows = list(csv.reader(f))
        assert rows[0][0] == "url"  # header written once
        assert len(rows) == 4       # header + 3 rows

    def test_export_empty_is_noop(self, tmp_path):
        from scraper.results import export_results

        path = str(tmp_path / "results.json")
        assert export_results([], path) is None
        assert not os.path.exists(path)

    def test_export_json_preserves_corrupt_existing_file(self, tmp_path):
        from scraper.results import export_results

        path = tmp_path / "results.json"
        original = "{ incomplete json"
        path.write_text(original, encoding="utf-8")

        with pytest.raises(json.JSONDecodeError):
            export_results(self._sample_results(1), str(path))

        assert path.read_text(encoding="utf-8") == original


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
