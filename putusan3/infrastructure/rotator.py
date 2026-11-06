"""
Proxy rotator with health tracking, round-robin selection, and automatic refresh.
"""
import asyncio
import logging
import random
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from putusan3.config import config
from putusan3.infrastructure.page_store import safe_write
from putusan3.infrastructure.validator import (
    ProxyValidationResult,
    get_best_proxies,
    validate_proxies,
)

logger = logging.getLogger(__name__)


@dataclass
class ProxyHealth:
    """Health tracking for a single proxy."""
    proxy: str
    failures: int = 0
    consecutive_failures: int = 0
    successes: int = 0
    last_used: float = 0
    last_failure: float = 0
    last_success: float = 0
    total_latency_ms: float = 0
    avg_latency_ms: float = 0
    is_retired: bool = False
    validation_result: ProxyValidationResult | None = None

    def record_success(self, latency_ms: float):
        """Record a successful request."""
        self.successes += 1
        self.consecutive_failures = 0
        self.last_used = time.time()
        self.last_success = time.time()
        self.total_latency_ms += latency_ms
        self.avg_latency_ms = self.total_latency_ms / self.successes

    def record_failure(self):
        """Record a failed request."""
        self.failures += 1
        self.consecutive_failures += 1
        self.last_used = time.time()
        self.last_failure = time.time()

    def should_retire(self, max_failures: int | None = None) -> bool:
        """Check if proxy should be retired.

        A proxy is retired after N *consecutive* failures (regardless of past
        successes), which matches the MAX_PROXY_FAILURES semantics.
        """
        max_f = max_failures or config.MAX_PROXY_FAILURES
        return self.consecutive_failures >= max_f

    def success_rate(self) -> float:
        """Calculate success rate."""
        total = self.successes + self.failures
        return self.successes / total if total > 0 else 0.0


