"""Playwright fetching engine + proxy-pool text IO.

Infrastructure adapter. One Chromium; one BrowserContext per proxy
(cookies persist per proxy); round-robin lease with sticky-success,
consecutive-failure retirement, hard watchdog and dead-proxy fast-skip.
Moved verbatim from scripts/putusan3_harvest.py (restructure).
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from collections import deque
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from playwright.async_api import (  # noqa: F401  (typed aliases)
    Browser,
    BrowserContext,
    Page,
    async_playwright,
)

from putusan3.domain.pagination_rules import (  # noqa: E402
    CHROME_UA,
    DIRECT_KEY,
    STEALTH_INIT_SCRIPT,
    VIEWPORTS,
    is_challenge,
)

LOG = logging.getLogger("putusan3.engine")


def load_pool(path: Path) -> list[str]:
    """One 'ip:port' (or user:pass@ip:port) per line, '#' comments allowed."""
    if not path.is_file():
        LOG.error("Proxy pool file not found: %s", path)
        return []
    parsed: list[str] = []
    for line_no, raw in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        host = line.rpartition("@")[-1]
        if "://" in host:
            host = host.split("://", 1)[1]
        if ":" not in host or not host.split(":", 1)[1].isdigit():
            LOG.warning("pool line %d ignored, not an ip:port: %r", line_no, line)
            continue
        parsed.append(line)
    unique: list[str] = []
    seen: set[str] = set()
    for p in parsed:
        if p not in seen:
            seen.add(p)
            unique.append(p)
    LOG.info("Pool %s: %d proxies loaded", path, len(unique))
    return unique


def load_courts_file(path: Path) -> list[str]:
    slugs: list[str] = []
    seen: set[str] = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        slug = raw.strip().lower()
        if not slug or slug.startswith("#"):
            continue
        if "/" in slug:
            slug = slug.rstrip("/").rsplit("/", 1)[-1]
        slug = slug.removesuffix(".html")
        if slug and slug not in seen:
            seen.add(slug)
            slugs.append(slug)
    return slugs


class Fetcher:
    """One Chromium; one BrowserContext per proxy; leased per request.

    After the first successful navigation on a proxy (which obtains the
    Cloudflare clearance cookie), later requests go through `context.request`
    and fall back to a real navigation when that is not enough.
    """

    def __init__(
        self,
        pw,
        pool: Sequence[str],
        *,
        headed: bool,
        channel: str | None,
        timeout_ms: int,
        dead_after: int,
        max_attempts: int = 0,
        direct: bool = False,
    ) -> None:
        self._pw = pw
        self._pool = list(dict.fromkeys(pool))
        self._headed = headed
        self._channel = channel
        self._timeout = timeout_ms
        self._dead_after = dead_after
        self._browser: Browser | None = None
        self._contexts: dict[str, BrowserContext] = {}
        self._warmed: set[str] = set()
        self._dead: set[str] = set()
        self._consec_fail: dict[str, int] = {}
        self._direct = direct
        self._leased: set[str] = set()
        self._lease_guard = asyncio.Lock()
        self._preferred: str | None = None  # last proxy that validated OK
        self._dump_dir: str | Path | None = None  # set by callers to save rejected 200 bodies for diagnosis

        self._keys = list(self._pool) + ([DIRECT_KEY] if direct else [])
        self._available = deque(self._keys)
        self.max_attempts = min(max_attempts or max(4, 2 * len(self._keys or [1])), 24)

    @property
    def alive(self) -> int:
        return sum(1 for key in self._keys if key not in self._dead)

    @property
    def all_dead(self) -> bool:
        return self.alive <= 0

    def set_diagnostic_directory(self, path: Path) -> None:
        self._dump_dir = path

    async def _lease(self) -> str | None:
        """Wait for a live proxy slot; return None when the pool is exhausted."""
        while True:
            async with self._lease_guard:
                preferred = self._preferred
                if preferred and preferred not in self._dead and preferred in self._available:
                    self._available.remove(preferred)
                    self._leased.add(preferred)
                    return preferred

                while self._available:
                    key = self._available.popleft()
                    if key not in self._dead:
                        self._leased.add(key)
                        return key
                if self.alive <= 0:
                    return None
            await asyncio.sleep(0.1)

    async def _release(self, key: str) -> None:
        async with self._lease_guard:
            self._leased.discard(key)
            if key not in self._dead:
                if key == self._preferred:
                    self._available.appendleft(key)
                else:
                    self._available.append(key)

    async def start(self) -> None:
        launch_kwargs: dict[str, Any] = {"headless": not self._headed}
        if self._channel:
            launch_kwargs["channel"] = self._channel
        self._browser = await self._pw.chromium.launch(**launch_kwargs)

    async def close(self) -> None:
        for key, ctx in list(self._contexts.items()):
            try:
                await ctx.close()
            except Exception:
                pass
            self._contexts.pop(key, None)
        if self._browser:
            try:
                await self._browser.close()
            except Exception:
                pass
            self._browser = None

    # -- health ------------------------------------------------------------
    def _mark(self, key: str, ok: bool) -> None:
        if ok:
            self._consec_fail[key] = 0
            return
        n = self._consec_fail.get(key, 0) + 1
        self._consec_fail[key] = n
        if n >= self._dead_after and key not in self._dead:
            self._dead.add(key)
            LOG.warning("Proxy %s rested after %d consecutive failures (%d alive)",
                        key, n, self.alive)
            try:
                asyncio.get_running_loop().create_task(self._retire(key))
            except RuntimeError:
                pass

    async def _retire(self, key: str) -> None:
        self._dead.add(key)
        ctx = self._contexts.pop(key, None)
        if ctx:
            try:
                await ctx.close()
            except Exception:
                pass

    # -- contexts ----------------------------------------------------------
    async def _context(self, key: str) -> BrowserContext:
        ctx = self._contexts.get(key)
        if ctx is not None:
            return ctx
        assert self._browser is not None
        kwargs: dict[str, Any] = {
            "user_agent": CHROME_UA,
            "ignore_https_errors": True,
            "locale": "id-ID",
            "timezone_id": "Asia/Jakarta",
            "viewport": random.choice(VIEWPORTS),
        }
        if key != DIRECT_KEY:
            server = key.split("://", 1)[-1]
            proxy_settings: dict[str, str] = {
                "server": f"http://{server}",
                "bypass": "localhost,127.0.0.1",
            }
            if "@" in server:
                credentials, _, server = server.rpartition("@")
                username, separator, password = credentials.partition(":")
                proxy_settings["server"] = f"http://{server}"
                proxy_settings["username"] = username
                if separator:
                    proxy_settings["password"] = password
            kwargs["proxy"] = proxy_settings
        ctx = await self._browser.new_context(**kwargs)
        await ctx.add_init_script(STEALTH_INIT_SCRIPT)
        self._contexts[key] = ctx
        return ctx

    async def _fetch_once(self, key: str, url: str) -> tuple[str, dict[str, Any]]:
        # Hard watchdog: even if a single Playwright await wedges beyond its
        # own timeout (observed on Windows + headless-shell), this guarantees
        # the attempt aborts and rotation continues.
        return await asyncio.wait_for(
            self._fetch_once_inner(key, url),
            timeout=self._timeout / 1000 + 18,  # goto timeout + challenge wait + slack
        )

    async def _fetch_once_inner(self, key: str, url: str) -> tuple[str, dict[str, Any]]:
        started = time.time()
        ctx = await self._context(key)
        meta: dict[str, Any] = {"proxy": key, "url": url}

        # Fast path: no rendering, cookies/UA of this context are reused.
        if key in self._warmed:
            try:
                resp = await ctx.request.get(url, timeout=self._timeout)
                status = resp.status
                body = await resp.text()
                latency = (time.time() - started) * 1000
                meta.update(mode="request", status=status, latency_ms=round(latency))
                if status == 200 and not is_challenge(body, status):
                    return body, meta
            except Exception:
                LOG.debug("context.request failed on %s, falling back to goto", key)

        # Warm/slow path: a real navigation (also obtains Cloudflare clearance).
        page: Page | None = None
        try:
            page = await ctx.new_page()
            page.set_default_timeout(self._timeout)
            page.set_default_navigation_timeout(self._timeout)
            try:
                page_resp = await page.goto(
                    url, wait_until="domcontentloaded", timeout=self._timeout)
            except Exception:
                page_resp = await page.goto(
                    url, wait_until="commit", timeout=self._timeout)
            status = page_resp.status if page_resp else 0
            # Give Cloudflare's JS challenge a chance to finish (and the listing
            # markup to render) before validating the HTML.
            try:
                await page.wait_for_selector(
                    "[data-ci-pagination-page], #tabs-1, .spost, .pagging",
                    timeout=10_000,
                )
            except Exception:
                pass
            body = await page.content()
            latency = (time.time() - started) * 1000
            meta.update(mode="goto", status=status, latency_ms=round(latency))
            if status == 200:
                self._warmed.add(key)
            return body, meta
        finally:
            if page and not page.is_closed():
                try:
                    await page.close()
                except Exception:
                    pass

    async def get(
        self,
        url: str,
        validator: Callable[[str, int], bool] | None = None,
        label: str = "",
    ) -> tuple[str | None, dict[str, Any]]:
        """Fetch `url`, rotating through proxies. Never raises.

        Returns (html, meta); html is None when every attempt failed.
        """
        last: dict[str, Any] = {"url": url, "proxy": None, "status": 0}
        for attempt in range(1, self.max_attempts + 1):
            # Sticky success: keep using the proxy that last validated OK until
            # it fails (dead / challenge), then rotate again.
            key = await self._lease()
            if key is None:
                LOG.error("No live proxies left in the pool.")
                return None, last
            sticky = key == self._preferred
            LOG.info(" [%d/%d] %s -> %s%s", attempt, self.max_attempts,
                     label or url, key, " (sticky)" if sticky else "")
            try:
                body, meta = await self._fetch_once(key, url)
                ok = (validator or (lambda h, s: s == 200))(body, int(meta.get("status", 0)))
                if ok:
                    self._mark(key, True)
                    self._preferred = key
                    return body, meta
                last = meta
                status = int(meta.get("status", 0))
                self._preferred = None
                LOG.warning(" [%d/%d] %s via %s rejected (status=%s, %d chars) — rotating",
                            attempt, self.max_attempts, label or url, key,
                            status, len(body))
                if status == 200:
                    # 200-but-rejected is a suspected validator/parser bug or an
                    # unexpected route: keep the body so it can be diagnosed.
                    dump = Path(self._dump_dir or Path("."))
                    try:
                        dump.mkdir(parents=True, exist_ok=True)
                        (dump / "reject_200_last.html").write_text(body, encoding="utf-8")
                        LOG.warning("  200-but-invalid body dumped to %s", dump / "reject_200_last.html")
                    except OSError as wexc:
                        LOG.debug("dump failed: %s", wexc)
                if status == 429:  # rate-limited: pause longer before reusing ANY proxy
                    await asyncio.sleep(random.uniform(5, 8))
                self._mark(key, False)
            except Exception as exc:  # noqa: BLE001 — engine boundary
                self._preferred = None
                last = {"url": url, "proxy": key, "status": 0,
                        "error": f"{type(exc).__name__}: {str(exc)[:120]}"}
                LOG.warning(" [%d/%d] %s via %s skipped (dead proxy): %s — next proxy",
                            attempt, self.max_attempts, label or url, key,
                            last["error"])
                self._mark(key, False)
            finally:
                await self._release(key)
        return None, last
