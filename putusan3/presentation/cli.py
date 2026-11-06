"""Presentation layer: `python -m putusan3 pages|conduct|harvest [...argv]`."""
from __future__ import annotations

import sys
from collections.abc import Callable


def route_command(argv: list[str]) -> int:
    """Dispatch to the right use-case; keeps CLI-level errors non-fatal."""
    runner: Callable[[list[str] | None], int]
    if argv and argv[0] in ("-h", "--help"):
        print("usage: python -m putusan3 pages|conduct|harvest [--help]")
        return 0
    if argv and argv[0] == "pages":
        from putusan3.presentation.bootstrap import pages_main

        runner = pages_main
        return runner(argv[1:])
    if argv and argv[0] == "conduct":
        from putusan3.presentation.bootstrap import conductor_main

        runner = conductor_main
        return runner(argv[1:])
    if argv and argv[0] == "harvest":
        from putusan3.presentation.harvest_cli import main as harvest_main

        runner = harvest_main
        return runner(argv[1:])
    print("usage: python -m putusan3 pages|conduct|harvest [--help]")
    return 1


def main() -> int:
    return route_command(sys.argv[1:])


if __name__ == "__main__":  # `python -m putusan3.cli`
    sys.exit(main())