class ProxyRotator:
    """
    Round-robin proxy rotator with health tracking.
    Automatically refreshes proxy list when pool is depleted.
    """

    def __init__(
        self,
        max_failures: int | None = None,
        refresh_interval: int | None = None,
        min_pool_size: int | None = None,
    ):
        self.max_failures = max_failures or config.MAX_PROXY_FAILURES
        self.refresh_interval = refresh_interval or config.PROXY_REFRESH_INTERVAL
        self.min_pool_size = min_pool_size or config.MIN_POOL_SIZE

        self._proxies: deque[ProxyHealth] = deque()
        self._proxy_map: dict[str, ProxyHealth] = {}
        self._last_refresh: float = 0
        self._refresh_lock = asyncio.Lock()
        self._current_index: int = 0

    async def initialize(self) -> int:
        """Initial proxy fetch and validation."""
        return await self.refresh()

    async def refresh(self) -> int:
        """Fetch new proxies, validate, and add to pool."""
        async with self._refresh_lock:
            logger.info("Refreshing proxy pool...")

            # Fetch from sources; country-filtered (=ID) candidates are
            # validated first, before the much larger generic lists
            from putusan3.infrastructure.sources import fetch_all_proxies_grouped
            raw_proxies, country_prioritized = await fetch_all_proxies_grouped()

            if not raw_proxies:
                logger.warning("No proxies fetched from sources")
                return len(self._proxies)

            # Validate proxies (limit to avoid long waits)
            results = await validate_proxies(
                raw_proxies,
                max_to_test=config.MAX_PROXIES_TO_TEST,
                prioritized=country_prioritized,
            )

            # Get best proxies (Indonesian exit, anonymous, sorted by latency)
            best_proxies = get_best_proxies(
                results,
                count=50,  # Keep top 50
                require_indonesian=True,
                require_anonymous=True,
            )

            if not best_proxies:
                logger.warning("No working Indonesian proxies found")
                return len(self._proxies)

            # Create health objects for new proxies
            new_count = 0
            for proxy in best_proxies:
                if proxy not in self._proxy_map:
                    # Find validation result for this proxy
                    validation = next((r for r in results if r.proxy == proxy), None)
                    health = ProxyHealth(proxy=proxy, validation_result=validation)
                    self._proxy_map[proxy] = health
                    self._proxies.append(health)
                    new_count += 1

            self._last_refresh = time.time()
            logger.info(f"Proxy pool refreshed: {new_count} new, {len(self._proxies)} total")

            # Optional auto-save to text file
            if config.PROXY_SAVE_PATH:
                self.save_pool(config.PROXY_SAVE_PATH)

            return len(self._proxies)

    def get_next(self) -> ProxyHealth | None:
        """
        Get next proxy in round-robin fashion.
        Skips retired proxies.
        """
        if not self._proxies:
            return None

        # Clean up retired proxies
        self._cleanup_retired()

        if not self._proxies:
            return None

        # Round-robin selection
        attempts = 0
        while attempts < len(self._proxies):
            health = self._proxies[self._current_index]
            self._current_index = (self._current_index + 1) % len(self._proxies)

            if not health.is_retired and not health.should_retire(self.max_failures):
                health.last_used = time.time()
                return health

            attempts += 1

        logger.warning("All proxies are retired or failed")
        return None

    def get_random(self) -> ProxyHealth | None:
        """Get a random healthy proxy."""
        healthy = [h for h in self._proxies if not h.is_retired and not h.should_retire(self.max_failures)]
        if not healthy:
            return None
        health = random.choice(healthy)
        health.last_used = time.time()
        return health

    def get_best_by_latency(self, count: int = 1) -> list[ProxyHealth]:
        """Get best proxies by average latency."""
        healthy = [h for h in self._proxies if not h.is_retired and h.successes > 0]
        healthy.sort(key=lambda h: h.avg_latency_ms)
        return healthy[:count]

    def record_success(self, proxy: str, latency_ms: float):
        """Record successful request for a proxy."""
        if proxy in self._proxy_map:
            self._proxy_map[proxy].record_success(latency_ms)

    def record_failure(self, proxy: str):
        """Record failed request for a proxy."""
        if proxy in self._proxy_map:
            health = self._proxy_map[proxy]
            health.record_failure()

            if health.should_retire(self.max_failures):
                health.is_retired = True
                logger.warning(f"Proxy retired after {health.consecutive_failures} consecutive failures: {proxy}")

    def is_retired(self, proxy: str) -> bool:
        """Check whether a proxy is (or should be) retired."""
        health = self._proxy_map.get(proxy)
        if not health:
            return False
        return health.is_retired or health.should_retire(self.max_failures)

    def _cleanup_retired(self):
        """Remove retired proxies from rotation."""
        initial_count = len(self._proxies)
        self._proxies = deque([h for h in self._proxies if not h.is_retired])
        removed = initial_count - len(self._proxies)
        if removed > 0:
            # The deque was rebuilt; the old index no longer marks a stable
            # position (it was relative to the previous ordering/length).
            self._current_index = 0
            logger.info(f"Cleaned up {removed} retired proxies")

    async def ensure_pool_size(self) -> bool:
        """Ensure minimum pool size, refresh if needed."""
        healthy_count = len([h for h in self._proxies if not h.is_retired])

        if healthy_count < self.min_pool_size:
            logger.info(f"Pool size ({healthy_count}) below minimum ({self.min_pool_size}), refreshing...")
            await self.refresh()
            healthy_count = len([h for h in self._proxies if not h.is_retired])
            return healthy_count >= self.min_pool_size

        # Also refresh periodically
        if time.time() - self._last_refresh > self.refresh_interval:
            logger.info("Periodic proxy refresh...")
            await self.refresh()

        return healthy_count >= self.min_pool_size

    def get_stats(self) -> dict:
        """Get pool statistics."""
        healthy = [h for h in self._proxies if not h.is_retired]
        used = [h for h in healthy if h.successes > 0]
        return {
            "total": len(self._proxies),
            "healthy": len(healthy),
            "retired": len(self._proxies) - len(healthy),
            "avg_latency_ms": (
                sum(h.avg_latency_ms for h in used) / len(used) if used else 0
            ),
            "total_successes": sum(h.successes for h in healthy),
            "total_failures": sum(h.failures for h in healthy),
        }

    def get_all_proxies(self) -> list[str]:
        """Get all healthy proxy strings."""
        return [h.proxy for h in self._proxies if not h.is_retired]

    def add_proxy(self, proxy: str) -> ProxyHealth | None:
        """
        Add a single proxy line to the pool (validated for format only).
        Accepts 'ip:port', 'http://ip:port', or 'user:pass@ip:port'.
        Returns the ProxyHealth entry, or None when the format is invalid.
        """
        from putusan3.infrastructure.sources import normalize_proxy_line

        normalized = normalize_proxy_line(proxy)
        if not normalized:
            logger.warning(f"Invalid proxy line ignored: {proxy!r}")
            return None

        if normalized in self._proxy_map:
            health = self._proxy_map[normalized]
            if health.is_retired:
                health.is_retired = False
                health.consecutive_failures = 0
                self._proxies.append(health)
                logger.info(f"Reactivated previously retired proxy: {normalized}")
            return health

        health = ProxyHealth(proxy=normalized)
        self._proxy_map[normalized] = health
        self._proxies.append(health)
        return health

    def save_pool(self, path: str, include_scheme: bool = False) -> int:
        """
        Write the healthy pool to a text file, one 'ip:port' per line.
        Skips comments/blank lines so the output is directly consumable by
        other tools (curl -x, browser proxy settings, or `python main.py load`).
        Returns the number of proxies written.
        """
        proxies = self.get_all_proxies()
        # Prefer proven proxies first (successes > 0, fastest latency ever)
        proxies.sort(
            key=lambda p: (
                -(self._proxy_map[p].successes),
                self._proxy_map[p].avg_latency_ms or float("inf"),
            )
        )
        _path = Path(path)
        if _path.parent.as_posix() not in ("", "."):
            _path.parent.mkdir(parents=True, exist_ok=True)
        body = (
            f"# Indonesian Proxy Scraper pool snapshot: "
            f"{len(proxies)} proxies (saved {time.strftime('%Y-%m-%d %H:%M:%S')})\n"
            + "".join(
                (f"http://{proxy}" if include_scheme else proxy) + "\n"
                for proxy in proxies
            )
        )
        # Atomic write: the conductor re-merges pool files every round, so a
        # torn write would poison the next resume with broken ip:port lines.
        safe_write(_path, body)
        logger.info(f"Saved {len(proxies)} proxies to {path}")
        return len(proxies)

    def load_from_text(self, path: str) -> int:
        """
        Load proxies from a text file into the pool (no network revalidation).
        Lines may be 'ip:port', 'http://ip:port', 'user:pass@ip:port', or
        '# comments'. Returns the number of proxies added/reactivated.
        """
        count = 0
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    stripped = line.strip()
                    if not stripped or stripped.startswith("#"):
                        continue  # comments/blanks are expected in saved files
                    if self.add_proxy(line):
                        count += 1
        except FileNotFoundError:
            logger.error(f"Proxy file not found: {path}")
            return 0
        logger.info(f"Loaded {count} proxies from {path}")
        return count

    async def shutdown(self):
        """Cleanup on shutdown."""
        logger.info(f"Proxy rotator shutdown. Final stats: {self.get_stats()}")


