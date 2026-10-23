"""The read-only importer: MT5 history into the bar cache (M3 plan, "Import and cache").

Every import fixes a cutoff when it starts and keeps only the bars that closed by then, so the
bar still forming is never cached for good. Each symbol's cutoff is the earlier of the clock and
its last tick: a terminal that stopped receiving quotes has no bars for the hours since, and
judging those hours would freeze them as thin, or as empty days that a later backfill serves
unjudged.

It reads chunk by chunk, M1 by UTC calendar month and the other timeframes by UTC calendar year,
and accepts a chunk only when two reads in a row agree on its closed bars: a terminal still
downloading history returns less at first, and a live market adds a bar between two reads, which
the cutoff hides. Four guards keep a bad read out of what Trials see:
- a chunk may not change bars on server days the mask has already judged: those are served, and
  a Trial's bars, and their fingerprint in the Ledger, must stay the same. The import fails,
  naming the chunk and the first day that changed, and writes nothing for it;
- a chunk that looks short is flagged in the report, and a flagged M15 chunk stops its symbol
  before any day is judged, since the mask is judged on M15;
- a terminal that has just reconnected has a live tick, but its history can still stop days
  before it, and two reads agree on that. So when the run's last closed M15 bar closed more than
  MAX_LAG before the symbol's cutoff, the symbol fails before any day is judged. Two hours allow
  for GOLD's daily break; at a weekend the tick itself is stale, so the lag stays small;
- days are judged (`record_import`) only after every chunk of the symbol settled, only when the
  run imported M15, and only up to the day of the run's last M15 bar, which stays open: it may
  still be filling, and a day after it is empty because the history stops there, not because the
  market was shut. So at a weekend, Friday waits for the import that brings Monday's bars. An
  import that fails half way has judged nothing, so serves nothing new.

A judged day is served on every timeframe at once, so a run that imports M15, and so judges, must
import every timeframe in TIMEFRAMES: one it left out would find days judged without its bars,
and every later import of it would be refused there. A refresh of some timeframes only leaves out
M15, and judges nothing.

`refresh` re-imports from the earlier of the chunk before the cutoff's and the chunk of the last
import, through the cutoff's chunk. That covers a month or year that ended since the last import,
and a refresh missed for a while.

One importer at a time, by a lock file under the cache. The report holds counts only, never a
price, since the cache reaches into the Holdout (ADR 0002).
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import numpy as np

from vibedesk.market.bars import Bars, Timeframe
from vibedesk.market.cache import (
    CacheError,
    read_meta,
    read_unmasked,
    record_import,
    write_chunk,
)
from vibedesk.market.grant import RESEARCH_START
from vibedesk.market.mt5 import Mt5BarSource
from vibedesk.market.mt5_api import Mt5Api
from vibedesk.market.server_time import server_days, server_to_utc
from vibedesk.market.universe import SYMBOLS

TIMEFRAMES = (Timeframe.M1, Timeframe.M15, Timeframe.H1, Timeframe.H4, Timeframe.D1)
MAX_READS = 6  # reads of one chunk before it counts as never settling
LATE_START = timedelta(days=4)  # longer than a weekend plus a New Year holiday
MAX_LAG = timedelta(hours=2)  # from the last M15 bar's close to the cutoff; over GOLD's break
_ARRAYS = ("time", "open", "high", "low", "close", "spread")
_EPOCH = date(1970, 1, 1)


class ImportFailed(Exception):
    """The import stopped; the message names the symbol, timeframe and range where it could."""


class ImportLocked(ImportFailed):
    """Another import holds the lock."""


@dataclass(frozen=True, slots=True)
class Chunk:
    symbol: str
    timeframe: Timeframe
    year: int
    month: int | None  # M1 chunks are months; the others whole years

    @property
    def start(self) -> datetime:
        return datetime(self.year, self.month or 1, 1, tzinfo=UTC)

    @property
    def end(self) -> datetime:
        if self.month is None:
            return datetime(self.year + 1, 1, 1, tzinfo=UTC)
        return datetime(self.year + self.month // 12, self.month % 12 + 1, 1, tzinfo=UTC)

    def __str__(self) -> str:
        span = f"{self.year}" if self.month is None else f"{self.year}-{self.month:02d}"
        return f"{self.symbol} {self.timeframe} {span}"


@dataclass(frozen=True, slots=True)
class ChunkReport:
    chunk: Chunk
    bars: int  # closed bars cached
    reads: int  # 2 when the first two reads agreed
    flag: str | None = None  # why the chunk looks short, e.g. a capped read


@dataclass(frozen=True, slots=True)
class SymbolReport:
    symbol: str
    cutoff: datetime  # the earlier of the import's clock and the symbol's last tick
    judged_through: date | None  # the last server day the mask has judged, after this run
    judged: bool  # False when this run judged no days (it imported no M15)


@dataclass(frozen=True, slots=True)
class ImportReport:
    cutoff: datetime  # the clock when the import started
    chunks: tuple[ChunkReport, ...]
    symbols: tuple[SymbolReport, ...]

    @property
    def bars(self) -> int:
        return sum(c.bars for c in self.chunks)

    @property
    def retries(self) -> int:
        return sum(c.reads - 2 for c in self.chunks)

    @property
    def flagged(self) -> tuple[ChunkReport, ...]:
        return tuple(c for c in self.chunks if c.flag)


def _now() -> datetime:
    return datetime.now(UTC)


def import_history(
    api: Mt5Api,
    root: Path,
    *,
    symbols: Sequence[str] = SYMBOLS,
    timeframes: Sequence[Timeframe] = TIMEFRAMES,
    start: datetime = RESEARCH_START,
    clock: Callable[[], datetime] = _now,
) -> ImportReport:
    """Import every chunk of each symbol and timeframe from `start` to now."""
    cutoff = _cutoff(clock)
    plan = {
        symbol: [c for tf in timeframes for c in _chunks(symbol, tf, start, cutoff)]
        for symbol in symbols
    }
    return _run(api, root, plan, timeframes, cutoff)


def refresh(
    api: Mt5Api,
    root: Path,
    *,
    symbols: Sequence[str] = SYMBOLS,
    timeframes: Sequence[Timeframe] = TIMEFRAMES,
    clock: Callable[[], datetime] = _now,
) -> ImportReport:
    """Re-import, per symbol, from the earlier of the chunk before the cutoff's and the chunk
    of its last import, through the cutoff's chunk."""
    cutoff = _cutoff(clock)
    plan = {}
    for symbol in symbols:
        try:
            last = read_meta(root, symbol).last_import
        except CacheError:
            last = None
        if last is None:
            raise ImportFailed(f"{symbol} has never been imported; run a full import first")
        chunks = []
        for timeframe in timeframes:
            current = _chunk_of(symbol, timeframe, cutoff)
            begin = min(_previous(current).start, _chunk_of(symbol, timeframe, last).start)
            chunks += _chunks(symbol, timeframe, begin, cutoff)
        plan[symbol] = chunks
    return _run(api, root, plan, timeframes, cutoff)


