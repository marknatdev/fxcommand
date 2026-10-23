"""The `desk` command line (M3 plan, "The desk CLI").

The commands arrive in M3 T5; until then each one says so. Every command will take its MT5 API
as a parameter, so tests pass FakeMt5 and only a real run loads the terminal.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

COMMANDS = {
    "smoke": "read-only check of the MT5 terminal: symbols, the live tick's UTC time, a little "
    "history",
    "import": "read-only import of every symbol and timeframe into the bar cache",
    "refresh": "re-import the current and previous chunk of every symbol and timeframe",
    "floors": "measure each symbol's Spread Floor and swap from recent ticks",
    "data-report": "first usable month, masked days and clock checks per symbol and year",
}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="desk", description="Vibe Desk market data commands")
    commands = parser.add_subparsers(dest="command", metavar="command")
    for name, summary in COMMANDS.items():
        commands.add_parser(name, help=summary)
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    print(f"desk {args.command}: not built yet (M3 T5)", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
