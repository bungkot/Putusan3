"""Putusan3 package (clean-architecture layout, Ports & Adapters).

Layers:
  domain          pure rules: constants/regexes, URL builders, parsers
  infrastructure  adapters: playwright engine, disk stores, proxy pool IO
  application     use-cases: harvest pages, conductor cycle, Harvest
  presentation    CLI entry-points (`python -m putusan3`)
"""
