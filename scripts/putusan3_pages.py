#!/usr/bin/env python3
"""Thin launcher for the composed pages use case."""
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

from putusan3.presentation.bootstrap import pages_main

if __name__ == "__main__":
    _sys.exit(pages_main())
