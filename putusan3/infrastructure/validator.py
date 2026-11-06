"""
Proxy validator for testing connectivity, anonymity, transparency, and
Indonesian geo-location.

Two GeoIP dimensions are tracked:
- `origin_country_code` — the egress/exit IP as seen by the target. This is
  what determines "visiting from Indonesia".
- `proxy_country_code` — the proxy's own IP location (its identity).

Transparent ("elite-but-leaking") proxies echo the client's real IP as the
origin and are flagged via `is_transparent` so they can be excluded.
"""
import asyncio
import logging
import random
import time
from dataclasses import dataclass

import httpx

from putusan3.config import config

logger = logging.getLogger(__name__)

# ip-api.com /batch accepts up to 100 IPs per request
GEOIP_BATCH_SIZE = 100

_GEO_FIELDS = ("status", "query", "countryCode")
_BATCH_URL = "http://ip-api.com/batch?fields=status,query,countryCode"


def proxy_public_ip(proxy: str) -> str:
    """Extract the proxy's host IP from an '[scheme://][user:pass@]host:port' string."""
    host = proxy.rpartition("@")[-1]
    if "://" in host:
        host = host.split("://", 1)[1]
    return host.rsplit(":", 1)[0]


@dataclass
class ProxyValidationResult:
    """Result of proxy validation."""
    proxy: str  # ip:port
    is_working: bool
    latency_ms: float | None = None
    is_anonymous: bool = False
    # True when the target saw our *real* IP — the proxy is transparent/leaking
    is_transparent: bool = False
    is_indonesian: bool = False  # egress/exit country is INDONESIAN_COUNTRY_CODE
    origin_ip: str | None = None            # egress IP reported by the target
    origin_country_code: str | None = None  # GeoIP of the egress IP (exit point)
    proxy_country_code: str | None = None   # GeoIP of the proxy's own IP
    error: str | None = None
    response_headers: dict | None = None


