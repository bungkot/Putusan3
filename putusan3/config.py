"""
Configuration settings for the Indonesian Proxy Scraper.
Environment variables can override these defaults.
"""
import os
import re

try:
    # Optional: loads a .env file if present (python-dotenv is in requirements)
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


class Config:
    """Application configuration with environment variable support."""

    # Target URLs to scrape (comma-separated in env)
    TARGET_URLS: list[str] = [
        url.strip()
        for url in os.getenv(
            "TARGET_URLS",
            "https://httpbin.org/ip,https://httpbin.org/headers",
        ).split(",")
        if url.strip()
    ]

    # Proxy settings
    PROXY_REFRESH_INTERVAL: int = int(os.getenv("PROXY_REFRESH_INTERVAL", "300"))  # seconds
    MIN_POOL_SIZE: int = int(os.getenv("MIN_POOL_SIZE", "5"))
    MAX_CONCURRENT: int = int(os.getenv("MAX_CONCURRENT", "3"))
    REQUEST_TIMEOUT: int = int(os.getenv("REQUEST_TIMEOUT", "60000"))  # milliseconds

    # Proxy validation
    VALIDATION_TIMEOUT: int = int(os.getenv("VALIDATION_TIMEOUT", "15000"))  # ms
    # NOTE: prefer http:// — most free proxies are HTTP-only (SSL errors on https);
    # /get echoes headers (needed for anonymity/transparency checks).
    VALIDATION_TEST_URL: str = os.getenv("VALIDATION_TEST_URL", "http://httpbin.org/get")
    MAX_PROXY_FAILURES: int = int(os.getenv("MAX_PROXY_FAILURES", "3"))

    # Proxy-source list fetch timeout (seconds). Kept separate from
    # VALIDATION_TIMEOUT so slow proxy-list pages don't tighten per-proxy tests.
    SOURCE_FETCH_TIMEOUT: float = float(
        os.getenv("SOURCE_FETCH_TIMEOUT", str(VALIDATION_TIMEOUT / 1000.0))
    )

    # Indonesian proxy filtering
    INDONESIAN_COUNTRY_CODE: str = os.getenv("INDONESIAN_COUNTRY_CODE", "ID")
    USE_GEOIP_FILTER: bool = os.getenv("USE_GEOIP_FILTER", "true").lower() == "true"

    # Browser settings
    HEADLESS: bool = os.getenv("HEADLESS", "true").lower() == "true"
    BROWSER_TYPE: str = os.getenv("BROWSER_TYPE", "chromium")  # chromium, firefox, webkit

    # Rate limiting
    MIN_DELAY: float = float(os.getenv("MIN_DELAY", "2.0"))  # seconds
    MAX_DELAY: float = float(os.getenv("MAX_DELAY", "10.0"))  # seconds

    # Retry settings
    MAX_RETRIES: int = int(os.getenv("MAX_RETRIES", "3"))
    RETRY_DELAY: float = float(os.getenv("RETRY_DELAY", "1.0"))  # seconds

    # Validation settings
    MAX_PROXIES_TO_TEST: int = int(os.getenv("MAX_PROXIES_TO_TEST", "50"))

    # Logging
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")

    # Results export (optional; .json appends to a list, .csv appends rows)
    RESULTS_PATH: str = os.getenv("RESULTS_PATH", "")

    # Optional: auto-save the healthy proxy pool to this file after each refresh
    # (one 'ip:port' per line; reusable via PROXY_LOAD_PATH or `main.py load`)
    PROXY_SAVE_PATH: str = os.getenv("PROXY_SAVE_PATH", "")

    # Optional: on startup, restore the pool from this text file instead of
    # running the (slow) network fetch; falls back to a full refresh if the
    # file is missing or empty.
    PROXY_LOAD_PATH: str = os.getenv("PROXY_LOAD_PATH", "")

    # Proxy sources (free proxy list APIs).
    # PROXY_SOURCES env var (comma/newline separated) overrides the defaults.
    _DEFAULT_PROXY_SOURCES: list[str] = [
        "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=http&timeout=10000&country=ID&ssl=all&anonymity=all",
        "https://www.proxy-list.download/api/v1/get?type=http&country=ID",
        "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt",
        "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt",
    ]
    PROXY_SOURCES: list[str] = [
        source.strip()
        for source in re.split(r"[,\n]", os.getenv("PROXY_SOURCES", ""))
        if source.strip()
    ] or _DEFAULT_PROXY_SOURCES

    @classmethod
    def validate(cls) -> bool:
        """Validate configuration."""
        if not cls.TARGET_URLS:
            raise ValueError("TARGET_URLS cannot be empty")
        if cls.MAX_CONCURRENT < 1:
            raise ValueError("MAX_CONCURRENT must be at least 1")
        if cls.MIN_POOL_SIZE < 1:
            raise ValueError("MIN_POOL_SIZE must be at least 1")
        if cls.REQUEST_TIMEOUT < 1000:
            raise ValueError("REQUEST_TIMEOUT must be at least 1000ms")
        return True


config = Config()
config.validate()
