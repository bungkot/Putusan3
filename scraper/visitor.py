"""Compatibility shim: `scraper.visitor` now lives in putusan3.infrastructure.visitor.

The module-level sys.modules alias keeps behavioural identity with the old
single-module layout, so `mock.patch("scraper.visitor.async_playwright")`
(and any other attribute patching) keeps working exactly as before.
"""
import sys

from putusan3.infrastructure import visitor as _impl  # noqa: F401

sys.modules[__name__] = _impl