class ProxyPoolManager:
    """
    High-level manager for proxy pool lifecycle.
    Handles initialization, periodic refresh, and graceful shutdown.
    """

    def __init__(self, rotator: ProxyRotator | None = None):
        self.rotator = rotator or ProxyRotator()
        self._refresh_task: asyncio.Task | None = None
        self._running = False

    async def start(self):
        """Start the proxy pool manager."""
        self._running = True
        await self.rotator.initialize()
        self._refresh_task = asyncio.create_task(self._periodic_refresh())
        logger.info("Proxy pool manager started")

    def start_periodic_refresh(self):
        """Start only the periodic refresh task (pool was filled externally)."""
        self._running = True
        self._refresh_task = asyncio.create_task(self._periodic_refresh())
        logger.info(f"Proxy pool manager started (periodic refresh every "
                    f"{self.rotator.refresh_interval}s, no initial fetch)")

    async def stop(self):
        """Stop the proxy pool manager."""
        self._running = False
        if self._refresh_task:
            self._refresh_task.cancel()
            try:
                await self._refresh_task
            except asyncio.CancelledError:
                pass
        await self.rotator.shutdown()
        logger.info("Proxy pool manager stopped")

    async def _periodic_refresh(self):
        """Periodically refresh proxy pool."""
        while self._running:
            try:
                await asyncio.sleep(self.rotator.refresh_interval)
                if self._running:
                    await self.rotator.ensure_pool_size()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in periodic refresh: {e}")

    def get_proxy(self, strategy: str = "round_robin") -> ProxyHealth | None:
        """Get a proxy using specified strategy."""
        if strategy == "random":
            return self.rotator.get_random()
        elif strategy == "best_latency":
            best = self.rotator.get_best_by_latency(1)
            return best[0] if best else None
        else:  # round_robin
            return self.rotator.get_next()

    def record_result(self, proxy: str, success: bool, latency_ms: float = 0):
        """Record request result."""
        if success:
            self.rotator.record_success(proxy, latency_ms)
        else:
            self.rotator.record_failure(proxy)
