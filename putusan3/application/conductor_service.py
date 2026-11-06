"""Use case that resumes harvest work after refreshing the proxy pool."""
from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Mapping
from pathlib import Path

from putusan3.application.ports import CommandRunnerPort, PageStorePort
from putusan3.domain.pagination_rules import (
    DEFAULT_POOL,
    DEFAULT_YEAR,
    SITE_MAX_PAGES,
    is_valid_listing,
)

LOG = logging.getLogger("putusan3.conductor")
PAGES_SCRIPT = Path("scripts") / "putusan3_pages.py"
LEGACY_CLI = Path("main.py")


def count_pages(out_dir: Path, store: PageStorePort) -> int:
    pages_dir = out_dir / "pages"
    return sum(
        1 for path in store.list_files(pages_dir, "list_*.html")
        if path.stem.removeprefix("list_").isdigit()
    )


def expected_last_page(out_dir: Path, store: PageStorePort) -> int:
    """Target page count for completion checks.

    Reads the real pagination discovered on page 1 (pagination.json,
    written by the pages run). Falls back to the site cap: some years are
    shorter than the 499-page cap, and without this the conductor would
    keep cycling refresh rounds for a target that can never be reached.
    """
    try:
        data = json.loads(store.read_text(out_dir / "pagination.json"))
    except (OSError, json.JSONDecodeError):
        return SITE_MAX_PAGES
    last = data.get("last_page") if isinstance(data, dict) else None
    if isinstance(last, int) and last >= 1:
        return min(last, SITE_MAX_PAGES)
    return SITE_MAX_PAGES


def pages_complete(out_dir: Path, store: PageStorePort, *, last_page: int | None = None) -> bool:
    """Only report completion when each expected page is a valid listing."""
    pages_dir = out_dir / "pages"
    end = last_page if last_page is not None else expected_last_page(out_dir, store)
    for page in range(1, end + 1):
        path = pages_dir / f"list_{page:04d}.html"
        try:
            if not is_valid_listing(store.read_text(path)):
                return False
        except OSError:
            return False
    return True


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Cycle pages+refresh until all pages saved")
    parser.add_argument("--year", type=int, default=DEFAULT_YEAR)
    parser.add_argument("--pool", default=DEFAULT_POOL)
    parser.add_argument("--out-dir", default=None, help="default data/harvest/<year>")
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--timeout", type=int, default=20_000)
    parser.add_argument("--max-attempts", type=int, default=24)
    parser.add_argument("--delay-min", type=float, default=1.0)
    parser.add_argument("--delay-max", type=float, default=3.0)
    parser.add_argument("--max-rounds", type=int, default=30,
                        help="max [fetch -> refresh] cycles")
    parser.add_argument("--test-n", type=int, default=450)
    parser.add_argument("--test-conc", type=int, default=6)
    parser.add_argument("--debug", action="store_true")
    return parser


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    return build_parser().parse_args(argv)


def run(
    args: argparse.Namespace,
    *,
    store: PageStorePort,
    command_runner: CommandRunnerPort,
    project_root: Path,
    environment: Mapping[str, str],
) -> int:
    out_arg = Path(args.out_dir or f"data/harvest/{args.year}")
    pool_arg = Path(args.pool)
    out = (out_arg if out_arg.is_absolute() else project_root / out_arg).resolve()
    pool_main = (pool_arg if pool_arg.is_absolute() else project_root / pool_arg).resolve()

    for rnd in range(1, args.max_rounds + 1):
        have = count_pages(out, store)
        expected = expected_last_page(out, store)
        print(f"\n[conductor] round {rnd}/{args.max_rounds}: {have}/{expected} "
              "pages on disk", flush=True)
        if have >= expected and pages_complete(out, store, last_page=expected):
            print("[conductor] COMPLETE: all pages on disk.")
            return 0

        pages_command = [
            sys.executable, str(project_root / PAGES_SCRIPT),
            "--year", str(args.year),
            "--pool", str(pool_main),
            "--out-dir", str(out),
            "--workers", str(args.workers),
            "--timeout", str(args.timeout),
            "--max-attempts", str(args.max_attempts),
            "--delay-min", str(args.delay_min),
            "--delay-max", str(args.delay_max),
        ]
        code = command_runner.run(
            pages_command, cwd=project_root, env=dict(environment))
        have = count_pages(out, store)
        expected = expected_last_page(out, store)
        print(f"[conductor] pages run exit={code}; {have}/{expected} pages on disk now",
              flush=True)
        if have >= expected and pages_complete(out, store, last_page=expected):
            print("[conductor] COMPLETE: all pages on disk.")
            return 0

        pool_dir = project_root / "data" / "pools"
        store.ensure_directory(pool_dir)
        fresh_pool = pool_dir / f"proxy_pool_r{rnd + 1}.txt"
        store.remove_file(fresh_pool)
        refresh_environment = {
            **environment,
            "MAX_PROXIES_TO_TEST": str(args.test_n),
            "MAX_CONCURRENT": str(args.test_conc),
        }
        print(f"[conductor] refresh: validate -> {fresh_pool.name} "
              f"(test {args.test_n} candidates, {args.test_conc} concurrent)",
              flush=True)
        refresh_code = command_runner.run(
            [sys.executable, str(project_root / LEGACY_CLI), "save", str(fresh_pool)],
            cwd=project_root,
            env=refresh_environment,
        )
        if refresh_code != 0 or not store.exists(fresh_pool):
            LOG.error("Proxy pool refresh failed in round %d (exit=%d).",
                      rnd, refresh_code)
            continue

        fresh = [
            line.strip()
            for line in store.read_text(fresh_pool, encoding="utf-8-sig").splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        try:
            old_lines = store.read_text(pool_main, encoding="utf-8-sig").splitlines()
        except FileNotFoundError:
            old_lines = []
        old = [line.strip() for line in old_lines
               if line.strip() and not line.strip().startswith("#")]
        merged = fresh + [line for line in old if line not in fresh]
        store.safe_write(
            pool_main,
            f"# conductor merge round {rnd}: fresh {len(fresh)}, total {len(merged)}\r\n"
            + "\r\n".join(merged) + "\r\n",
        )
        print(f"[conductor] merged pool: {len(merged)} proxies "
              f"({len(fresh)} fresh)", flush=True)

    final = count_pages(out, store)
    final_expected = expected_last_page(out, store)
    print(f"[conductor] EXHAUSTED max rounds; pages on disk final = "
          f"{final}/{final_expected}")
    print("             rerun the same command to continue later.")
    return 0 if final >= final_expected and pages_complete(out, store, last_page=final_expected) else 2


def main(
    argv: list[str] | None = None,
    *,
    store: PageStorePort,
    command_runner: CommandRunnerPort,
    project_root: Path,
    environment: Mapping[str, str],
) -> int:
    args = build_parser().parse_args(argv)
    return run(
        args,
        store=store,
        command_runner=command_runner,
        project_root=project_root,
        environment=environment,
    )
