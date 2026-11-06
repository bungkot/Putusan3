"""
Result export helpers: persist ScrapingResult objects to JSON or CSV.

- .json path: the file holds a JSON array; new results are appended.
- .csv path: rows are appended; a header is written when the file is created.
"""
import csv
import json
import logging
import os
import threading
from dataclasses import asdict
from pathlib import Path
from typing import Any

from putusan3.infrastructure.page_store import safe_write
from putusan3.infrastructure.visitor import ScrapingResult

logger = logging.getLogger(__name__)

# Serializes read-modify-write / append cycles within one process. Concurrent
# exports previously raced: two writers could each read the old JSON array and
# overwrite each other's results.
_export_lock = threading.Lock()

# Columns written for CSV export; the full dataclass has nested structures
# (cookies, headers) that do not map cleanly to flat rows.
CSV_FIELDS = [
    "url",
    "proxy",
    "success",
    "status_code",
    "latency_ms",
    "error",
    "timestamp",
]


def _ensure_parent_dir(path: str):
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)


def export_results(results: list[ScrapingResult], path: str) -> str | None:
    """
    Append results to a .json (array) or .csv file, chosen by extension.
    Returns the path on success, None for empty input, and raises on write errors.
    """
    if not results:
        return None

    if path.lower().endswith(".csv"):
        return _export_csv(results, path)
    return _export_json(results, path)


def _to_row(result: ScrapingResult) -> dict:
    """Flatten a ScrapingResult for export."""
    data: dict[str, Any] = asdict(result)
    data["proxy"] = data.get("proxy") or ""
    return data


def _export_json(results: list[ScrapingResult], path: str) -> str:
    _ensure_parent_dir(path)

    with _export_lock:
        existing: list[Any] = []
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                loaded = json.load(f)
            if not isinstance(loaded, list):
                raise ValueError(f"Existing results file must contain a JSON array: {path}")
            existing = loaded

        existing.extend([_to_row(r) for r in results])
        safe_write(
            Path(path),
            json.dumps(existing, indent=2, ensure_ascii=False),
        )

    logger.info(f"Exported {len(results)} results to {path} (total: {len(existing)})")
    return path


def _export_csv(results: list[ScrapingResult], path: str) -> str:
    _ensure_parent_dir(path)

    with _export_lock:
        write_header = not os.path.exists(path)
        with open(path, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
            if write_header:
                writer.writeheader()
            for result in results:
                writer.writerow(_to_row(result))

    logger.info(f"Appended {len(results)} results to {path}")
    return path
