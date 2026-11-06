"""Shim: moved to putusan3.infrastructure.sources."""
from putusan3.infrastructure.sources import (  # noqa: F401
    FreeProxyListNetSource,
    GeoNodeSource,
    GitHubRawSource,
    ProxyListDownloadSource,
    ProxyscrapeSource,
    ProxySource,
    create_sources,
    fetch_all_proxies,
    normalize_proxy_line,
)
