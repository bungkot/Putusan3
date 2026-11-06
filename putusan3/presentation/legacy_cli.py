"""
Main entry point for Indonesian Proxy Scraper.
Orchestrates proxy fetch → validate → rotate → scrape.
"""
import asyncio
import logging
import os
import signal
import sys

from putusan3.config import config
from putusan3.domain.pagination_rules import DEFAULT_POOL
from putusan3.infrastructure.results_store import export_results
from putusan3.infrastructure.rotator import ProxyPoolManager
from putusan3.infrastructure.visitor import ProxyVisitor, ScrapingResult

# Configure logging
logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL.upper()),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


class ScraperApp:
    """Main application class."""

    def __init__(self) -> None:
        self.pool_manager: ProxyPoolManager | None = None
        self.visitor: ProxyVisitor | None = None
        self._running = False
        self._shutdown_event = asyncio.Event()

    @property
    def pool(self) -> ProxyPoolManager:
        if self.pool_manager is None:
            raise RuntimeError("Proxy pool manager has not been initialized.")
        return self.pool_manager

    @property
    def active_visitor(self) -> ProxyVisitor:
        if self.visitor is None:
            raise RuntimeError("Proxy visitor has not been initialized.")
        return self.visitor

    async def initialize(
        self,
        refresh: bool = True,
        allow_empty: bool = False,
        need_visitor: bool = True,
    ) -> bool:
        """Initialize proxy pool and (optionally) the browser visitor.

        When `config.PROXY_LOAD_PATH` is set, the pool is (re)loaded from that
        text file regardless of `refresh` (so pool-ops commands like `save`
        can operate on a restored pool) — and a full network refresh only
        happens when `refresh=True` as a fallback if the file yields nothing.
        With no PROXY_LOAD_PATH, refresh=True performs the network fetch and
        refresh=False skips pool filling entirely (`load` fills it itself).

        `allow_empty=True` skips the healthy-pool gate and browser startup
        (for pool-ops commands like `save`, which manage the pool themselves)

        `need_visitor=False` skips browser startup for commands whose work is
        purely pool-side (e.g. `list-proxies` just prints pool stats) — the
        browser is only launched when actually required, so machines without
        Chromium installed can still run pool-ops commands.
        """
        logger.info("Initializing Indonesian Proxy Scraper...")

        # Initialize proxy pool manager
        self.pool_manager = ProxyPoolManager()
        if config.PROXY_LOAD_PATH:
            count = self.pool.rotator.load_from_text(config.PROXY_LOAD_PATH)
            if count:
                logger.info(f"Pool restored from {config.PROXY_LOAD_PATH} "
                            f"({count} proxies)")
                if refresh:
                    self.pool.start_periodic_refresh()
            elif refresh:
                logger.warning(f"Could not load pool from {config.PROXY_LOAD_PATH}, "
                               "falling back to network refresh")
                await self.pool.start()
        elif refresh:
            await self.pool.start()
        # refresh=False without PROXY_LOAD_PATH: pool will be filled by
        # `load` from a text file.

        if allow_empty:
            stats = self.pool.rotator.get_stats()
            logger.info(f"Proxy pool stats: {stats} (empty allowed)")
            return True

        # Check if we have proxies
        stats = self.pool.rotator.get_stats()
        logger.info(f"Proxy pool stats: {stats}")

        if stats["healthy"] == 0:
            logger.error("No healthy proxies available! Exiting.")
            return False

        # Initialize visitor — only for commands that actually visit URLs.
        # Pool-ops commands (save / list-proxies / load) work on the pool
        # alone and must not require a browser to be installed.
        if need_visitor:
            rotator = self.pool.rotator
            self.visitor = ProxyVisitor(rotator)
            await self.active_visitor.start()

        return True

    async def run_single_test(self, url: str | None = None) -> ScrapingResult:
        """Run a single test visit to verify proxy is working."""
        test_url = url or config.VALIDATION_TEST_URL
        logger.info(f"Running single test visit to: {test_url}")

        result = await self.active_visitor.visit_with_retry(test_url)

        if result.success:
            logger.info(f"✅ Success via {result.proxy} ({result.latency_ms:.0f}ms)")
            logger.info(f"   Status: {result.status_code}")
            # Print first 500 chars of content
            if result.content:
                preview = result.content[:500].replace('\n', ' ')
                logger.info(f"   Content preview: {preview}...")
        else:
            logger.error(f"❌ Failed: {result.error}")

        self._export_results([result])
        return result

    async def run_multiple_tests(self, urls: list[str] | None = None) -> list[ScrapingResult]:
        """Run multiple test visits."""
        test_urls = urls or config.TARGET_URLS
        logger.info(f"Running tests for {len(test_urls)} URLs...")

        results = await self.active_visitor.visit_multiple(test_urls)

        success_count = sum(1 for r in results if r.success)
        logger.info(f"Results: {success_count}/{len(results)} successful")

        for result in results:
            status = "✅" if result.success else "❌"
            proxy_info = f" via {result.proxy}" if result.proxy else ""
            logger.info(f"  {status} {result.url}{proxy_info} ({result.latency_ms:.0f}ms)")

        self._export_results(results)
        return results

    def _export_results(self, results: list[ScrapingResult]) -> None:
        """Append scrape results to config.RESULTS_PATH when configured."""
        if not config.RESULTS_PATH:
            return
        path = export_results(results, config.RESULTS_PATH)
        if path:
            logger.info(f"Results appended to: {path}")

    async def run_continuous(self, interval: int = 60):
        """Run continuous scraping at interval."""
        self._running = True
        logger.info(f"Starting continuous scraping every {interval}s. Press Ctrl+C to stop.")

        while self._running and not self._shutdown_event.is_set():
            try:
                # Ensure pool has enough proxies
                await self.pool.rotator.ensure_pool_size()

                # Run scraping
                await self.run_multiple_tests()

                # Wait for next interval
                await asyncio.sleep(interval)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in continuous run: {e}")
                await asyncio.sleep(10)  # Back off on error

    def shutdown(self) -> None:
        """Signal shutdown."""
        logger.info("Shutdown signal received...")
        self._running = False
        self._shutdown_event.set()

    async def cleanup(self) -> None:
        """Clean up resources."""
        logger.info("Cleaning up...")
        if self.visitor:
            await self.visitor.stop()
        if self.pool_manager:
            await self.pool_manager.stop()
        logger.info("Cleanup complete")