@contextmanager
def import_lock(root: Path) -> Iterator[None]:
    """Hold the cache's import lock; a second importer is refused while it is held."""
    path = Path(root) / "bars" / "import.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        holder = path.read_text(encoding="utf-8").strip() if path.exists() else "unknown"
        raise ImportLocked(
            f"another import holds {path} ({holder}). If none is running, a crashed import "
            "left it: delete the file and run the import again"
        ) from None
    try:
        os.write(fd, f"pid {os.getpid()} since {_now():%Y-%m-%d %H:%M:%S} UTC".encode())
        os.close(fd)
        yield
    finally:
        path.unlink(missing_ok=True)


# Internals -----------------------------------------------------------------------------------


def _run(
    api: Mt5Api,
    root: Path,
    plan: dict[str, list[Chunk]],
    timeframes: Sequence[Timeframe],
    cutoff: datetime,
) -> ImportReport:
    missing = [str(timeframe) for timeframe in TIMEFRAMES if timeframe not in timeframes]
    if Timeframe.M15 in timeframes and missing:
        raise ImportFailed(
            "a run that imports M15 judges days, and a judged day is served on every timeframe, "
            f"so it must import them all; this one is without {', '.join(missing)}. A refresh "
            "of some timeframes only must leave out M15"
        )
    chunks: list[ChunkReport] = []
    symbols: list[SymbolReport] = []
    with import_lock(root), _connected(api):
        source = Mt5BarSource(api, clock=lambda: cutoff)
        for symbol, symbol_chunks in plan.items():
            if Timeframe.M15 not in timeframes and not _has_mask(root, symbol):
                raise ImportFailed(
                    f"{symbol}'s first import needs M15: the data-quality mask is judged on it"
                )
            until = _symbol_cutoff(api, symbol, cutoff)
            judged = _judged_through(root, symbol)
            seen: set[Timeframe] = set()  # timeframes with bars in an earlier chunk
            last_m15: int | None = None  # the open time of the last closed M15 bar read
            for chunk in symbol_chunks:
                if chunk.start >= until:  # after the last tick: no bar there has closed
                    continue
                bars, reads = _settled(source, chunk, until)
                flag = _flag(chunk, bars, after_data=chunk.timeframe in seen, cutoff=until)
                if len(bars):
                    seen.add(chunk.timeframe)
                    if chunk.timeframe is Timeframe.M15:  # its chunks come in time order
                        last_m15 = int(bars.time[-1])
                if judged is not None:
                    _refuse_changes_to_judged_days(root, chunk, bars, judged)
                # An empty chunk is written only to clear bars the cache held there before.
                if len(bars) or _cached(root, chunk):
                    write_chunk(root, bars, chunk.year, chunk.month)
                chunks.append(ChunkReport(chunk, len(bars), reads, flag))
            symbols.append(_judge(root, symbol, until, timeframes, chunks, last_m15))
    return ImportReport(cutoff, tuple(chunks), tuple(symbols))


