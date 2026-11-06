"""
Proxy source fetchers for free Indonesian proxy lists.
Supports multiple free proxy APIs and GitHub raw lists.
"""
import asyncio
import logging
import re

import httpx
from bs4 import BeautifulSoup

from putusan3.config import config

logger = logging.getLogger(__name__)

_IP_PORT_PATTERN = re.compile(r"^(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}):(\d{1,5})$")


def normalize_proxy_line(raw: str) -> str | None:
    """
    Normalize a single proxy token to 'ip:port' (or 'user:pass@ip:port').
    Accepts bare 'ip:port', lines with credentials, and URL-scheme forms.
    Returns None when the line does not contain a parsable proxy.
    """
    line = raw.strip()
    if not line or line.startswith("#"):
        return None

    match = _IP_PORT_PATTERN.match(line)
    if match:
        ip, port = match.groups()
        if _is_valid_ip(ip) and _is_valid_port(port):
            return line  # already canonical
        return None

    # Optional scheme prefix; credentials (user:pass@) are kept as-is
    body = line
    if "://" in body:
        body = body.split("://", 1)[1]
    if "@" in body:
        credentials, _, body = body.rpartition("@")
        body = f"{credentials}@{body}"
        host_part = body.rpartition("@")[-1]
    else:
        host_part = body

    if ":" not in host_part:
        return None
    host, port = host_part.rsplit(":", 1)
    if _is_valid_ip(host) and _is_valid_port(port):
        return body if "@" in body else f"{host}:{port}"
    return None


def _is_valid_ip(ip: str) -> bool:
    """Validate IPv4 address (module-level; shared across packages)."""
    parts = ip.split(".")
    if len(parts) != 4:
        return False
    return all(0 <= int(part) <= 255 for part in parts)


def _is_valid_port(port: str) -> bool:
    """Validate port number (module-level; shared across packages)."""
    try:
        p = int(port)
        return 1 <= p <= 65535
    except ValueError:
        return False


class ProxySource:
    """Base class for proxy sources."""

    def __init__(self, name: str, url: str):
        self.name = name
        self.url = url
        # Dedicated list-fetch timeout (seconds) — reading a source page may
        # legitimately take longer than per-proxy validation.
        self.timeout = config.SOURCE_FETCH_TIMEOUT

    async def fetch(self) -> list[str]:
        """Fetch proxies from this source. Returns list of 'ip:port' strings."""
        raise NotImplementedError

    def _normalize_proxies(self, raw_proxies: list[str]) -> list[str]:
        """Normalize proxy strings to 'ip:port' format."""
        normalized = []
        for proxy in raw_proxies:
            proxy = proxy.strip()
            if not proxy:
                continue
            parsed = normalize_proxy_line(proxy)
            if parsed:
                normalized.append(parsed)
        return normalized

    @staticmethod
    def _is_valid_ip(ip: str) -> bool:
        """Validate IPv4 address."""
        parts = ip.split(".")
        if len(parts) != 4:
            return False
        return all(0 <= int(part) <= 255 for part in parts)

    @staticmethod
    def _is_valid_port(port: str) -> bool:
        """Validate port number."""
        try:
            p = int(port)
            return 1 <= p <= 65535
        except ValueError:
            return False


class PlainListSource(ProxySource):
    """Source whose response body is a plain text list of proxies.

    One 'ip:port' per line; every line is normalised and validated
    (shared by proxyscrape, proxy-list.download, and GitHub raw lists).
    """

    async def fetch(self) -> list[str]:
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(self.url)
                response.raise_for_status()
                raw = response.text.strip().split("\n")
                return self._normalize_proxies(raw)
        except Exception as e:
            logger.warning(f"Failed to fetch from {self.name}: {e}")
            return []


class ProxyscrapeSource(PlainListSource):
    """Fetch from proxyscrape.com API."""


class ProxyListDownloadSource(PlainListSource):
    """Fetch from proxy-list.download API."""


class GitHubRawSource(PlainListSource):
    """Fetch from GitHub raw proxy lists."""


class FreeProxyListNetSource(ProxySource):
    """Scrape free-proxy-list.net for Indonesian proxies."""

    PAGE_URL = "https://free-proxy-list.net/"

    async def fetch(self) -> list[str]:
        try:
            # Use the configured URL if it points at the site, else the default page
            page_url = self.url if "free-proxy-list" in self.url else self.PAGE_URL
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(page_url)
                response.raise_for_status()

            soup = BeautifulSoup(response.text, "lxml")
            table = soup.find("table", {"id": "proxylisttable"})
            if not table:
                logger.warning("Could not find proxy table on free-proxy-list.net")
                return []
            tbody = table.find("tbody")
            if not tbody:
                logger.warning("Could not find tbody in proxy table on free-proxy-list.net")
                return []

            proxies = []
            for row in tbody.find_all("tr"):
                cols = row.find_all("td")
                if len(cols) >= 7:
                    ip = cols[0].text.strip()
                    port = cols[1].text.strip()
                    country = cols[2].text.strip()

                    # Filter for Indonesia (ID); HTTPS support is validated
                    # later by validator.py against the real egress endpoint
                    if country.upper() == "INDONESIA" or country.upper() == "ID":
                        if self._is_valid_ip(ip) and self._is_valid_port(port):
                            proxies.append(f"{ip}:{port}")

            logger.info(f"Found {len(proxies)} Indonesian proxies from free-proxy-list.net")
            return proxies
        except Exception as e:
            logger.warning(f"Failed to fetch from {self.name}: {e}")
            return []


