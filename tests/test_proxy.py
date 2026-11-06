"""
Tests for proxy functionality.
"""
import pytest

from proxy.rotator import ProxyHealth, ProxyRotator
from proxy.sources import GitHubRawSource, ProxyscrapeSource, create_sources, fetch_all_proxies
from proxy.validator import (
    ProxyValidator,
    filter_working_proxies,
    get_best_proxies,
    validate_proxies,
)


class TestProxySources:
    """Test proxy source fetching."""

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_fetch_all_proxies_returns_list(self):
        """Test that fetch_all_proxies returns a list of proxies."""
        proxies = await fetch_all_proxies()
        assert isinstance(proxies, list)
        # Should have at least some proxies from free sources
        assert len(proxies) >= 0  # May be 0 if all sources fail

    @pytest.mark.asyncio
    async def test_create_sources(self):
        """Test source creation from config."""
        sources = create_sources()
        assert isinstance(sources, list)
        assert len(sources) > 0

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_proxyscrape_source(self):
        """Test proxyscrape source."""
        source = ProxyscrapeSource("test", "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=http&timeout=10000&country=ID&ssl=all&anonymity=all")
        proxies = await source.fetch()
        assert isinstance(proxies, list)

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_github_raw_source(self):
        """Test GitHub raw source."""
        source = GitHubRawSource("test", "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt")
        proxies = await source.fetch()
        assert isinstance(proxies, list)


class TestProxyValidator:
    """Test proxy validation."""

    @pytest.mark.asyncio
    async def test_validator_initialization(self):
        """Test validator can be created."""
        validator = ProxyValidator()
        assert validator is not None
        assert validator.max_concurrent > 0

    def test_proxy_public_ip(self):
        """Test host-IP extraction from various proxy string formats."""
        from proxy.validator import proxy_public_ip

        assert proxy_public_ip("1.2.3.4:8080") == "1.2.3.4"
        assert proxy_public_ip("user:pass@1.2.3.4:8080") == "1.2.3.4"
        assert proxy_public_ip("http://1.2.3.4:8080") == "1.2.3.4"

    @pytest.mark.asyncio
    async def test_geo_labels_proxy_not_origin(self, monkeypatch):
        """Geo labels must track egress (origin_country_code) and proxy identity separately."""
        from config import config
        from proxy.validator import ProxyValidationResult, ProxyValidator

        monkeypatch.setattr(config, "USE_GEOIP_FILTER", True)
        validator = ProxyValidator()
        result = ProxyValidationResult(
            proxy="102.209.18.96:8080",  # a KE-geo proxy IP (identity)
            is_working=True,
            origin_ip="103.157.58.88",   # an ID-geo egress (exit point)
        )
        await validator._apply_geo([result], {"102.209.18.96": "KE", "103.157.58.88": "ID"})
        assert result.proxy_country_code == "KE"
        assert result.origin_country_code == "ID"
        # is_indonesian follows the *egress* country — what the target sees
        assert result.is_indonesian is True

        # And the transparent-proxy exclusion: same IP data, flagged transparent
        result2 = ProxyValidationResult(
            proxy="102.209.18.96:8080",
            is_working=True,
            origin_ip="103.157.58.88",
            is_transparent=True,  # target saw our real IP → leaking
        )
        result2.proxy_country_code = "KE"
        result2.origin_country_code = "ID"
        result2.is_indonesian = True
        best = get_best_proxies([result2], require_indonesian=True, require_anonymous=False)
        assert best == []  # transparent proxies are excluded by default

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_validate_single_proxy(self):
        """Test validation of a single proxy."""
        # Use a known working proxy format (will likely fail but shouldn't crash)
        validator = ProxyValidator(timeout=5.0)
        result = await validator.validate("8.8.8.8:80")  # Google DNS, not a proxy
        assert result is not None
        assert hasattr(result, 'is_working')
        assert hasattr(result, 'proxy')

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_validate_batch(self):
        """Test batch validation."""
        validator = ProxyValidator(timeout=5.0, max_concurrent=2)
        test_proxies = ["8.8.8.8:80", "1.1.1.1:80"]
        results = await validator.validate_batch(test_proxies)
        assert len(results) == 2
        for r in results:
            assert hasattr(r, 'proxy')
            assert hasattr(r, 'is_working')