async def main() -> int:
    """Main entry point."""
    app = ScraperApp()

    # Setup signal handlers (Unix only)
    try:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, app.shutdown)
    except NotImplementedError:
        # Windows doesn't support add_signal_handler
        pass

    # Commands that do not want a (slow) network refresh at startup;
    # `load` fills the pool from its own text file instead.
    command = sys.argv[1] if len(sys.argv) > 1 else None

    if command == "load":
        # Fill the pool from a text file (no network, no browser startup)
        path = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_POOL
        app.pool_manager = ProxyPoolManager()
        count = app.pool.rotator.load_from_text(path)
        stats = app.pool.rotator.get_stats()
        print(f"Loaded {count} proxies from {path}; pool stats: {stats}")
        await app.cleanup()
        return 0 if stats["healthy"] > 0 else 1

    if command == "save":
        # Save the pool: same path handling, no browser needed
        app.pool_manager = ProxyPoolManager()

    refresh = command != "save"

    # Initialize — the browser (visitor) is only started for commands that
    # actually visit URLs; pool-ops commands must not require Chromium.
    visitor_commands = {"test", "test-all", "continuous"}
    if not await app.initialize(
        refresh=refresh,
        allow_empty=(command == "save"),
        need_visitor=command in visitor_commands,
    ):
        await app.cleanup()
        return 1

    # Check command line arguments
    if len(sys.argv) > 1:

        if command == "test":
            # Single test
            url = sys.argv[2] if len(sys.argv) > 2 else None
            result = await app.run_single_test(url)
            await app.cleanup()
            return 0 if result.success else 1

        elif command == "test-all":
            # Test all configured URLs
            await app.run_multiple_tests()
            await app.cleanup()
            return 0

        elif command == "continuous":
            # Continuous mode
            interval = int(sys.argv[2]) if len(sys.argv) > 2 else 60
            await app.run_continuous(interval)
            await app.cleanup()
            return 0

        elif command == "list-proxies":
            # List available proxies
            proxies = app.pool.rotator.get_all_proxies()
            stats = app.pool.rotator.get_stats()
            print(f"Proxy Pool Stats: {stats}")
            print(f"\nAvailable proxies ({len(proxies)}):")
            for i, proxy in enumerate(proxies, 1):
                health = app.pool.rotator._proxy_map.get(proxy)
                if health:
                    print(f"  {i}. {proxy} - Success: {health.successes}, Failures: {health.failures}, "
                          f"Avg Latency: {health.avg_latency_ms:.0f}ms")
            await app.cleanup()
            return 0

        elif command == "save":
            # Save the healthy proxy pool to a text file (ip:port per line)
            path = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_POOL
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

            proxies = app.pool.rotator.get_all_proxies()
            if not proxies:
                logger.info("Pool empty, refreshing from sources first...")
                await app.pool.rotator.refresh()
                proxies = app.pool.rotator.get_all_proxies()

            if not proxies:
                logger.error("No proxies available to save.")
                await app.cleanup()
                return 1

            count = app.pool.rotator.save_pool(path)
            print(f"Saved {count} proxies to {path}")
            await app.cleanup()
            return 0

    # Default: run single test
    logger.info("Running default single test...")
    result = await app.run_single_test()
    await app.cleanup()
    return 0 if result.success else 1


if __name__ == "__main__":
    try:
        exit_code = asyncio.run(main())
        sys.exit(exit_code)
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
        sys.exit(0)
    except Exception as e:
        logger.exception(f"Fatal error: {e}")
        sys.exit(1)
