"""
Playwright-based visitor for scraping through rotating Indonesian proxies.
"""
import asyncio
import json
import logging
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    ProxySettings,
    ViewportSize,
    async_playwright,
)

from putusan3.config import config
from putusan3.domain.pagination_rules import CHROME_UA  # single source of truth
from putusan3.infrastructure.rotator import ProxyHealth, ProxyRotator

logger = logging.getLogger(__name__)


@dataclass
class ScrapingResult:
    """Result of a scraping operation."""
    url: str
    proxy: str
    success: bool
    status_code: int | None = None
    content: str | None = None
    headers: dict[str, str] | None = None
    cookies: list[dict[str, Any]] | None = None
    latency_ms: float = 0
    error: str | None = None
    timestamp: float = field(default_factory=time.time)
    extracted: Any = None  # output of the per-visit `extract_fn`, if any


class ProxyVisitor:
    """
    Playwright visitor that uses rotating proxies.
    Manages browser contexts per proxy for session persistence.
    """

    def __init__(
        self,
        rotator: ProxyRotator,
        headless: bool | None = None,
        browser_type: str | None = None,
        ignore_https_errors: bool = True,
    ):
        self.rotator = rotator
        self.headless = headless if headless is not None else config.HEADLESS
        self.browser_type = browser_type or config.BROWSER_TYPE
        self.ignore_https_errors = ignore_https_errors

        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._contexts: dict[str, BrowserContext] = {}  # proxy -> context
        self._pages: dict[str, Page] = {}  # proxy -> page
        self._visit_locks: dict[str, asyncio.Lock] = {}
        self._user_agents = [
            # Chrome/131: UA proven live to pass Cloudflare for putusan3.
            CHROME_UA,
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:132.0) Gecko/20100101 Firefox/132.0",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
            "(KHTML, like Gecko) Version/17.6 Safari/605.1.15",
        ]

    async def __aenter__(self):
        await self.start()
        return self

    async def __aexit__(self, _exc_type, _exc_val, _exc_tb):
        await self.stop()

    async def start(self):
        """Initialize Playwright and browser."""
        self._playwright = await async_playwright().start()

        browser_map = {
            "chromium": self._playwright.chromium,
            "firefox": self._playwright.firefox,
            "webkit": self._playwright.webkit,
        }

        browser_launcher = browser_map.get(self.browser_type, self._playwright.chromium)
        self._browser = await browser_launcher.launch(headless=self.headless)

        logger.info(f"Playwright started with {self.browser_type} (headless={self.headless})")

    async def stop(self):
        """Clean up all resources."""
        # Close all contexts
        for proxy, context in self._contexts.items():
            try:
                await context.close()
            except Exception as e:
                logger.warning(f"Error closing context for {proxy}: {e}")

        self._contexts.clear()
        self._pages.clear()

        # Close browser
        if self._browser:
            try:
                await self._browser.close()
            except Exception as e:
                logger.warning(f"Error closing browser: {e}")
            self._browser = None

        # Stop playwright
        if self._playwright:
            try:
                await self._playwright.stop()
            except Exception as e:
                logger.warning(f"Error stopping playwright: {e}")
            self._playwright = None

        logger.info("Playwright stopped")

    def _create_proxy_settings(self, proxy: str) -> ProxySettings:
        """
        Create Playwright proxy settings from a proxy string.
        Supports optional credentials in 'user:pass@host:port' format.
        """
        server = proxy
        username = password = None

        if "@" in proxy:
            credentials, _, server = proxy.rpartition("@")
            if ":" in credentials:
                username, password = credentials.split(":", 1)
            else:
                username = credentials

        settings: ProxySettings = {
            "server": f"http://{server}",
            # Add bypass for local addresses if needed
            "bypass": "localhost,127.0.0.1",
        }
        if username:
            settings["username"] = username
        if password:
            settings["password"] = password
        return settings

    def _get_random_user_agent(self) -> str:
        """Get a random user agent."""
        return random.choice(self._user_agents)

    def _get_random_viewport(self) -> ViewportSize:
        """Get a random viewport size."""
        viewports: list[ViewportSize] = [
            {"width": 1920, "height": 1080},
            {"width": 1366, "height": 768},
            {"width": 1440, "height": 900},
            {"width": 1536, "height": 864},
            {"width": 1280, "height": 720},
        ]
        return random.choice(viewports)

    async def _get_or_create_context(self, proxy: str) -> BrowserContext:
        """Get existing context for proxy or create new one."""
        if proxy in self._contexts:
            return self._contexts[proxy]

        # Create new context with proxy
        proxy_settings = self._create_proxy_settings(proxy)
        browser = self._browser
        if browser is None:
            raise RuntimeError("Browser not started — call start() first.")

        context = await browser.new_context(
            proxy=proxy_settings,
            user_agent=self._get_random_user_agent(),
            viewport=self._get_random_viewport(),
            ignore_https_errors=self.ignore_https_errors,
            locale="id-ID",
            timezone_id="Asia/Jakarta",
        )

        # Add stealth-like settings
        await context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {
                get: () => undefined
            });
        """)

        self._contexts[proxy] = context
        logger.info(f"Created new browser context for proxy: {proxy}")
        return context

    async def _get_or_create_page(self, proxy: str) -> Page:
        """Get existing page for proxy or create new one."""
        if proxy in self._pages:
            page = self._pages[proxy]
            if not page.is_closed():
                return page

        context = await self._get_or_create_context(proxy)
        page = await context.new_page()

        # Set default timeout
        page.set_default_timeout(config.REQUEST_TIMEOUT)
        page.set_default_navigation_timeout(config.REQUEST_TIMEOUT)

        self._pages[proxy] = page
        return page

    def _pick_proxy(self, strategy: str) -> ProxyHealth | None:
        """Select the next proxy using the named rotation strategy.

        Supported: 'round_robin' (default), 'random', 'best_latency' — the
        same vocabulary ProxyPoolManager.get_proxy() already exposes.
        """
        if strategy == "random":
            return self.rotator.get_random()
        if strategy == "best_latency":
            best = self.rotator.get_best_by_latency(1)
            return best[0] if best else None
        return self.rotator.get_next()

    async def visit(
        self,
        url: str,
        proxy: str | None = None,
        wait_for: str = "networkidle",
        extract_fn: Callable[[Page], Awaitable[Any]] | None = None,
        strategy: str = "round_robin",
    ) -> ScrapingResult:
        """
        Visit a URL through a proxy.
        If proxy is not provided, get next from rotator using `strategy`.
        """
        # Get proxy from rotator if not provided
        if proxy is None:
            health = self._pick_proxy(strategy)
            if not health:
                return ScrapingResult(
                    url=url,
                    proxy="none",
                    success=False,
                    error="No healthy proxies available",
                )
            proxy = health.proxy

        lock = self._visit_locks.setdefault(proxy, asyncio.Lock())
        async with lock:
            return await self._visit_with_proxy(url, proxy, wait_for, extract_fn)

    async def _visit_with_proxy(
        self,
        url: str,
        proxy: str,
        wait_for: str,
        extract_fn: Callable[[Page], Awaitable[Any]] | None,
    ) -> ScrapingResult:
        start_time = time.time()

        try:
            page = await self._get_or_create_page(proxy)

            # Navigate to URL
            response = await page.goto(
                url, wait_until=wait_for  # type: ignore[arg-type]  # validated str
            )
            latency_ms = (time.time() - start_time) * 1000

            status_code = response.status if response else 0

            if status_code >= 400:
                self.rotator.record_failure(proxy)
                if self.rotator.is_retired(proxy):
                    await self._close_proxy_resources(proxy)
                return ScrapingResult(
                    url=url,
                    proxy=proxy,
                    success=False,
                    status_code=status_code,
                    latency_ms=latency_ms,
                    error=f"HTTP {status_code}",
                )

            # Extract content
            content = await page.content()

            # Get cookies (dict-copies; playwright's `Cookie` TypedDict → dict)
            cookies: list[dict[str, Any]] = [dict(c) for c in await page.context.cookies()]

            # Get response headers
            headers = dict(response.headers) if response else {}

            # Run custom extraction if provided
            extracted = None
            if extract_fn:
                try:
                    extracted = await extract_fn(page)
                except Exception as e:
                    logger.warning(f"Extraction function failed: {e}")

            self.rotator.record_success(proxy, latency_ms)

            return ScrapingResult(
                url=url,
                proxy=proxy,
                success=True,
                status_code=status_code,
                content=content,
                headers=headers,
                cookies=cookies,
                latency_ms=latency_ms,
                extracted=extracted,
            )

        except Exception as e:
            latency_ms = (time.time() - start_time) * 1000
            logger.warning(f"Visit failed for {url} via {proxy}: {e}")
            self.rotator.record_failure(proxy)
            if self.rotator.is_retired(proxy):
                await self._close_proxy_resources(proxy)

            return ScrapingResult(
                url=url,
                proxy=proxy,
                success=False,
                latency_ms=latency_ms,
                error=str(e),
            )

    async def visit_with_retry(
        self,
        url: str,
        max_retries: int | None = None,
        **kwargs,
    ) -> ScrapingResult:
        """Visit URL with automatic retry on failure."""
        # explicit `max_retries=0` must mean "no retries", not "falsy -> default"
        retries = config.MAX_RETRIES if max_retries is None else max(0, max_retries)
        last_error = None

        for attempt in range(retries):
            result = await self.visit(url, **kwargs)

            if result.success:
                return result

            last_error = result.error
            logger.warning(f"Attempt {attempt + 1}/{retries} failed for {url}: {last_error}")

            if attempt < retries - 1:
                # Wait before retry with exponential backoff
                delay = config.RETRY_DELAY * (2 ** attempt)
                await asyncio.sleep(delay + random.uniform(0, 1))

        return ScrapingResult(
            url=url,
            proxy="failed",
            success=False,
            error=f"All retries exhausted. Last error: {last_error}",
        )

    async def visit_multiple(
        self,
        urls: list[str],
        max_concurrent: int | None = None,
        **kwargs,
    ) -> list[ScrapingResult]:
        """Visit multiple URLs concurrently."""
        max_concurrent = max_concurrent or config.MAX_CONCURRENT
        semaphore = asyncio.Semaphore(max_concurrent)

        async def visit_with_semaphore(url: str) -> ScrapingResult:
            async with semaphore:
                # Add random delay between requests
                await asyncio.sleep(random.uniform(config.MIN_DELAY, config.MAX_DELAY))
                return await self.visit_with_retry(url, **kwargs)

        tasks = [visit_with_semaphore(url) for url in urls]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        processed: list[ScrapingResult] = []
        for url, result in zip(urls, results, strict=False):
            if isinstance(result, asyncio.CancelledError):
                raise result
            if isinstance(result, Exception):
                processed.append(ScrapingResult(
                    url=url,
                    proxy="error",
                    success=False,
                    error=str(result),
                ))
            elif isinstance(result, BaseException):
                raise result
            else:
                processed.append(result)

        return processed

    def get_context(self, proxy: str) -> BrowserContext | None:
        """Get existing context for proxy."""
        return self._contexts.get(proxy)

    async def _close_proxy_resources(self, proxy: str):
        """Close and remove the cached page/context for a proxy (e.g. after retirement)."""
        self._visit_locks.pop(proxy, None)  # keep the lock table bounded
        page = self._pages.pop(proxy, None)
        if page and not page.is_closed():
            try:
                await page.close()
            except Exception as e:
                logger.warning(f"Error closing page for {proxy}: {e}")

        context = self._contexts.pop(proxy, None)
        if context:
            try:
                await context.close()
            except Exception as e:
                logger.warning(f"Error closing context for {proxy}: {e}")
            logger.info(f"Closed browser context for retired proxy: {proxy}")

    def close_context(self, proxy: str):
        """Schedule removal of a proxy's context and page (fire-and-forget)."""
        if proxy in self._contexts or proxy in self._pages:
            asyncio.create_task(self._close_proxy_resources(proxy))


