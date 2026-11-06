#!/usr/bin/env python3
"""
putusan3_access.py — Standalone access tester for https://putusan3.mahkamahagung.go.id
=======================================================================================

Independent, single-file tool. Only dependency: Playwright + a Chromium-family
browser. Does NOT import anything from this repository — copy this file
anywhere and it still works.

What it does
------------
Iterates through a proxy list (plain text, one `ip:port` per line, `#`
comments allowed) and, for each candidate, opens a real Chromium context that:

  * announces itself as Google Chrome (UA: Chrome/131, Windows 10 x64)
  * hides `navigator.webdriver` (basic stealth)
  * browses as an Indonesian user (locale `id-ID`, timezone `Asia/Jakarta`)
  * tolerates the free-proxy certificate zoo (`ignore_https_errors=True`)

The first proxy that returns a healthy page wins:
  * full-page screenshot is written to `--screenshot`
  * (optional) rendered HTML snapshot to `--html`
  * (optional) JSON result record to `--json`

The target is Cloudflare-protected — raw HTTP clients receive a 403
challenge, so this tool never "prefilters" proxies with requests; the
browser itself is the validator.

Usage
-----
    python putusan3_access.py                          # default target, pool file
    python putusan3_access.py --pool my_list.txt
    python putusan3_access.py --headed                 # watch the browser live
    python putusan3_access.py --channel chrome         # use real installed Chrome
    python putusan3_access.py --url https://... --html page.html --json result.json

Exit codes: 0 = access verified, 1 = no proxy succeeded / no candidates.

Author: project Putusan3 — Indonesian Proxy Scraper. License: MIT.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import random
import sys
import time
from pathlib import Path
from typing import Any

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Response,
    async_playwright,
)

__version__ = "1.0.0"

LOG = logging.getLogger("putusan3_access")

DEFAULT_URL = "https://putusan3.mahkamahagung.go.id/direktori"
DEFAULT_POOL = "data/pools/proxy_pool.txt"
# Run artifacts belong in diagnostics/, not the repo root (workspace convention).
DEFAULT_SCREENSHOT = "diagnostics/screenshot_putusan3_direktori.png"
DEFAULT_HTML: str | None = None
DEFAULT_JSON: str | None = None

# Google Chrome user agent — current stable on Windows 10 x64.
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


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="putusan3_access.py",
        description=(
            "Verify that a page is reachable through the proxies in a list file, "
            "using a Google Chrome user agent in Playwright Chromium. "
            "Writes a full-page screenshot of the first successful attempt."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--url", default=DEFAULT_URL, help="Target URL to access")
    p.add_argument("--pool", default=DEFAULT_POOL, help="Proxy list file (ip:port per line)")
    p.add_argument("--screenshot", default=DEFAULT_SCREENSHOT,
                   help="Screenshot output path for the successful attempt")
    p.add_argument("--html", default=DEFAULT_HTML, metavar="FILE",
                   help="Also save the rendered HTML snapshot")
    p.add_argument("--json", default=DEFAULT_JSON, metavar="FILE",
                   help="Also save the attempt result record (JSON)")
    p.add_argument("--attempts", type=int, default=8,
                   help="Maximum number of proxies to try from the pool")
    p.add_argument("--timeout", type=int, default=55_000,
                   help="Navigation timeout in milliseconds")
    p.add_argument("--min-body", type=int, default=500,
                   help="Minimum rendered HTML length to accept a success")
    p.add_argument("--expect-text", default=None, metavar="TEXT",
                   help="Fail the attempt unless this text appears in the page title or body")
    p.add_argument("--headed", action="store_true", help="Run the browser visibly")
    p.add_argument("--channel", choices=["chrome", "msedge"], default=None,
                   help="Use an installed browser channel (real Chrome/Edge) instead of bundled Chromium")
    p.add_argument("--shuffle", action="store_true",
                   help="Randomize proxy order instead of 'proven first'")
    p.add_argument("--debug", action="store_true", help="Verbose DEBUG logging")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return p


# ---------------------------------------------------------------------------
# Proxy list handling
# ---------------------------------------------------------------------------

def load_pool(path: Path, shuffle: bool = False) -> list[str]:
    """Read a proxy text file: one 'ip:port' per line, '#' comments ignored."""
    if not path.is_file():
        LOG.error("Proxy pool file not found: %s", path)
        LOG.error("Generate one with your scraper, or create it manually:")
        LOG.error("    # proxy_pool.txt")
        LOG.error("    202.58.77.114:8080")
        LOG.error("    149.129.255.179:18080")
        return []

    parsed: list[str] = []
    for line_no, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        host = line.rpartition("@")[-1]
        if "://" in host:
            host = host.split("://", 1)[1]
        if ":" not in host or not host.split(":", 1)[1].isdigit():
            LOG.warning("pool line %d ignored, not an ip:port statement: %r", line_no, line)
            continue
        parsed.append(line)

    # De-duplicate, keep order (saved files put proven proxies first)
    seen: set[str] = set()
    unique = [p for p in parsed if not (p in seen or seen.add(p))]

    if shuffle:
        random.shuffle(unique)
    LOG.info("Pool %s: %d proxies loaded (from %d lines parsed)", path, len(unique), len(parsed))
    return unique


# ---------------------------------------------------------------------------
# Browser attempt
# ---------------------------------------------------------------------------

async def attempt(
    pw,  # Playwright object type is intentionally loose for portability
    proxy: str,
    url: str,
    *,
    headed: bool,
    channel: str | None,
    timeout_ms: int,
    min_body: int,
    expect_text: str | None,
    screenshot: Path,
    html_out: Path | None,
) -> dict[str, Any]:
    """One browser attempt against `url` through `proxy`. Never raises."""
    start = time.time()
    browser: Browser | None = None
    context: BrowserContext | None = None
    page: Page | None = None
    record: dict[str, Any] = {"proxy": proxy, "ok": False, "url": url}

    try:
        launch_kwargs: dict[str, Any] = {"headless": not headed}
        if channel:
            launch_kwargs["channel"] = channel
        browser = await pw.chromium.launch(**launch_kwargs)

        context = await browser.new_context(
            proxy={"server": f"http://{proxy}", "bypass": "localhost,127.0.0.1"},
            user_agent=CHROME_UA,
            ignore_https_errors=True,
            locale="id-ID",
            timezone_id="Asia/Jakarta",
            viewport=random.choice(VIEWPORTS),
        )
        await context.add_init_script(STEALTH_INIT_SCRIPT)
        page = await context.new_page()
        page.set_default_timeout(timeout_ms)
        page.set_default_navigation_timeout(timeout_ms)

        # networkidle first (best representation of "page loaded well"),
        # domcontentloaded as the fallback for noisy/free-proxy pages.
        response: Response | None
        try:
            response = await page.goto(url, wait_until="networkidle", timeout=timeout_ms - 5_000)
        except Exception:
            response = await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)

        latency_ms = (time.time() - start) * 1000
        status = response.status if response else 0
        content = await page.content()
        title = await page.title()

        record.update(status=status, title=title, body_chars=len(content), latency_ms=latency_ms)

        if status >= 400:
            record["error"] = f"HTTP {status}"
            return record
        if len(content) < min_body:
            record["error"] = f"body too small ({len(content)} < {min_body} chars; challenge page?)"
            return record
        if expect_text and expect_text.lower() not in (title + content).lower():
            record["error"] = f"expected text {expect_text!r} not found in title/body"
            return record

        # Success — persist the evidence first, then report.
        screenshot.parent.mkdir(parents=True, exist_ok=True)
        await page.screenshot(path=str(screenshot), full_page=True)
        record["screenshot"] = str(screenshot)
        if html_out:
            html_out.write_text(content, encoding="utf-8")
            record["html_snapshot"] = str(html_out)

        record["ok"] = True
        return record

    except Exception as exc:  # noqa: BLE001 — boundary of the tool: every failure must be a record
        record["error"] = f"{type(exc).__name__}: {str(exc)[:120]}"
        record["latency_ms"] = (time.time() - start) * 1000
        return record
    finally:
        for closer in (page, context):
            if closer:
                try:
                    await closer.close()
                except Exception:
                    LOG.debug("Ignored cleanup error closing page/context for %s", proxy)
        if browser:
            try:
                await browser.close()
            except Exception:
                LOG.debug("Ignored cleanup error closing browser for %s", proxy)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

async def run(args: argparse.Namespace) -> int:
    pool_path = Path(args.pool)
    screenshot_path = Path(args.screenshot)
    html_path = Path(args.html) if args.html else None
    json_path = Path(args.json) if args.json else None

    proxies = load_pool(pool_path, shuffle=args.shuffle)
    if not proxies:
        return 1
    proxies = proxies[: args.attempts]

    LOG.info(
        "Access test: %s  (ua=Chrome, headless=%s, channel=%s, attempts=%d)",
        args.url, not args.headed, args.channel or "chromium-bundled", len(proxies),
    )
    print()
    print("=" * 72)
    print(f"{'ATTEMPT ROUND':^72}")
    print("=" * 72)

    all_attempts: list[dict[str, Any]] = []
    async with async_playwright() as pw:
        for idx, proxy in enumerate(proxies, 1):
            print(f"  [{idx}/{len(proxies)}] {proxy} ...", flush=True)
            record = await attempt(
                pw,
                proxy,
                args.url,
                headed=args.headed,
                channel=args.channel,
                timeout_ms=args.timeout,
                min_body=args.min_body,
                expect_text=args.expect_text,
                screenshot=screenshot_path,
                html_out=html_path,
            )
            all_attempts.append(record)

            if record["ok"]:
                print(f"      OK     status={record['status']}  {record['latency_ms']:.0f} ms")
                print(f"      title  : {record['title']!r}")
                print(f"      body   : {record['body_chars']:,} chars")
                print(f"      sample : {record['screenshot']}")
                if json_path:
                    payload = {
                        "url": args.url,
                        "verified_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                        "success": True,
                        "attempts": all_attempts,
                    }
                    json_path.write_text(
                        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
                    )
                    print(f"      record : {json_path}")
                print()
                print("ACCESS VERIFIED")
                return 0
            print(f"      FAIL   {record['error']}  ({record['latency_ms']:.0f} ms)")

    if json_path:
        payload = {
            "url": args.url,
            "verified_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "success": False,
            "attempts": all_attempts,
        }
        json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"  record : {json_path}")

    print()
    print("NO PROXY REACHED THE TARGET — every attempt failed. See messages above.")
    return 1


def main() -> int:
    args = build_parser().parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\nInterrupted by user — shutting down cleanly.")
        return 130
    except Exception as exc:  # noqa: BLE001
        LOG.exception("Fatal error: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