class ProxyValidator:
    """Validate proxy connectivity, anonymity, transparency, and geo-location."""

    def __init__(
        self,
        test_url: str | None = None,
        timeout: int | None = None,
        max_concurrent: int | None = None,
        max_proxies_to_test: int = 50,  # Limit proxies to test per batch
    ):
        self.test_url = test_url or config.VALIDATION_TEST_URL
        self.timeout = timeout or (config.VALIDATION_TIMEOUT / 1000.0)
        self.max_concurrent = max_concurrent or config.MAX_CONCURRENT
        self.max_proxies_to_test = max_proxies_to_test
        self._semaphore = asyncio.Semaphore(self.max_concurrent)
        self._real_ip: str | None = None
        self._real_ip_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def validate(self, proxy: str) -> ProxyValidationResult:
        """Validate a single proxy; returns a full ProxyValidationResult."""
        await self._get_real_ip()
        async with self._semaphore:
            result = await self._validate_single(proxy)
        await self._apply_geo([result])
        return result

    async def validate_batch(self, proxies: list[str]) -> list[ProxyValidationResult]:
        """Validate multiple proxies concurrently, with batched GeoIP lookups."""
        # Deterministic shuffle first: the raw list is source-ordered, so the
        # first-N slice would otherwise only ever test the first source's
        # candidates and never see the rest of the pool.
        test_proxies = sorted(set(proxies))
        # Re-seed every call: a fixed seed would retest the exact same slice on
        # every run, so the rest of the deduplicated pool would never get a turn.
        rng = random.Random()
        rng.shuffle(test_proxies)
        test_proxies = test_proxies[: self.max_proxies_to_test]
        logger.info(
            f"Validating {len(test_proxies)} proxies (max concurrent: {self.max_concurrent})..."
        )

        # One shared lookup of our real IP for transparency detection
        await self._get_real_ip()

        tasks = [self._validate_single(proxy) for proxy in test_proxies]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        validated: list[ProxyValidationResult] = []
        for proxy, result in zip(test_proxies, results, strict=False):
            if isinstance(result, BaseException):
                logger.warning(f"Validation exception for {proxy}: {result}")
                validated.append(
                    ProxyValidationResult(proxy=proxy, is_working=False, error=str(result))
                )
            else:
                validated.append(result)

        # Batched GeoIP: proxy's own IP + egress origin, one POST per ≤100-IP chunk
        working = [r for r in validated if r.is_working]
        if config.USE_GEOIP_FILTER and working:
            ips: list[str | None] = []
            for r in working:
                ips.append(proxy_public_ip(r.proxy))
                ips.append(r.origin_ip)
            country_by_ip = await self._lookup_countries(ips)
            await self._apply_geo(working, country_by_ip)

        working = [r for r in validated if r.is_working]
        indonesian = [r for r in working if r.is_indonesian]
        anonymous = [r for r in working if r.is_anonymous and not r.is_transparent]

        logger.info(
            f"Validation complete: {len(working)}/{len(test_proxies)} working, "
            f"{len(indonesian)} Indonesian exit, {len(anonymous)} anonymous non-transparent"
        )
        return validated

    # ------------------------------------------------------------------
    # Single-proxy checks
    # ------------------------------------------------------------------

    async def _validate_single(self, proxy: str) -> ProxyValidationResult:
        """Connectivity, latency, anonymity, and transparency."""
        proxy_url = f"http://{proxy}"
        start_time = time.time()

        try:
            # httpx 0.28+ uses proxy parameter instead of proxies dict
            async with httpx.AsyncClient(
                proxy=proxy_url,
                timeout=self.timeout,
                follow_redirects=True,
            ) as client:
                response = await client.get(self.test_url)
                latency_ms = (time.time() - start_time) * 1000

                if response.status_code != 200:
                    return ProxyValidationResult(
                        proxy=proxy,
                        is_working=False,
                        latency_ms=latency_ms,
                        error=f"HTTP {response.status_code}",
                    )

                # Parse response to check anonymity, transparency, and egress
                try:
                    data = response.json()
                    origin_ip = data.get("origin", "")
                    headers = data.get("headers", {})
                    origin_ip = origin_ip.split(",")[0].strip() if origin_ip else None

                    # Anonymous when no header leaks the client's real IP
                    forwarded_for = headers.get("X-Forwarded-For", "")
                    real_ip = headers.get("X-Real-Ip", "")
                    via = headers.get("Via", "")
                    is_anonymous = not any([forwarded_for, real_ip, via])

                    # Transparent when the target saw our real IP as origin
                    # (catches "elite" transparent proxies that add no headers)
                    is_transparent = bool(
                        origin_ip and self._real_ip and origin_ip == self._real_ip
                    )
                    if is_transparent:
                        is_anonymous = False

                    return ProxyValidationResult(
                        proxy=proxy,
                        is_working=True,
                        latency_ms=latency_ms,
                        is_anonymous=is_anonymous,
                        is_transparent=is_transparent,
                        origin_ip=origin_ip if origin_ip else None,
                        response_headers=headers,
                    )
                except Exception as e:
                    return ProxyValidationResult(
                        proxy=proxy,
                        is_working=True,  # Connected but couldn't parse
                        latency_ms=latency_ms,
                        error=f"Parse error: {e}",
                    )

        except httpx.TimeoutException:
            return ProxyValidationResult(
                proxy=proxy,
                is_working=False,
                error="Timeout",
            )
        except httpx.ProxyError as e:
            return ProxyValidationResult(
                proxy=proxy,
                is_working=False,
                error=f"Proxy error: {e}",
            )
        except Exception as e:
            return ProxyValidationResult(
                proxy=proxy,
                is_working=False,
                error=str(e),
            )

    async def _get_real_ip(self) -> str | None:
        """Fetch this machine's public IP once (no proxy) for transparency checks."""
        async with self._real_ip_lock:
            if self._real_ip is not None:
                return self._real_ip
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    response = await client.get(self.test_url)
                    if response.status_code == 200:
                        origin = response.json().get("origin", "")
                        self._real_ip = origin.split(",")[0].strip() if origin else None
            except Exception as e:
                logger.debug(f"Could not determine real client IP: {e}")
            return self._real_ip

    # ------------------------------------------------------------------
    # GeoIP helpers
    # ------------------------------------------------------------------

    async def _lookup_countries(self, ips: list[str | None]) -> dict[str, str]:
        """
        Look up country codes for a list of IPs via the batch endpoint,
        chunked at GEOIP_BATCH_SIZE IPs per request.
        Falls back to per-IP lookups when the batch endpoint fails.
        Returns a dict mapping ip -> countryCode.
        """
        unique_ips = [ip for ip in dict.fromkeys(ips) if ip]
        if not unique_ips:
            return {}

        out: dict[str, str] = {}
        chunks = [
            unique_ips[i : i + GEOIP_BATCH_SIZE]
            for i in range(0, len(unique_ips), GEOIP_BATCH_SIZE)
        ]

        batch_ok = True
        for chunk in chunks:
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    response = await client.post(_BATCH_URL, json=chunk)
                if response.status_code == 200:
                    for row in response.json():
                        ip = row.get("query")
                        code = row.get("countryCode")
                        if row.get("status") == "success" and ip and code:
                            out[ip] = code
                else:
                    logger.warning(
                        f"GeoIP batch lookup failed (HTTP {response.status_code}), "
                        "falling back to per-IP lookups"
                    )
                    batch_ok = False
                    break
            except Exception as e:
                logger.warning(f"GeoIP batch lookup failed, falling back to per-IP: {e}")
                batch_ok = False
                break

        if not batch_ok:
            for ip in unique_ips:
                if ip not in out:
                    code = await self._lookup_country(ip)
                    if code:
                        out[ip] = code
        return out

    async def _lookup_country(self, single_ip: str) -> str | None:
        """Look up the country code for a single IP via ip-api.com."""
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.get(
                    f"http://ip-api.com/json/{single_ip}?fields=countryCode"
                )
                if response.status_code == 200:
                    return response.json().get("countryCode")
        except Exception:
            pass
        return None

    async def _apply_geo(
        self,
        results: list[ProxyValidationResult],
        country_by_ip: dict[str, str] | None = None,
    ) -> None:
        """Attach country codes and the Indonesian flag to validation results.

        - `origin_country_code`   ← GeoIP of the egress IP (the exit point the
          target site sees; this is what `is_indonesian` is based on).
        - `proxy_country_code`    ← GeoIP of the proxy's own IP (its identity).
        """
        if not config.USE_GEOIP_FILTER:
            for result in results:
                result.is_indonesian = True  # Skip geo check; accept all proxies
            return

        if country_by_ip is None:
            ips: list[str | None] = []
            for result in results:
                ips.append(proxy_public_ip(result.proxy))
                ips.append(result.origin_ip)
            country_by_ip = await self._lookup_countries(ips)

        for result in results:
            result.proxy_country_code = country_by_ip.get(proxy_public_ip(result.proxy))
            result.origin_country_code = (
                country_by_ip.get(result.origin_ip) if result.origin_ip else None
            )
            result.is_indonesian = result.origin_country_code == config.INDONESIAN_COUNTRY_CODE