# Convenience functions for simple usage
async def visit_with_proxy(
    url: str,
    proxy: str,
    headless: bool = True,
    browser_type: str = "chromium",
    **kwargs,
) -> ScrapingResult:
    """Simple one-off visit with a specific proxy."""
    rotator = ProxyRotator()
    # Manually add the proxy
    health = ProxyHealth(proxy=proxy)
    rotator._proxies.append(health)
    rotator._proxy_map[proxy] = health

    async with ProxyVisitor(rotator, headless=headless, browser_type=browser_type) as visitor:
        return await visitor.visit(url, proxy=proxy, **kwargs)


async def scrape_with_rotating_proxies(
    urls: list[str],
    headless: bool = True,
    browser_type: str = "chromium",
    **kwargs,
) -> list[ScrapingResult]:
    """Scrape multiple URLs using rotating proxy pool."""
    rotator = ProxyRotator()
    await rotator.initialize()

    async with ProxyVisitor(rotator, headless=headless, browser_type=browser_type) as visitor:
        return await visitor.visit_multiple(urls, **kwargs)


# Extraction helpers
async def extract_text(page: Page, selector: str) -> str | None:
    """Extract text content from element."""
    try:
        element = await page.query_selector(selector)
        if element:
            return await element.inner_text()
    except Exception:
        pass
    return None


async def extract_html(page: Page, selector: str) -> str | None:
    """Extract HTML content from element."""
    try:
        element = await page.query_selector(selector)
        if element:
            return await element.inner_html()
    except Exception:
        pass
    return None


async def extract_attribute(page: Page, selector: str, attribute: str) -> str | None:
    """Extract attribute from element."""
    try:
        element = await page.query_selector(selector)
        if element:
            return await element.get_attribute(attribute)
    except Exception:
        pass
    return None


async def extract_all_text(page: Page, selector: str) -> list[str]:
    """Extract text from all matching elements."""
    try:
        elements = await page.query_selector_all(selector)
        return [await el.inner_text() for el in elements]
    except Exception:
        return []


async def extract_json_from_page(page: Page) -> dict | None:
    """Extract JSON from page (useful for API responses)."""
    try:
        # Try to find JSON in <pre> tag (common for API responses)
        pre = await page.query_selector("pre")
        if pre:
            text = await pre.inner_text()
            return json.loads(text)
    except Exception:
        pass
    return None
