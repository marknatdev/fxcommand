"""The `desk` command line (M3 plan, "The desk CLI"): read-only market commands.

Every command takes its MT5 API as a parameter: `main(..., api=FakeMt5(...))` in tests, and the
live terminal, loaded only when a command needs it, otherwise. Nothing here can place an order
(`market/mt5_api.py`).

Nothing printed is a price. MT5's own SymbolInfo and Tick objects carry bids and asks, which for
today's dates are Holdout prices, so only named fields that aren't prices are printed: point,
digits, swaps, times, counts and spreads (ADR 0002).

Exit codes: 0 when everything passed, 1 when a check failed or a command could not finish, 2 for
bad usage.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import numpy as np

from vibedesk.market.bars import Timeframe
from vibedesk.market.cache import CacheError
from vibedesk.market.costs import CostError, measure_cost, record_cost
from vibedesk.market.grant import RESEARCH_START
from vibedesk.market.importer import ImportFailed, ImportReport, import_history, refresh
from vibedesk.market.mt5 import Mt5Error, recent_bars
from vibedesk.market.mt5_api import Mt5Api
from vibedesk.market.report import DataReport, data_report
from vibedesk.market.server_time import server_bound, server_to_utc, server_weekdays
from vibedesk.market.universe import SYMBOLS

DATA = Path(__file__).resolve().parents[2] / "data"  # the repo's data folder
MAX_TICK_LAG = 120  # seconds between the freshest tick and this computer's UTC clock
OK, FAILED, USAGE = 0, 1, 2

COMMANDS = {
    "smoke": "read-only check of the MT5 terminal: symbols, the live tick's UTC time, a little "
    "history",
    "import": "read-only import of every symbol and timeframe into the bar cache",
    "refresh": "re-import the latest chunks of every symbol and timeframe",
    "floors": "measure each symbol's Spread Floor and swap from recent ticks",
    "data-report": "first usable month, masked days and clock checks per symbol and year",
}


def main(
    argv: Sequence[str] | None = None,
    *,
    api: Mt5Api | None = None,
    clock: Callable[[], datetime] | None = None,
) -> int:
    parser = argparse.ArgumentParser(prog="desk", description="Vibe Desk market data commands")
    commands = parser.add_subparsers(dest="command", metavar="command")
    for name, summary in COMMANDS.items():
        command = commands.add_parser(name, help=summary)
        command.add_argument("--symbols", nargs="+", choices=SYMBOLS, default=list(SYMBOLS))
        if name != "smoke":
            command.add_argument("--data", type=Path, default=DATA, help=f"default {DATA}")
        if name == "import":
            command.add_argument("--start", type=date.fromisoformat, default=RESEARCH_START.date())
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return OK

    now = clock or (lambda: datetime.now(UTC))
    if args.command == "data-report":  # reads the cache only, never the terminal
        return report(args.data, args.symbols)
    try:
        terminal = api if api is not None else _real_api()
    except ImportError:
        print("FAILED: the live terminal needs the mt5 extra: uv sync --extra mt5")
        return FAILED
    if args.command == "smoke":
        return smoke(terminal, args.symbols, now())
    if args.command == "import":
        start = datetime.combine(args.start, datetime.min.time(), UTC)
        return _imported(lambda: import_history(terminal, args.data, symbols=args.symbols,
                                                start=start, clock=now))  # fmt: skip
    if args.command == "refresh":
        return _imported(lambda: refresh(terminal, args.data, symbols=args.symbols, clock=now))
    return floors(terminal, args.data, args.symbols, now())


def smoke(api: Mt5Api, symbols: Sequence[str], now: datetime) -> int:
    """Connect, read each symbol's details and last tick, check the tick's UTC time, read a day
    of M15 bars and an hour of ticks, and disconnect. Counts and times only."""
    print(f"desk smoke at {now:%Y-%m-%d %H:%M:%S} UTC")
    if not api.initialize():
        print(f"FAILED: MT5 initialize: {api.last_error()}")
        return FAILED
    problems: list[str] = []
    lags: dict[str, int] = {}
    try:
        for symbol in symbols:
            if not api.symbol_select(symbol, True):
                problems.append(f"{symbol}: symbol_select failed: {api.last_error()}")
                continue
            info = api.symbol_info(symbol)
            if info is None:
                problems.append(f"{symbol}: symbol_info failed: {api.last_error()}")
                continue
            print(
                f"  {symbol:<7} point {info.point:g}  digits {info.digits}  swap mode "
                f"{info.swap_mode}  triple-swap day {info.swap_rollover3days}"
            )
            tick = api.symbol_info_tick(symbol)
            if tick is None:
                problems.append(f"{symbol}: no last tick: {api.last_error()}")
                continue
            tick_utc = int(server_to_utc(np.array([int(tick.time)]))[0])
            lags[symbol] = int(now.timestamp()) - tick_utc
        problems += _tick_check(lags, now)
        if symbols:
            problems += _history_check(api, symbols[0], now)
    finally:
        api.shutdown()
    return _verdict(problems)


def floors(api: Mt5Api, root: Path, symbols: Sequence[str], now: datetime) -> int:
    """Measure and record each symbol's Spread Floor and swap; a failure is reported per
    symbol and the others carry on."""
    print(f"desk floors at {now:%Y-%m-%d %H:%M} UTC, 14 days of ticks, into {root}")
    if not api.initialize():
        print(f"FAILED: MT5 initialize: {api.last_error()}")
        return FAILED
    problems: list[str] = []
    try:
        for symbol in symbols:
            if not api.symbol_select(symbol, True):
                problems.append(f"{symbol}: symbol_select failed: {api.last_error()}")
                continue
            try:
                measurement = measure_cost(api, symbol, now)
            except CostError as error:
                problems.append(str(error))
                continue
            record_cost(root, measurement)
            charged = measurement.snapshot()
            print(
                f"  {symbol:<7} floor {measurement.spread_floor_points:g} points "
                f"(p90 {measurement.spread_p90_points:g}, {measurement.ticks:,} ticks)  swap long "
                f"{measurement.swap_long_points:g} / short {measurement.swap_short_points:g}, "
                f"charged {charged.swap_long_points:g} / {charged.swap_short_points:g}"
            )
    finally:
        api.shutdown()
    return _verdict(problems)


def report(root: Path, symbols: Sequence[str]) -> int:
    """The data report of each cached symbol; it fails when any year fails a clock check."""
    problems: list[str] = []
    for symbol in symbols:
        try:
            result = data_report(root, symbol)
        except CacheError as error:
            problems.append(f"{symbol}: {error}")
            continue
        _print_report(result)
        problems += [f"{symbol} {y.year}: a clock check failed" for y in result.years
                     if not y.passed]  # fmt: skip
    return _verdict(problems)


# Internals -----------------------------------------------------------------------------------


def _real_api() -> Mt5Api:
    from vibedesk.market.mt5_api import real_api  # loads MetaTrader5 only when a command runs

    return real_api()


def _tick_check(lags: dict[str, int], now: datetime) -> list[str]:
    """The freshest tick must be within MAX_TICK_LAG of this computer's UTC clock (either way:
    a clock running ahead is wrong too). FX keeps ticking through GOLD's daily break, so the
    freshest symbol decides. On a server Saturday or Sunday the market is shut: skipped."""
    if not lags:
        return ["live tick: no symbol returned a tick"]
    freshest = min(lags, key=lambda symbol: abs(lags[symbol]))
    lag = lags[freshest]
    if server_weekdays(np.array([int(now.timestamp())]))[0] >= 5:
        print(f"  live tick: skipped, the server's weekend (freshest {freshest}, {_ago(lag)})")
        return []
    if abs(lag) > MAX_TICK_LAG:
        return [
            f"live tick: the freshest tick ({freshest}) is {_ago(lag)}, more than "
            f"{MAX_TICK_LAG} s off. The terminal may not be receiving quotes (or the market is "
            "shut for a holiday); check its connection"
        ]
    print(f"  live tick: {freshest} {lag:+d} s from this computer's UTC clock")
    return []


def _history_check(api: Mt5Api, symbol: str, now: datetime) -> list[str]:
    """A day of M15 bars and an hour of ticks: counts and times, never prices."""
    try:
        count, last_open = recent_bars(api, symbol, Timeframe.M15, now, timedelta(days=1))
    except Mt5Error as error:
        return [str(error)]
    last = (
        f", the last opening {datetime.fromtimestamp(last_open, UTC):%Y-%m-%d %H:%M} UTC"
        if last_open is not None else ""
    )  # fmt: skip
    print(f"  {symbol} M15, last 24 hours: {count} bars{last}")
    epoch = int(now.timestamp())
    ticks = api.copy_ticks_range(
        symbol, server_bound(epoch - 3600), server_bound(epoch), api.COPY_TICKS_INFO
    )
    if ticks is None:
        return [f"{symbol}: copy_ticks_range failed: {api.last_error()}"]
    print(f"  {symbol} ticks, last hour: {len(ticks):,}")
    return []


def _imported(run: Callable[[], ImportReport]) -> int:
    try:
        result = run()
    except ImportFailed as error:
        print(f"FAILED: {error}")
        return FAILED
    print(f"import at {result.cutoff:%Y-%m-%d %H:%M:%S} UTC")
    for symbol in result.symbols:
        chunks = [c for c in result.chunks if c.chunk.symbol == symbol.symbol]
        judged = (f"judged through {symbol.judged_through}" if symbol.judged
                  else "no new day judged")  # fmt: skip
        print(
            f"  {symbol.symbol:<7} {len(chunks)} chunks, {sum(c.bars for c in chunks):,} bars, "
            f"{sum(c.reads - 2 for c in chunks)} retries; cut off at "
            f"{symbol.cutoff:%Y-%m-%d %H:%M} UTC; {judged}"
        )
    for chunk in result.flagged:
        print(f"  flagged {chunk.chunk}: {chunk.flag}")
    return OK


def _print_report(result: DataReport) -> None:
    first = f"{result.first_usable_month:%Y-%m}" if result.first_usable_month else "not judged"
    last = f"{result.last_import:%Y-%m-%d %H:%M} UTC" if result.last_import else "never"
    print(
        f"{result.symbol}: first usable month {first}, judged through {result.judged_through}, "
        f"last import {last}"
    )
    for year in result.years:
        bars = "  ".join(f"{tf} {year.bars.get(tf, 0):,}" for tf in Timeframe)
        spread = "-" if year.spread_median is None else f"{year.spread_median:g}"
        print(
            f"  {year.year}  {bars}  masked: {len(year.thin_days)} thin, "
            f"{len(year.empty_weekdays)} empty  gaps {year.gaps}  spread p50 {spread}"
        )
        if year.thin_days:
            shown = ", ".join(f"{day:%m-%d}" for day in year.thin_days[:8])
            more = f" and {len(year.thin_days) - 8} more" if len(year.thin_days) > 8 else ""
            print(f"        thin days: {shown}{more}")
        broken = {str(tf): n for tf, n in year.conversion.items() if n}
        print(f"        our conversion: {'ok' if not broken else f'FAIL {broken}'}")
        if year.xm is not None:
            xm = year.xm
            moved = {None: "n/a", True: "MOVED", False: "same"}[xm.break_moved]
            print(
                f"        XM's clock: {'ok' if xm.passed else 'FAIL'}  weekend bars "
                f"{xm.weekend_bars}, weeks over 5 D1 bars {xm.long_weeks}, daily break (NY+7) "
                f"US summer {_hours(xm.summer_break)} winter {_hours(xm.winter_break)} ({moved})"
            )


def _hours(hours: tuple[int, ...] | None) -> str:
    if hours is None:
        return "n/a"
    return "none" if not hours else ",".join(f"{h:02d}h" for h in hours)


def _ago(lag: int) -> str:
    if abs(lag) < 120:
        return f"{lag} s old" if lag >= 0 else f"{-lag} s in the future"
    hours = abs(lag) / 3600
    when = f"{hours:.1f} h" if hours >= 1 else f"{abs(lag) // 60} min"
    return f"{when} old" if lag >= 0 else f"{when} in the future"


def _verdict(problems: list[str]) -> int:
    if not problems:
        print("OK")
        return OK
    print(f"FAILED ({len(problems)}):")
    for problem in problems:
        print(f"  - {problem}")
    return FAILED


if __name__ == "__main__":
    sys.exit(main())
