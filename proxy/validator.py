"""Shim: moved to putusan3.infrastructure.validator."""
from putusan3.infrastructure.validator import (  # noqa: F401
    ProxyValidationResult,
    ProxyValidator,
    filter_anonymous_proxies,
    filter_indonesian_proxies,
    filter_working_proxies,
    get_best_proxies,
    proxy_public_ip,
    validate_proxies,
)