def _symbol_cutoff(api: Mt5Api, symbol: str, cutoff: datetime) -> datetime:
    """The earlier of the import's cutoff and the symbol's last tick, in UTC."""
    if not api.symbol_select(symbol, True):
        raise ImportFailed(f"symbol_select failed for {symbol}: {api.last_error()}")
    tick = api.symbol_info_tick(symbol)
    if tick is None:
        raise ImportFailed(f"{symbol} has no last tick to bound the import: {api.last_error()}")
    last = int(server_to_utc(np.array([int(tick.time)]))[0])
    return min(cutoff, datetime.fromtimestamp(last, UTC))


def _judge(
    root: Path,
    symbol: str,
    cutoff: datetime,
    timeframes: Sequence[Timeframe],
    chunks: list[ChunkReport],
    last_m15: int | None,
) -> SymbolReport:
    """Judge the server days closed by the cutoff and before the day of the last M15 bar, if
    this run makes that safe."""
    if not _has_meta(root, symbol):  # the terminal had no bars for it at all
        return SymbolReport(symbol, cutoff, None, judged=False)
    short = [c for c in chunks if c.chunk.symbol == symbol and c.flag]
    short_m15 = [c for c in short if c.chunk.timeframe is Timeframe.M15]
    if short_m15:
        raise ImportFailed(
            f"{short_m15[0].chunk} looks short ({short_m15[0].flag}); the data-quality mask is "
            f"judged on M15, so no day of {symbol} was judged. Check the terminal's history"
        )
    if Timeframe.M15 not in timeframes:
        return SymbolReport(symbol, cutoff, _judged_through(root, symbol), judged=False)
    _refuse_lagging_history(symbol, cutoff, last_m15)
    try:
        # The last M15 bar's own day may still be filling, so it is judged by the next import.
        meta = record_import(root, symbol, cutoff, judge_until=_utc(last_m15))
    except ValueError as error:  # no M15 in the Research Window to judge days by
        raise ImportFailed(str(error)) from error
    return SymbolReport(symbol, cutoff, meta.mask.judged_through, judged=True)


