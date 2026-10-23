"""Spec §4 and tasks/plan.md ("Import and cache", "Data-quality mask", "Reports stop at the
Holdout"): the Parquet cache gives back exactly what was written, survives a failed rename,
serves only what the frozen mask keeps, and reports Holdout years by structural facts alone."""

import dataclasses
import json
import os
import re
from datetime import UTC, date, datetime, timedelta

import numpy as np
import pytest
from test_quality import CUTOFF, MASKED, PLANTED, SPARSE_UNTIL, as_dates, plant, take, weekdays

import vibedesk.market.cache as cache
from vibedesk.market.bars import Bars, Timeframe
from vibedesk.market.cache import (
    COLUMNS,
    GAP_HOURS,
    CachedBarSource,
    CacheError,
    Gap,
    YearSummary,
    read_meta,
    read_unmasked,
    record_import,
    summarize,
    write_chunk,
)
from vibedesk.market.grant import HOLDOUT_START, RESEARCH_START
from vibedesk.market.server_time import server_days
from vibedesk.market.synthetic import SyntheticBarSource

# Five weeks over New Year, from a Monday to a Saturday.
NEW_YEAR = SyntheticBarSource(seed=5, start=date(2024, 12, 16), end=date(2025, 1, 18))
NEW_YEAR_CUTOFF = datetime(2025, 1, 18, tzinfo=UTC)
HOLDOUT = int(HOLDOUT_START.timestamp())
END = 2**62  # an open end for Bars.between


def utc(epoch) -> datetime:
    return datetime.fromtimestamp(int(epoch), UTC)