async def validate_proxies(
    proxies: list[str], max_to_test: int | None = None
) -> list[ProxyValidationResult]:
    """Convenience function to validate a list of proxies."""
    validator = ProxyValidator(max_proxies_to_test=max_to_test or config.MAX_PROXIES_TO_TEST)
    return await validator.validate_batch(proxies)


def filter_working_proxies(results: list[ProxyValidationResult]) -> list[str]:
    """Extract working proxy strings from validation results."""
    return [r.proxy for r in results if r.is_working]


def filter_indonesian_proxies(results: list[ProxyValidationResult]) -> list[str]:
    """Extract proxies whose egress/exit country is Indonesian."""
    return [r.proxy for r in results if r.is_working and r.is_indonesian]


def filter_anonymous_proxies(results: list[ProxyValidationResult]) -> list[str]:
    """Extract anonymous, non-transparent proxy strings from validation results."""
    return [r.proxy for r in results if r.is_working and r.is_anonymous and not r.is_transparent]


def get_best_proxies(
    results: list[ProxyValidationResult],
    count: int = 10,
    require_indonesian: bool = True,
    require_anonymous: bool = True,
    exclude_transparent: bool = True,
) -> list[str]:
    """
    Get the best proxies sorted by latency.

    `require_indonesian` checks the egress country (what the target sees).
    `exclude_transparent` drops leaking proxies that echo the client's real IP.
    """
    filtered = [
        r for r in results
        if r.is_working
        and (not require_indonesian or r.is_indonesian)
        and (not require_anonymous or r.is_anonymous)
        and (not exclude_transparent or not r.is_transparent)
    ]

    # Sort by latency (fastest first)
    filtered.sort(key=lambda r: r.latency_ms or float('inf'))

    return [r.proxy for r in filtered[:count]]


if __name__ == "__main__":
    """Standalone check: fetch, validate, and print best proxies."""
    from putusan3.infrastructure.sources import fetch_all_proxies

    async def _main():
        proxies = await fetch_all_proxies()
        print(f"Fetched {len(proxies)} proxies")
        results = await validate_proxies(proxies)
        best = get_best_proxies(results, count=20)
        print(f"Best proxies ({len(best)}):")
        for proxy in best:
            print(f"  {proxy}")

    asyncio.run(_main())