def _refuse_lagging_history(symbol: str, cutoff: datetime, last_m15: int | None) -> None:
    """Fail when the run's last closed M15 bar closed more than MAX_LAG before the cutoff.

    The last tick bounds the cutoff, but a terminal that has just reconnected has a live tick
    while its history may still stop days earlier, and two reads agree on that history. Judged
    by the tick, the missing days would be judged empty and served so; the full history would
    then change bars on judged days, and every later import would be refused.
    """
    if last_m15 is None:
        raise ImportFailed(
            f"this run read no closed M15 bar of {symbol} before its cutoff "
            f"({cutoff:%Y-%m-%d %H:%M} UTC). The terminal may still be downloading its history; "
            f"no day of {symbol} was judged"
        )
    close = _utc(last_m15 + Timeframe.M15.seconds)
    lag = cutoff - close
    if lag > MAX_LAG:
        raise ImportFailed(
            f"{symbol}'s M15 history ends {lag.total_seconds() / 3600:.1f} hours before its "
            f"cutoff ({cutoff:%Y-%m-%d %H:%M} UTC): its last closed M15 bar closed at "
            f"{close:%Y-%m-%d %H:%M} UTC. The terminal may still be downloading its history; "
            f"no day of {symbol} was judged. Import again once it has caught up"
        )


def _settled(source: Mt5BarSource, chunk: Chunk, cutoff: datetime) -> tuple[Bars, int]:
    """The chunk's bars closed by the cutoff, once two reads in a row agree on them."""
    end = max(chunk.start, min(chunk.end, cutoff))
    previous = _closed(source.fetch(chunk.symbol, chunk.timeframe, chunk.start, end), cutoff)
    for reads in range(2, MAX_READS + 1):
        bars = _closed(source.fetch(chunk.symbol, chunk.timeframe, chunk.start, end), cutoff)
        if _same(previous, bars):
            return bars, reads
        previous = bars
    raise ImportFailed(
        f"{chunk} ({chunk.start:%Y-%m-%d} to {end:%Y-%m-%d %H:%M} UTC) did not settle in "
        f"{MAX_READS} reads; the terminal may still be downloading its history"
    )


def _closed(bars: Bars, cutoff: datetime) -> Bars:
    """The bars that closed by the cutoff; the one still forming is dropped."""
    last_open = int(cutoff.timestamp()) - bars.timeframe.seconds
    return bars.between(int(np.iinfo(np.int64).min), last_open + 1)


def _same(a: Bars, b: Bars) -> bool:
    return all(np.array_equal(getattr(a, name), getattr(b, name)) for name in _ARRAYS)


def _refuse_changes_to_judged_days(root: Path, chunk: Chunk, bars: Bars, judged: date) -> None:
    """Fail, before writing, if the chunk changes any bar on a server day already judged."""
    old = read_unmasked(root, chunk.symbol, chunk.timeframe, chunk.start, chunk.end)
    last = (judged - _EPOCH).days
    old = _through_day(old, last)
    new = _through_day(bars, last)
    if _same(old, new):
        return
    n = min(len(old), len(new))
    differs = np.zeros(n, dtype=bool)
    for name in _ARRAYS:
        differs |= getattr(old, name)[:n] != getattr(new, name)[:n]
    first = int(np.argmax(differs)) if differs.any() else n
    time = old.time[first] if first < len(old) else new.time[first]
    day = _EPOCH + timedelta(days=int(server_days(np.array([time]))[0]))
    raise ImportFailed(
        f"{chunk} would change bars on {day}, a server day already judged and served "
        f"({len(old)} cached bars there, {len(new)} read now). A Trial's bars must never change; "
        "the terminal's history may be incomplete or revised. Nothing was written for this chunk"
    )