class GeoNodeSource(ProxySource):
    """Fetch from geonode.com free proxy list."""

    API_URL = (
        "https://proxylist.geonode.com/api/proxy-list"
        "?limit=500&page=1&sort_by=lastChecked&sort_type=desc"
        "&protocols=http%2Chttps&country=ID"
    )

    async def fetch(self) -> list[str]:
        try:
            # Use the configured URL when it is a GeoNode API endpoint, else the default
            api_url = self.url if "proxylist.geonode.com/api" in self.url else self.API_URL
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(api_url)
                response.raise_for_status()
                data = response.json()

            proxies = []
            for item in data.get("data", []):
                ip = item.get("ip")
                port = item.get("port")
                if ip and port and self._is_valid_ip(ip) and self._is_valid_port(str(port)):
                    proxies.append(f"{ip}:{port}")

            logger.info(f"Found {len(proxies)} Indonesian proxies from GeoNode")
            return proxies
        except Exception as e:
            logger.warning(f"Failed to fetch from {self.name}: {e}")
            return []


# Registry of available sources
SOURCE_CLASSES = {
    "proxyscrape": ProxyscrapeSource,
    "proxy-list-download": ProxyListDownloadSource,
    "github-raw": GitHubRawSource,
    "free-proxy-list": FreeProxyListNetSource,
    "geonode": GeoNodeSource,
}


def create_sources() -> list[ProxySource]:
    """Create source instances from config."""
    sources: list[ProxySource] = []
    for url in config.PROXY_SOURCES:
        # Determine source type from URL
        if "proxyscrape" in url:
            sources.append(ProxyscrapeSource("proxyscrape", url))
        elif "proxy-list.download" in url:
            sources.append(ProxyListDownloadSource("proxy-list-download", url))
        elif "github.com" in url or "raw.githubusercontent.com" in url:
            sources.append(GitHubRawSource("github-raw", url))
        elif "geonode" in url:
            sources.append(GeoNodeSource("geonode", url))
        elif "free-proxy-list" in url:
            sources.append(FreeProxyListNetSource("free-proxy-list", url))
        else:
            sources.append(GitHubRawSource("unknown", url))
    return sources


def source_is_country_filtered(source: ProxySource) -> bool:
    """True when the source itself already filters to the target country.

    Sources whose URL query includes `country=ID` (proxyscrape,
    proxy-list.download) and classes that scrape ID-only pages
    (free-proxy-list.net, geonode) return only country-filtered candidates.
    The generic lists are ~40x larger, so merged random sampling would
    almost never give the country-filtered candidates validation budget —
    callers must prioritize them explicitly (see validator.validate_batch).
    """
    domain_sources = (FreeProxyListNetSource, GeoNodeSource)
    return isinstance(source, domain_sources) or "country=ID" in source.url


async def fetch_all_proxies_grouped() -> tuple[list[str], list[str]]:
    """
    Fetch proxies from all configured sources concurrently.

    Returns `(all_unique, country_prioritized)`: `country_prioritized`
    holds the deduplicated candidates that come from sources which
    already filter by country (=ID). Callers should give them validation
    priority — see `ProxyValidator.validate_batch(prioritized=...)`.
    """
    sources = create_sources()
    logger.info(f"Fetching proxies from {len(sources)} sources...")

    # Fetch from all sources concurrently
    tasks = [source.fetch() for source in sources]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    # Collect and deduplicate, keeping country-filtered provenance
    all_proxies: set[str] = set()
    country_prioritized: set[str] = set()
    for source, result in zip(sources, results, strict=False):
        if isinstance(result, Exception):
            logger.warning(f"Source {source.name} raised exception: {result}")
            continue
        if isinstance(result, list):
            all_proxies.update(result)
            if source_is_country_filtered(source):
                country_prioritized.update(result)
            logger.info(f"Source {source.name}: {len(result)} proxies")

    proxies_list = list(all_proxies)
    prioritized_list = list(country_prioritized)
    logger.info(
        f"Total unique proxies fetched: {len(proxies_list)} "
        f"(incl. {len(prioritized_list)} country-filtered)"
    )
    return proxies_list, prioritized_list


async def fetch_all_proxies() -> list[str]:
    """
    Fetch proxies from all configured sources concurrently.
    Returns deduplicated list of 'ip:port' strings.
    """
    return (await fetch_all_proxies_grouped())[0]