def chunks(bars: Bars) -> list[tuple[int, int | None, Bars]]:
    """(year, month, bars) per chunk, as the importer writes them: M1 by month, others by year."""
    first, last = utc(bars.time[0]), utc(bars.time[-1])
    if bars.timeframe is Timeframe.M1:
        months = range(first.year * 12 + first.month - 1, last.year * 12 + last.month)
        bounds = [(m // 12, m % 12 + 1) for m in months]
        edges = [datetime(y, m, 1, tzinfo=UTC) for y, m in bounds]
        edges.append(datetime(last.year + last.month // 12, last.month % 12 + 1, 1, tzinfo=UTC))
    else:
        bounds = [(y, None) for y in range(first.year, last.year + 1)]
        edges = [datetime(y, 1, 1, tzinfo=UTC) for y in range(first.year, last.year + 2)]
    return [
        (year, month, bars.between(int(lo.timestamp()), int(hi.timestamp())))
        for (year, month), lo, hi in zip(bounds, edges, edges[1:], strict=False)
    ]


def store(root, source, symbol="GOLD", m15=None, change=None) -> dict[Timeframe, Bars]:
    """Write every timeframe of `source` into the cache, `m15` in place of its M15 if given,
    and each through `change` if given. Returns what was written."""
    written = {}
    for timeframe in Timeframe:
        bars = (
            m15
            if m15 is not None and timeframe is Timeframe.M15
            else source.fetch(symbol, timeframe)
        )
        bars = change(bars) if change else bars
        for year, month, chunk in chunks(bars):
            write_chunk(root, chunk, year, month)
        written[timeframe] = bars
    return written


def assert_same(actual: Bars, expected: Bars) -> None:
    assert (actual.symbol, actual.timeframe, actual.point, actual.digits) == (
        expected.symbol,
        expected.timeframe,
        expected.point,
        expected.digits,
    )
    for name in COLUMNS:
        a, e = getattr(actual, name), getattr(expected, name)
        assert a.dtype == e.dtype and np.array_equal(a, e), name


def joined(first: Bars, second: Bars) -> Bars:
    return dataclasses.replace(
        first, **{name: np.r_[getattr(first, name), getattr(second, name)] for name in COLUMNS}
    )


def year_file(root, timeframe, year, symbol="GOLD"):
    return root / "bars" / symbol / timeframe.value / f"{year}.parquet"


@pytest.fixture(scope="module")
def new_year_root(tmp_path_factory):
    root = tmp_path_factory.mktemp("new-year")
    store(root, NEW_YEAR)
    return root


# Write and read. -----------------------------------------------------------------------------


@pytest.mark.parametrize("timeframe", list(Timeframe))
def test_write_then_read_gives_back_the_same_bars(new_year_root, timeframe):
    expected = NEW_YEAR.fetch("GOLD", timeframe)

    assert_same(read_unmasked(new_year_root, "GOLD", timeframe), expected)
    assert year_file(new_year_root, timeframe, 2024).exists()
    assert year_file(new_year_root, timeframe, 2025).exists()
    for start, end in [
        (datetime(2024, 12, 30, tzinfo=UTC), datetime(2025, 1, 3, tzinfo=UTC)),
        (datetime(2024, 12, 31, 22, tzinfo=UTC), datetime(2025, 1, 1, 1, tzinfo=UTC)),
        (datetime(2025, 1, 6, tzinfo=UTC), datetime(2026, 1, 1, tzinfo=UTC)),
        (datetime(2020, 1, 1, tzinfo=UTC), datetime(2024, 12, 20, tzinfo=UTC)),
    ]:
        assert_same(
            read_unmasked(new_year_root, "GOLD", timeframe, start, end),
            NEW_YEAR.fetch("GOLD", timeframe, start, end),
        )


def test_an_empty_year_and_an_empty_month_round_trip(tmp_path):
    m15, m1 = NEW_YEAR.fetch("GOLD", Timeframe.M15), NEW_YEAR.fetch("GOLD", Timeframe.M1)
    for year, month, chunk in chunks(m15):
        write_chunk(tmp_path, chunk, year, month)
    write_chunk(tmp_path, m15.between(0, 0), 2023)
    write_chunk(tmp_path, m1.between(0, 0), 2023, 6)

    assert year_file(tmp_path, Timeframe.M15, 2023).exists()
    assert year_file(tmp_path, Timeframe.M1, 2023).exists()
    for timeframe in (Timeframe.M15, Timeframe.M1):
        empty = read_unmasked(
            tmp_path,
            "GOLD",
            timeframe,
            datetime(2023, 1, 1, tzinfo=UTC),
            datetime(2024, 1, 1, tzinfo=UTC),
        )
        assert_same(empty, m15.between(0, 0) if timeframe is Timeframe.M15 else m1.between(0, 0))
    assert_same(read_unmasked(tmp_path, "GOLD", Timeframe.M15), m15)


def test_a_month_chunk_replaces_only_its_month(tmp_path):
    m1 = NEW_YEAR.fetch("GOLD", Timeframe.M1)
    for year, month, chunk in chunks(m1):
        write_chunk(tmp_path, chunk, year, month)
    january = year_file(tmp_path, Timeframe.M1, 2025).read_bytes()
    mid_december = int(datetime(2024, 12, 20, tzinfo=UTC).timestamp())

    write_chunk(tmp_path, m1.between(0, mid_december), 2024, 12)

    assert_same(
        read_unmasked(tmp_path, "GOLD", Timeframe.M1),
        joined(m1.between(0, mid_december), m1.between(HOLDOUT, END)),
    )
    assert year_file(tmp_path, Timeframe.M1, 2025).read_bytes() == january

    write_chunk(tmp_path, m1.between(0, 0), 2024, 12)  # an empty month clears it

    assert_same(read_unmasked(tmp_path, "GOLD", Timeframe.M1), m1.between(HOLDOUT, END))


def test_a_year_chunk_replaces_the_whole_year(tmp_path):
    m15 = NEW_YEAR.fetch("GOLD", Timeframe.M15)
    write_chunk(tmp_path, m15.between(0, HOLDOUT), 2024)
    mid_december = int(datetime(2024, 12, 20, tzinfo=UTC).timestamp())

    write_chunk(tmp_path, m15.between(mid_december, HOLDOUT), 2024)

    assert_same(read_unmasked(tmp_path, "GOLD", Timeframe.M15), m15.between(mid_december, HOLDOUT))


def test_a_chunk_holds_only_its_own_bars(tmp_path):
    m15, m1 = NEW_YEAR.fetch("GOLD", Timeframe.M15), NEW_YEAR.fetch("GOLD", Timeframe.M1)

    with pytest.raises(ValueError, match="not all inside the chunk"):
        write_chunk(tmp_path, m15, 2024)  # it runs into 2025
    with pytest.raises(ValueError, match="not all inside the chunk"):
        write_chunk(tmp_path, m1.between(HOLDOUT, END), 2024, 12)


def test_point_and_digits_must_match_the_symbols(tmp_path):
    m15 = NEW_YEAR.fetch("GOLD", Timeframe.M15).between(0, HOLDOUT)
    write_chunk(tmp_path, m15, 2024)

    with pytest.raises(ValueError, match="point 0.01 and 2 digits"):
        write_chunk(tmp_path, dataclasses.replace(m15, point=0.001, digits=3), 2024)


def test_unknown_symbols_and_bad_names_are_refused(tmp_path):
    m15 = NEW_YEAR.fetch("GOLD", Timeframe.M15).between(0, HOLDOUT)

    with pytest.raises(CacheError, match="no cached bars for EURUSD"):
        read_unmasked(tmp_path, "EURUSD", Timeframe.M15)
    with pytest.raises(ValueError, match="not a symbol name"):
        write_chunk(tmp_path, dataclasses.replace(m15, symbol="../GOLD"), 2024)
    assert not any(tmp_path.iterdir())


def test_a_failed_rename_leaves_the_old_files_intact(tmp_path, monkeypatch):
    """A crash between the temporary write and the rename: the old year and meta.json stay."""
    m15 = NEW_YEAR.fetch("GOLD", Timeframe.M15)
    old = m15.between(0, HOLDOUT)
    write_chunk(tmp_path, old, 2024)
    path = year_file(tmp_path, Timeframe.M15, 2024)
    meta = tmp_path / "bars" / "GOLD" / "meta.json"
    before = path.read_bytes(), meta.read_bytes()

    def crash(src, dst):
        raise OSError("the power went out")

    monkeypatch.setattr(os, "replace", crash)
    mid_december = int(datetime(2024, 12, 20, tzinfo=UTC).timestamp())
    with pytest.raises(OSError, match="power"):
        write_chunk(tmp_path, m15.between(mid_december, HOLDOUT), 2024)
    with pytest.raises(OSError, match="power"):
        record_import(tmp_path, "GOLD", datetime(2025, 1, 1, tzinfo=UTC))
    monkeypatch.undo()

    assert (path.read_bytes(), meta.read_bytes()) == before
    assert_same(read_unmasked(tmp_path, "GOLD", Timeframe.M15), old)
    assert read_meta(tmp_path, "GOLD").mask is None
    assert [p.name for p in tmp_path.rglob("*.tmp")] == []


def held_open(times, replace=os.replace):
    """os.replace as Windows does it while a reader holds the target open, `times` times."""
    refused = []

    def replacing(src, dst):
        if len(refused) < times:
            refused.append(dst)
            raise PermissionError(13, "Access is denied", str(dst))
        replace(src, dst)

    return replacing, refused


def test_a_file_held_open_for_a_moment_is_replaced_once_let_go(tmp_path, monkeypatch):
    """On Windows a file a Trial is reading can't be replaced; a refresh racing it waits a
    moment instead of dying part way."""
    m15 = NEW_YEAR.fetch("GOLD", Timeframe.M15).between(0, HOLDOUT)
    write_chunk(tmp_path, m15, 2024)
    mid_december = int(datetime(2024, 12, 20, tzinfo=UTC).timestamp())
    replacing, refused = held_open(2)
    waits = []
    monkeypatch.setattr(os, "replace", replacing)
    monkeypatch.setattr(cache, "sleep", waits.append)

    write_chunk(tmp_path, m15.between(0, mid_december), 2024)

    assert refused == [year_file(tmp_path, Timeframe.M15, 2024)] * 2
    assert waits == [cache.REPLACE_WAIT] * 2
    assert_same(read_unmasked(tmp_path, "GOLD", Timeframe.M15), m15.between(0, mid_december))
    assert [p.name for p in tmp_path.rglob("*.tmp")] == []


def test_a_file_held_open_too_long_fails_by_name_and_stays_as_it_was(tmp_path, monkeypatch):
    m15 = NEW_YEAR.fetch("GOLD", Timeframe.M15).between(0, HOLDOUT)
    write_chunk(tmp_path, m15, 2024)
    path = year_file(tmp_path, Timeframe.M15, 2024)
    before = path.read_bytes()
    replacing, refused = held_open(cache.REPLACE_TRIES)
    monkeypatch.setattr(os, "replace", replacing)
    monkeypatch.setattr(cache, "sleep", lambda seconds: None)

    with pytest.raises(CacheError, match=re.escape(f"{path} is held open by another program")):
        write_chunk(tmp_path, m15.between(0, HOLDOUT - 86400), 2024)

    assert len(refused) == cache.REPLACE_TRIES
    assert path.read_bytes() == before
    assert [p.name for p in tmp_path.rglob("*.tmp")] == []


# The mask behind CachedBarSource. --------------------------------------------------------------


@pytest.fixture(scope="module")
def planted_root(tmp_path_factory):
    root = tmp_path_factory.mktemp("planted")
    store(root, PLANTED, m15=plant(PLANTED.fetch("GOLD", Timeframe.M15)))
    record_import(root, "GOLD", CUTOFF)
    return root


def unmasked_days(bars: Bars) -> np.ndarray:
    days = as_dates(server_days(bars.time))
    return np.array([day >= SPARSE_UNTIL and day not in MASKED for day in days], dtype=bool)


@pytest.mark.parametrize("timeframe", list(Timeframe))
def test_the_cached_source_serves_only_what_the_mask_keeps(planted_root, timeframe):
    full = PLANTED.fetch("GOLD", timeframe)
    if timeframe is Timeframe.M15:
        full = plant(full)
    expected = take(full, unmasked_days(full))
    source = CachedBarSource(planted_root)

    assert_same(source.fetch("GOLD", timeframe), expected)
    start, end = datetime(2024, 1, 20, tzinfo=UTC), datetime(2024, 3, 20, tzinfo=UTC)
    assert_same(
        source.fetch("GOLD", timeframe, start, end),
        expected.between(int(start.timestamp()), int(end.timestamp())),
    )


def test_meta_json_records_the_mask_and_the_last_import(planted_root):
    meta = json.loads((planted_root / "bars" / "GOLD" / "meta.json").read_text(encoding="utf-8"))

    assert meta["point"] == 0.01 and meta["digits"] == 2
    assert meta["mask"]["first_usable_month"] == "2024-02"
    assert meta["mask"]["masked_days"] == ["2024-03-13", "2024-04-17"]
    assert meta["mask"]["median_m15_bars_per_day"] == 96
    assert meta["last_import"] == "2024-05-04T00:00:00+00:00"


def test_the_cached_source_needs_a_recorded_import(tmp_path):
    store(tmp_path, NEW_YEAR)

    with pytest.raises(CacheError, match="no data-quality mask"):
        CachedBarSource(tmp_path).fetch("GOLD", Timeframe.M15)


def research_bytes(root) -> dict[Timeframe, bytes]:
    """Every cached Research Window bar, as Trials read it."""
    source = CachedBarSource(root)
    out = {}
    for timeframe in Timeframe:
        bars = source.fetch("GOLD", timeframe, RESEARCH_START, HOLDOUT_START)
        assert len(bars) > 0
        out[timeframe] = repr((bars.point, bars.digits)).encode() + b"".join(
            getattr(bars, name).tobytes() for name in COLUMNS
        )
    return out


def test_a_refresh_never_changes_the_research_bars(tmp_path):
    """A refresh re-imports the previous chunk and adds new days. The broker has since
    backfilled an old thin day, and the new days are mostly thinner than the old median, so a
    mask that re-judged old days, or took its median over all days, would serve different bars.
    """
    source = SyntheticBarSource(seed=8, start=date(2024, 12, 9), end=date(2025, 2, 15))
    first_cutoff, refresh_cutoff = (
        datetime(2025, 1, 4, tzinfo=UTC),
        datetime(2025, 2, 15, tzinfo=UTC),
    )
    old_thin, new_thin = date(2024, 12, 18), date(2025, 1, 22)
    m15 = source.fetch("GOLD", Timeframe.M15)
    later_days = {
        day: 60 for day in as_dates(np.unique(server_days(m15.time))) if day >= date(2025, 1, 6)
    }

    before_cutoff = lambda bars: bars.between(0, int(first_cutoff.timestamp()))  # noqa: E731
    store(tmp_path, source, m15=plant(m15, date(2014, 1, 1), {old_thin: 40}), change=before_cutoff)
    record_import(tmp_path, "GOLD", first_cutoff)
    first = research_bytes(tmp_path)
    first_mask = read_meta(tmp_path, "GOLD").mask
    # The first usable month's weekdays before the data starts have no bars: masked, losing none.
    before_the_data = tuple(weekdays(date(2024, 12, 2), date(2024, 12, 9)))
    assert first_mask.masked_days == (*before_the_data, old_thin)

    store(tmp_path, source, m15=plant(m15, date(2014, 1, 1), later_days | {new_thin: 10}))
    meta = record_import(tmp_path, "GOLD", refresh_cutoff)

    backfilled = read_unmasked(tmp_path, "GOLD", Timeframe.M15)
    assert as_dates(server_days(backfilled.time)).count(old_thin) == 96  # the backfill landed
    assert research_bytes(tmp_path) == first
    assert meta.mask.median == first_mask.median == 96
    assert meta.mask.masked_days == (*before_the_data, old_thin, new_thin)
    assert meta.mask.judged_through == date(2025, 2, 14)


# The data report's input. --------------------------------------------------------------------

HOLIDAYS = (date(2024, 12, 25), date(2025, 1, 1))
THIN_DAY = date(2025, 1, 8)


def without_holidays(bars: Bars) -> Bars:
    return take(
        bars, ~np.isin(server_days(bars.time), [(d - date(1970, 1, 1)).days for d in HOLIDAYS])
    )


def build_report_cache(root, change=None):
    def holidays_then(bars):
        bars = without_holidays(bars)
        return change(bars) if change else bars

    m15 = plant(NEW_YEAR.fetch("GOLD", Timeframe.M15), date(2014, 1, 1), {THIN_DAY: 30})
    written = store(root, NEW_YEAR, m15=m15, change=holidays_then)
    record_import(root, "GOLD", NEW_YEAR_CUTOFF)
    return written


def test_the_summary_counts_bars_masked_days_spreads_and_gaps(tmp_path):
    written = build_report_cache(tmp_path)

    summary = summarize(tmp_path, "GOLD")

    assert (summary.first_usable_month, summary.judged_through) == (
        date(2024, 12, 1),
        date(2025, 1, 17),
    )
    assert [(y.timeframe, y.year) for y in summary.years] == [
        (timeframe, year) for timeframe in Timeframe for year in (2024, 2025)
    ]
    for year in summary.years:
        bars = written[year.timeframe]
        bars = bars.between(0, HOLDOUT) if year.year == 2024 else bars.between(HOLDOUT, END)
        on_thin_day = np.array([day == THIN_DAY for day in as_dates(server_days(bars.time))])
        assert year.bars == len(bars)
        assert year.kept == len(bars) - on_thin_day.sum()
        assert year.masked_days == (() if year.year == 2024 else (THIN_DAY,))
        assert year.spread_median == np.median(bars.spread)
        assert year.spread_p90 == np.percentile(bars.spread, 90)
        assert year.spread_max == bars.spread.max()
        # Christmas in 2024; New Year's Day in 2025, though its gap starts at 22:00 UTC in 2024.
        # Weekends are no gaps. The thin day's 30 M15 bars end at 05:30 UTC, leaving one too.
        holiday = HOLIDAYS[year.year - 2024]
        closes = datetime(holiday.year, holiday.month, holiday.day, tzinfo=UTC) - timedelta(hours=2)
        expected = [Gap(closes, closes + timedelta(days=1))]
        if (year.timeframe, year.year) == (Timeframe.M15, 2025):
            thin_close = datetime(2025, 1, 8, 5, 30, tzinfo=UTC)
            expected.append(Gap(thin_close, datetime(2025, 1, 8, 22, tzinfo=UTC)))
        assert year.gaps == len(expected)
        assert year.longest_gaps == tuple(expected)
        assert year.longest_gaps[0].hours == 24 >= GAP_HOURS


def test_holdout_years_hold_only_structural_facts(tmp_path):
    """The summary has no price field, and Holdout prices can't move it: two caches that differ
    only in their Holdout prices give the same summary."""
    allowed = {
        "timeframe", "year", "bars", "kept", "masked_days", "spread_median", "spread_p90",
        "spread_max", "gaps", "longest_gaps",
    }  # fmt: skip
    assert {field.name for field in dataclasses.fields(YearSummary)} == allowed
    assert {field.name for field in dataclasses.fields(Gap)} == {"start", "end"}

    def reprice_the_holdout(bars: Bars) -> Bars:
        factor = np.where(bars.time >= HOLDOUT, 1.37, 1.0)
        return dataclasses.replace(
            bars,
            **{name: getattr(bars, name) * factor for name in ("open", "high", "low", "close")},
        )

    build_report_cache(tmp_path / "real")
    written = build_report_cache(tmp_path / "repriced", change=reprice_the_holdout)
    assert not np.array_equal(
        written[Timeframe.D1].close, without_holidays(NEW_YEAR.fetch("GOLD", Timeframe.D1)).close
    )

    real, repriced = summarize(tmp_path / "real", "GOLD"), summarize(tmp_path / "repriced", "GOLD")

    assert any(year.year >= HOLDOUT_START.year for year in real.years)
    assert real == repriced