class TestProxyRotator:
    """Test proxy rotation logic."""

    def test_rotator_initialization(self):
        """Test rotator can be created."""
        rotator = ProxyRotator()
        assert rotator is not None
        assert rotator.max_failures > 0

    def test_proxy_health_tracking(self):
        """Test ProxyHealth tracking."""
        health = ProxyHealth(proxy="1.2.3.4:8080")
        assert health.proxy == "1.2.3.4:8080"
        assert health.failures == 0
        assert health.successes == 0

        health.record_success(100.0)
        assert health.successes == 1
        assert health.avg_latency_ms == 100.0

        health.record_failure()
        assert health.failures == 1

    def test_proxy_retirement(self):
        """Test proxy retirement after max consecutive failures."""
        health = ProxyHealth(proxy="1.2.3.4:8080")
        max_failures = 3

        for _ in range(max_failures):
            health.record_failure()

        assert health.should_retire(max_failures) is True

    def test_retirement_counts_consecutive_failures(self):
        """Retirement triggers on consecutive failures even after successes."""
        health = ProxyHealth(proxy="1.2.3.4:8080")
        max_failures = 3

        health.record_success(100.0)
        for _ in range(max_failures):
            health.record_failure()
        # consecutive_failures == 3 → retired despite past success
        assert health.should_retire(max_failures) is True

        health.record_success(100.0)
        health.record_failure()
        # consecutive_failures == 1 → not retired
        assert health.should_retire(max_failures) is False

    def test_rotator_is_retired(self):
        """Test rotator.is_retired helper."""
        rotator = ProxyRotator(max_failures=3)
        health = ProxyHealth(proxy="1.2.3.4:8080")
        rotator._proxies.append(health)
        rotator._proxy_map["1.2.3.4:8080"] = health

        assert rotator.is_retired("1.2.3.4:8080") is False
        for _ in range(3):
            rotator.record_failure("1.2.3.4:8080")
        assert rotator.is_retired("1.2.3.4:8080") is True
        assert rotator.is_retired("unknown:1") is False

    def test_rotator_round_robin(self):
        """Test round-robin selection."""
        rotator = ProxyRotator()
        # Add some test proxies
        for i in range(3):
            health = ProxyHealth(proxy=f"1.2.3.{i}:8080")
            health.successes = 1  # Make them healthy
            rotator._proxies.append(health)
            rotator._proxy_map[f"1.2.3.{i}:8080"] = health

        # Get proxies in round-robin
        proxies = []
        for _ in range(6):
            h = rotator.get_next()
            if h:
                proxies.append(h.proxy)

        # Should cycle through all 3
        assert len(set(proxies)) == 3


class TestIntegration:
    """Integration tests (may require network)."""

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_full_flow_fetch_validate(self):
        """Test full flow: fetch -> validate."""
        # This is an integration test that requires network
        proxies = await fetch_all_proxies()
        if not proxies:
            pytest.skip("No proxies fetched from sources")

        # Validate a subset
        test_proxies = proxies[:5]
        results = await validate_proxies(test_proxies)

        working = filter_working_proxies(results)
        # At least some should work (or all fail gracefully)
        assert isinstance(working, list)

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_rotator_refresh(self):
        """Test rotator refresh."""
        rotator = ProxyRotator(min_pool_size=1)
        count = await rotator.initialize()
        assert count >= 0  # May be 0 if no proxies available

        stats = rotator.get_stats()
        assert "total" in stats
        assert "healthy" in stats


class TestProxyPoolTextPersistence:
    """Test saving/loading the pool as a plain text file."""

    def _rotator_with(self, proxies):
        rotator = ProxyRotator()
        for proxy in proxies:
            rotator.add_proxy(proxy)
        return rotator

    def test_save_and_load_roundtrip(self, tmp_path):
        """Saved pool must be loadable back into a fresh rotator."""
        source = self._rotator_with(["1.2.3.4:8080", "5.6.7.8:3128"])
        path = str(tmp_path / "proxies.txt")

        assert source.save_pool(path) == 2

        loaded = ProxyRotator()
        assert loaded.load_from_text(path) == 2
        assert set(loaded.get_all_proxies()) == {"1.2.3.4:8080", "5.6.7.8:3128"}

    def test_saved_lines_are_bare_ip_port(self, tmp_path):
        """Output must be directly consumable as 'ip:port' lines."""

        rotator = self._rotator_with(["1.2.3.4:8080"])
        path = str(tmp_path / "proxies.txt")
        rotator.save_pool(path)

        lines = [ln for ln in open(path, encoding="utf-8").read().splitlines() if ln]
        data = [ln for ln in lines if not ln.startswith("#")]
        assert data == ["1.2.3.4:8080"]

    def test_load_skips_comments_and_invalid(self, tmp_path):
        """Comments, blanks, and malformed lines are ignored on load."""
        proxy_l = self._rotator_with([])
        path = tmp_path / "pool.txt"
        path.write_text(
            "# snapshot header\n"
            "9.9.9.9:9000\n"
            "\n"
            "not-a-proxy\n"
            "http://10.0.0.1:8080\n"
            "# trailing comment\n",
            encoding="utf-8",
        )

        assert proxy_l.load_from_text(str(path)) == 2
        assert set(proxy_l.get_all_proxies()) == {"9.9.9.9:9000", "10.0.0.1:8080"}

    def test_load_missing_file(self, tmp_path):
        """A missing file loads zero proxies without raising."""
        rotator = self._rotator_with([])
        assert rotator.load_from_text(str(tmp_path / "nope.txt")) == 0

    def test_add_proxy_reactivates_retired(self):
        """Loading a previously retired proxy resets its health and reactivates it."""
        rotator = ProxyRotator()
        rotator.add_proxy("1.2.3.4:8080")
        for _ in range(3):
            rotator.record_failure("1.2.3.4:8080")
        assert rotator.is_retired("1.2.3.4:8080") is True

        assert rotator.add_proxy("1.2.3.4:8080") is not None
        health = rotator._proxy_map["1.2.3.4:8080"]
        assert health.is_retired is False
        assert health.consecutive_failures == 0

    def test_normalize_proxy_line(self):
        """Shared line parser accepts the documented formats."""
        from proxy.sources import normalize_proxy_line

        assert normalize_proxy_line("1.2.3.4:8080") == "1.2.3.4:8080"
        assert normalize_proxy_line("http://1.2.3.4:8080") == "1.2.3.4:8080"
        assert normalize_proxy_line("user:pass@1.2.3.4:8080") == "user:pass@1.2.3.4:8080"
        assert normalize_proxy_line("# comment") is None
        assert normalize_proxy_line("garbage") is None
        assert normalize_proxy_line("1.2.3.4:99999") is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
