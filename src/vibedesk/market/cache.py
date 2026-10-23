"""The Parquet bar cache (spec §4), and the only module that touches Parquet.

Layout, under a data directory `root` (`data/` for the desk; tests pass a temporary one):
- `bars/{symbol}/{timeframe}/{year}.parquet`: one UTC calendar year of bars, by open time;
- `bars/{symbol}/meta.json`: point, digits, the data-quality mask and the last import time.

Every file is written under a temporary name and renamed into place, so a crash never leaves
half a year. The importer writes chunk by chunk with `write_chunk`, then calls `record_import`,
which judges the days that closed. Everything else reads through `CachedBarSource`, which applies
the mask, behind `MarketData` and its Holdout guard. `summarize` gives the data report its
structural facts per year, read from the times and spreads alone.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from vibedesk.market.bars import Bars, Timeframe
from vibedesk.market.quality import Mask, build_mask, extend_mask, server_days

SCHEMA = pa.schema(
    [
        ("time", pa.int64()),
        ("open", pa.float64()),
        ("high", pa.float64()),
        ("low", pa.float64()),
        ("close", pa.float64()),
        ("spread", pa.int32()),
    ]
)
COLUMNS = tuple(SCHEMA.names)

GAP_HOURS = 2  # a stretch this long with no bar inside a server week is a gap in the report
LONGEST_GAPS = 5  # how many of a year's gaps the summary lists

_SYMBOL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._#-]*")


class CacheError(Exception):
    """The cache has no such symbol, no mask yet, or a file in the wrong shape."""


@dataclass(frozen=True)
class Meta:
    symbol: str
    point: float
    digits: int
    mask: Mask | None = None  # None until the first import is recorded
    last_import: datetime | None = None  # that import's cutoff


def write_chunk(root: Path, bars: Bars, year: int, month: int | None = None) -> None:
    """Store `bars` as the cache's whole UTC calendar year, or month, of their timeframe.

    The chunk is replaced: bars the cache held in it before are dropped, even where `bars` has
    none, and every bar must open inside it. A month is merged into its year's file. The first
    chunk of a symbol records its point and digits; every later one must match them.
    """
    lo, hi = _chunk_range(year, month)
    if len(bars) and (bars.time[0] < lo or bars.time[-1] >= hi):
        raise ValueError(
            f"{bars.symbol} {bars.timeframe} bars from {_utc(bars.time[0]):%Y-%m-%d %H:%M} to "
            f"{_utc(bars.time[-1]):%Y-%m-%d %H:%M} UTC are not all inside the chunk "
            f"{_utc(lo):%Y-%m-%d} to {_utc(hi):%Y-%m-%d}"
        )
    try:
        meta = read_meta(root, bars.symbol)
    except CacheError:
        _write_meta(root, Meta(bars.symbol, bars.point, bars.digits))
    else:
        if (meta.point, meta.digits) != (bars.point, bars.digits):
            raise ValueError(
                f"{bars.symbol} is cached with point {meta.point} and {meta.digits} digits, "
                f"not point {bars.point} and {bars.digits} digits"
            )

    columns = {name: np.asarray(getattr(bars, name)) for name in COLUMNS}
    path = _year_path(root, bars.symbol, bars.timeframe, year)
    if month is not None and path.exists():
        old = _read_columns(path, COLUMNS)
        before, after = old["time"] < lo, old["time"] >= hi
        columns = {
            name: np.concatenate([old[name][before], columns[name], old[name][after]])
            for name in COLUMNS
        }
    table = pa.table(columns, schema=SCHEMA)
    _write_atomic(path, lambda tmp: pq.write_table(table, tmp))


def read_unmasked(
    root: Path,
    symbol: str,
    timeframe: Timeframe,
    start: datetime | None = None,
    end: datetime | None = None,
) -> Bars:
    """The cached bars whose UTC open time is in [start, end), without the mask.

    For the cache's own bookkeeping and its tests. A Trial reads through `CachedBarSource`, and
    the Holdout guard tests check that nothing outside `market/` calls this.
    """
    meta = read_meta(root, symbol)
    lo = int(start.timestamp()) if start else np.iinfo(np.int64).min
    hi = int(end.timestamp()) if end else np.iinfo(np.int64).max
    parts = [
        _read_columns(path, COLUMNS)
        for year, path in _year_files(root, symbol, timeframe)
        if _chunk_range(year)[0] < hi and _chunk_range(year)[1] > lo
    ]
    arrays = {
        name: np.concatenate([part[name] for part in parts]) if parts else np.empty(0)
        for name in COLUMNS
    }
    bars = Bars(symbol, timeframe, meta.point, meta.digits, **arrays)
    return bars.between(lo, hi)


def read_meta(root: Path, symbol: str) -> Meta:
    path = _symbol_dir(root, symbol) / "meta.json"
    if not path.exists():
        raise CacheError(f"no cached bars for {symbol} under {Path(root).resolve()}")
    data = json.loads(path.read_text(encoding="utf-8"))
    return Meta(
        symbol=data["symbol"],
        point=float(data["point"]),
        digits=int(data["digits"]),
        mask=Mask.from_json(data["mask"]) if data["mask"] else None,
        last_import=datetime.fromisoformat(data["last_import"]) if data["last_import"] else None,
    )


def record_import(root: Path, symbol: str, cutoff: datetime) -> Meta:
    """Judge the server days that closed by `cutoff`, and record `cutoff` as the last import.

    The first call builds the mask from the cached M15 bars and freezes it; a later call only
    judges the days after the last judged one (quality.py). The importer calls this once all of
    an import's chunks are written.
    """
    meta = read_meta(root, symbol)
    if meta.mask is None:
        mask = build_mask(read_unmasked(root, symbol, Timeframe.M15), cutoff)
    else:
        since = datetime.combine(meta.mask.judged_through, datetime.min.time(), UTC)
        mask = extend_mask(meta.mask, read_unmasked(root, symbol, Timeframe.M15, since), cutoff)
    meta = replace(meta, mask=mask, last_import=cutoff.astimezone(UTC))
    _write_meta(root, meta)
    return meta


class CachedBarSource:
    """A BarSource over the cache that serves only what the data-quality mask lets through.

    It reads the mask on every fetch, so Trials and Holdouts see the same bars and a refresh is
    seen at once. The cache holds Holdout years too, so only `MarketData` should hold one.
    """

    def __init__(self, root: Path) -> None:
        self._root = Path(root)

    def fetch(
        self,
        symbol: str,
        timeframe: Timeframe,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> Bars:
        meta = read_meta(self._root, symbol)
        if meta.mask is None:
            raise CacheError(f"{symbol} has no data-quality mask yet; record an import first")
        bars = read_unmasked(self._root, symbol, timeframe, start, end)
        keep = meta.mask.keep(bars.time)
        return replace(bars, **{name: getattr(bars, name)[keep] for name in COLUMNS})


# The data report's input: structural facts only. ---------------------------------------------


@dataclass(frozen=True)
class Gap:
    """A stretch with no bar inside a server week, in UTC."""

    start: datetime  # when the bar before it closed
    end: datetime  # when the bar after it opened

    @property
    def hours(self) -> float:
        return (self.end - self.start).total_seconds() / 3600


@dataclass(frozen=True)
class YearSummary:
    """One year file, by structural facts alone: no price, return or anything derived from them.

    It is the same for every year, so the data report built on it is no side door into the
    Holdout (ADR 0002). Only a file's `time` and `spread` columns are ever read for it.
    """

    timeframe: Timeframe
    year: int
    bars: int  # in the file
    kept: int | None  # left after the mask; None before the first import is recorded
    masked_days: tuple[date, ...]  # thin server days with bars in this file
    spread_median: float | None  # in points, over the file's bars; None for an empty year
    spread_p90: float | None
    spread_max: int | None
    gaps: int  # stretches of GAP_HOURS or more with no bar, inside a server week
    longest_gaps: tuple[Gap, ...]  # the LONGEST_GAPS longest, longest first


@dataclass(frozen=True)
class CacheSummary:
    symbol: str
    point: float
    digits: int
    first_usable_month: date | None
    judged_through: date | None
    last_import: datetime | None
    years: tuple[YearSummary, ...]  # by timeframe, then year


def summarize(root: Path, symbol: str) -> CacheSummary:
    """Bar counts, masked days, spreads and gaps per timeframe and year of a cached symbol.

    A gap belongs to the year of the bar that ends it, so one that spans New Year is counted.
    """
    meta = read_meta(root, symbol)
    mask = meta.mask
    masked = np.array(mask.masked_days if mask else [], dtype="datetime64[D]").astype(np.int64)
    years = []
    for timeframe in Timeframe:
        previous: int | None = None  # the open time of the last bar of the year before
        for year, path in _year_files(root, symbol, timeframe):
            columns = _read_columns(path, ("time", "spread"))
            time, spread = columns["time"], columns["spread"]
            days = server_days(time)
            gaps = _gaps(time if previous is None else np.r_[previous, time], timeframe)
            thin = np.unique(days[np.isin(days, masked)])
            years.append(
                YearSummary(
                    timeframe=timeframe,
                    year=year,
                    bars=len(time),
                    kept=int(mask.keep(time).sum()) if mask else None,
                    masked_days=tuple(thin.astype("datetime64[D]").tolist()),
                    spread_median=float(np.median(spread)) if len(spread) else None,
                    spread_p90=float(np.percentile(spread, 90)) if len(spread) else None,
                    spread_max=int(spread.max()) if len(spread) else None,
                    gaps=len(gaps),
                    longest_gaps=tuple(sorted(gaps, key=lambda gap: -gap.hours)[:LONGEST_GAPS]),
                )
            )
            if len(time):
                previous = int(time[-1])
    return CacheSummary(
        symbol=meta.symbol,
        point=meta.point,
        digits=meta.digits,
        first_usable_month=mask.first_usable_month if mask else None,
        judged_through=mask.judged_through if mask else None,
        last_import=meta.last_import,
        years=tuple(years),
    )


def _gaps(time: np.ndarray, timeframe: Timeframe) -> list[Gap]:
    closes = time[:-1] + timeframe.seconds
    weeks = (server_days(time) + 3) // 7  # Monday to Sunday on the server clock
    found = (time[1:] - closes >= GAP_HOURS * 3600) & (weeks[1:] == weeks[:-1])
    return [Gap(_utc(closes[i]), _utc(time[i + 1])) for i in np.flatnonzero(found)]


# Files. ------------------------------------------------------------------------------------


def _symbol_dir(root: Path, symbol: str) -> Path:
    if not _SYMBOL.fullmatch(symbol):
        raise ValueError(f"{symbol!r} is not a symbol name")
    return Path(root) / "bars" / symbol


def _year_path(root: Path, symbol: str, timeframe: Timeframe, year: int) -> Path:
    return _symbol_dir(root, symbol) / timeframe.value / f"{year}.parquet"


def _year_files(root: Path, symbol: str, timeframe: Timeframe) -> list[tuple[int, Path]]:
    folder = _symbol_dir(root, symbol) / timeframe.value
    files = folder.glob("*.parquet") if folder.exists() else []
    return sorted((int(path.stem), path) for path in files if path.stem.isdigit())


def _read_columns(path: Path, columns: tuple[str, ...]) -> dict[str, np.ndarray]:
    table = pq.read_table(path, columns=list(columns))
    expected = pa.schema([SCHEMA.field(name) for name in columns])
    if not table.schema.remove_metadata().equals(expected):
        raise CacheError(f"{path} holds {table.schema} instead of {expected}")
    return {name: table.column(name).to_numpy() for name in columns}


def _write_meta(root: Path, meta: Meta) -> None:
    data = {
        "symbol": meta.symbol,
        "point": meta.point,
        "digits": meta.digits,
        "mask": meta.mask.to_json() if meta.mask else None,
        "last_import": meta.last_import.isoformat() if meta.last_import else None,
    }
    text = json.dumps(data, indent=2, sort_keys=True) + "\n"
    path = _symbol_dir(root, meta.symbol) / "meta.json"
    _write_atomic(path, lambda tmp: tmp.write_text(text, encoding="utf-8"))


def _write_atomic(path: Path, write: Callable[[Path], None]) -> None:
    """Write through a temporary file in the same folder, then rename it over `path`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        write(tmp)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _chunk_range(year: int, month: int | None = None) -> tuple[int, int]:
    """[start, end) of a UTC calendar year, or of one month of it, as epoch seconds."""
    if month is None:
        start, end = datetime(year, 1, 1, tzinfo=UTC), datetime(year + 1, 1, 1, tzinfo=UTC)
    else:
        start = datetime(year, month, 1, tzinfo=UTC)
        end = datetime(year + month // 12, month % 12 + 1, 1, tzinfo=UTC)
    return int(start.timestamp()), int(end.timestamp())


def _utc(epoch: int) -> datetime:
    return datetime.fromtimestamp(int(epoch), UTC)