def _through_day(bars: Bars, last_day: int) -> Bars:
    keep = server_days(bars.time) <= last_day
    n = int(keep.sum())  # bars are sorted, so the judged ones come first
    return bars.between(int(np.iinfo(np.int64).min), int(bars.time[n - 1]) + 1 if n else -(2**62))


def _flag(chunk: Chunk, bars: Bars, *, after_data: bool, cutoff: datetime) -> str | None:
    """Why a chunk looks short, for the report.

    A terminal whose "Max bars in chart" caps a read returns only the latest bars of it, so a
    chunk after earlier data starts late or holds nothing; the bar source trims what MT5 returns,
    so the cap can't be seen as an exact bar count. A terminal that stalls while downloading can
    return the same short read twice, which two agreeing reads can't tell from the truth, so a
    finished chunk whose bars stop well before its end is flagged too.
    """
    if not len(bars):
        if after_data:
            return "no bars after earlier ones: a capped read or a gap in the history"
        return None
    late = datetime.fromtimestamp(int(bars.time[0]), UTC) - chunk.start
    if after_data and late > LATE_START:
        return f"first bar {late.days} days after the chunk starts: a capped read or a gap"
    last_close = datetime.fromtimestamp(int(bars.time[-1]) + bars.timeframe.seconds, UTC)
    early = chunk.end - last_close
    if chunk.end <= cutoff and early > LATE_START:
        return f"last bar {early.days} days before the chunk ends: a read cut short or a gap"
    return None


def _chunks(symbol: str, timeframe: Timeframe, start: datetime, cutoff: datetime) -> list[Chunk]:
    """The chunks from the one holding `start` through the one holding the cutoff."""
    chunks = [_chunk_of(symbol, timeframe, start)]
    while chunks[-1].end < cutoff:
        chunks.append(_chunk_of(symbol, timeframe, chunks[-1].end))
    return chunks


def _chunk_of(symbol: str, timeframe: Timeframe, when: datetime) -> Chunk:
    when = when.astimezone(UTC)
    month = when.month if timeframe is Timeframe.M1 else None
    return Chunk(symbol, timeframe, when.year, month)


def _previous(chunk: Chunk) -> Chunk:
    return _chunk_of(chunk.symbol, chunk.timeframe, chunk.start - timedelta(seconds=1))


def _has_meta(root: Path, symbol: str) -> bool:
    try:
        read_meta(root, symbol)
    except CacheError:
        return False
    return True


def _has_mask(root: Path, symbol: str) -> bool:
    return _has_meta(root, symbol) and read_meta(root, symbol).mask is not None


def _judged_through(root: Path, symbol: str) -> date | None:
    return read_meta(root, symbol).mask.judged_through if _has_mask(root, symbol) else None


def _cached(root: Path, chunk: Chunk) -> bool:
    """Whether the cache holds bars inside the chunk already."""
    if not _has_meta(root, chunk.symbol):
        return False
    return bool(len(read_unmasked(root, chunk.symbol, chunk.timeframe, chunk.start, chunk.end)))


def _cutoff(clock: Callable[[], datetime]) -> datetime:
    now = clock()
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("the clock must give a timezone-aware UTC datetime")
    return now.astimezone(UTC).replace(microsecond=0)


def _utc(epoch: int) -> datetime:
    return datetime.fromtimestamp(epoch, UTC)


@contextmanager
def _connected(api: Mt5Api) -> Iterator[None]:
    if not api.initialize():
        raise ImportFailed(f"MT5 initialize failed: {api.last_error()}")
    try:
        yield
    finally:
        api.shutdown()
