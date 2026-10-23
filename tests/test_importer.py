"""T4: the read-only importer against FakeMt5, including the ways a real terminal misbehaves."""

import dataclasses
import hashlib
import tracemalloc
from datetime import UTC, date, datetime, timedelta

import numpy as np
import pytest

import vibedesk.market.importer as importer
from vibedesk.ledger import Ledger
from vibedesk.market.bars import Timeframe
from vibedesk.market.cache import CachedBarSource, CacheError, read_meta, read_unmasked
from vibedesk.market.data import MarketData
from vibedesk.market.fake_mt5 import FakeMt5
from vibedesk.market.importer import (
    MAX_READS,
    ChunkReport,
    ImportFailed,
    ImportLocked,
    import_history,
    import_lock,
    refresh,
)
from vibedesk.market.server_time import server_offset
from vibedesk.market.synthetic import SyntheticBarSource

ALL = (Timeframe.M1, Timeframe.M15, Timeframe.H1, Timeframe.H4, Timeframe.D1)
START = datetime(2023, 11, 1, tzinfo=UTC)
SATURDAY = datetime(2024, 2, 24, 12, tzinfo=UTC)  # every bar of the week has closed
LAST_TICK = datetime(2024, 2, 23, 21, 59, 59, tzinfo=UTC)  # Friday's, as the terminal has it then


@pytest.fixture(scope="module")
def source():
    return SyntheticBarSource(6, date(2023, 11, 1), date(2024, 2, 26))


def at(when):
    return lambda: when


def run_import(source, root, *, clock=SATURDAY, timeframes=ALL, **fake):
    api = FakeMt5(source, **{"now": clock} | fake)  # the terminal lives at the clock's time
    return import_history(
        api, root, symbols=("GOLD",), timeframes=timeframes, start=START, clock=at(clock)
    )


def closed(bars, cutoff):
    return bars.time + bars.timeframe.seconds <= cutoff.timestamp()


def test_a_full_import_reads_back_through_market_data_as_the_source_after_the_mask(
    source, tmp_path
):
    report = run_import(source, tmp_path)

    mask = read_meta(tmp_path, "GOLD").mask
    with Ledger(":memory:") as ledger:
        market = MarketData(CachedBarSource(tmp_path), ledger)
        for timeframe in ALL:
            got = market.bars("GOLD", timeframe, START, SATURDAY)
            want = source.fetch("GOLD", timeframe, START, SATURDAY)
            keep = mask.keep(want.time) & closed(want, SATURDAY)
            for name in ("time", "open", "high", "low", "close", "spread"):
                assert np.array_equal(getattr(got, name), getattr(want, name)[keep]), timeframe
            assert (got.point, got.digits) == (0.01, 2)
    # Four M1 months, and 2023 and 2024 for each other timeframe; every chunk settled at once.
    assert len(report.chunks) == 4 + 2 * 4
    assert {c.reads for c in report.chunks} == {2}
    assert report.retries == 0 and report.flagged == ()
    assert report.bars == sum(len(read_unmasked(tmp_path, "GOLD", tf)) for tf in ALL)
    assert read_meta(tmp_path, "GOLD").last_import == LAST_TICK
    # Friday's last tick is the cutoff, and Friday holds the last M15 bar: it stays open until
    # an import brings Monday's bars.
    [gold] = report.symbols
    assert (gold.cutoff, gold.judged, gold.judged_through) == (LAST_TICK, True, date(2024, 2, 22))


def test_a_downloading_terminal_is_read_until_two_reads_agree(source, tmp_path):
    clean, slow = tmp_path / "clean", tmp_path / "slow"
    run_import(source, clean)

    report = run_import(source, slow, settling_reads=1)

    assert {c.reads for c in report.chunks if c.bars} == {3}  # short, full, full
    assert report.retries > 0
    for timeframe in ALL:
        assert np.array_equal(
            read_unmasked(slow, "GOLD", timeframe).close,
            read_unmasked(clean, "GOLD", timeframe).close,
        )


def test_a_chunk_that_never_settles_fails_by_name_and_serves_nothing(source, tmp_path):
    with pytest.raises(ImportFailed, match=r"GOLD M1 2023-11 \(2023-11-01 to .*did not settle"):
        run_import(source, tmp_path, settling_reads=MAX_READS + 1)

    with pytest.raises(CacheError):
        CachedBarSource(tmp_path).fetch("GOLD", Timeframe.M15)
    assert not (tmp_path / "bars" / "import.lock").exists()


def test_a_live_market_still_settles_because_only_closed_bars_are_compared(source, tmp_path):
    # The terminal's clock runs 15 minutes on at every call, so each read of the current year
    # has a bar the read before it lacked; the import's own cutoff stays at 10:07.
    cutoff = datetime(2024, 2, 21, 10, 7, tzinfo=UTC)
    ticks = [cutoff]

    def terminal_clock():
        ticks.append(ticks[-1] + timedelta(minutes=15))
        return ticks[-1]

    api = FakeMt5(source, now=terminal_clock)
    raw = []
    read = api.copy_rates_range
    api.copy_rates_range = lambda *a: raw.append((a[1], read(*a))) or raw[-1][1]

    report = import_history(
        api, tmp_path, symbols=("GOLD",), timeframes=ALL, start=START, clock=at(cutoff)
    )

    assert {c.reads for c in report.chunks} == {2}
    m15 = [rates for timeframe, rates in raw if timeframe == api.TIMEFRAME_M15]
    assert len(m15[-1]) > len(m15[-2])  # the two raw reads of 2024 really differed
    last = read_unmasked(tmp_path, "GOLD", Timeframe.M15).time[-1]
    assert last == datetime(2024, 2, 21, 9, 45, tzinfo=UTC).timestamp()  # 10:00 still formed


def test_the_forming_bar_is_cached_by_the_next_refresh_once_it_has_closed(source, tmp_path):
    first = datetime(2024, 2, 21, 10, 7, tzinfo=UTC)
    later = datetime(2024, 2, 21, 10, 20, tzinfo=UTC)
    run_import(source, tmp_path, clock=first, now=first)
    before = read_unmasked(tmp_path, "GOLD", Timeframe.M15).time[-1]

    refresh(
        FakeMt5(source, now=later), tmp_path, symbols=("GOLD",), timeframes=ALL, clock=at(later)
    )

    after = read_unmasked(tmp_path, "GOLD", Timeframe.M15).time[-1]
    assert before == datetime(2024, 2, 21, 9, 45, tzinfo=UTC).timestamp()
    assert after == datetime(2024, 2, 21, 10, 0, tzinfo=UTC).timestamp()
    m1 = read_unmasked(tmp_path, "GOLD", Timeframe.M1).time
    assert m1[-1] == datetime(2024, 2, 21, 10, 19, tzinfo=UTC).timestamp()


def test_a_capped_terminal_is_flagged(source, tmp_path):
    # 20,000 M1 bars cover about 14 trading days, so each month after the first starts late.
    report = run_import(source, tmp_path, max_bars=20000)

    flagged = {str(c.chunk) for c in report.flagged}
    assert flagged == {"GOLD M1 2023-12", "GOLD M1 2024-01", "GOLD M1 2024-02"}
    assert all("capped" in c.flag for c in report.flagged)


class StalledMt5(FakeMt5):
    """A terminal stuck half way through its download: every read returns the same first half."""

    def copy_rates_range(self, symbol, timeframe, date_from, date_to):
        rates = super().copy_rates_range(symbol, timeframe, date_from, date_to)
        return rates[: len(rates) // 2]


def test_a_stalled_terminals_short_m15_judges_no_day(source, tmp_path):
    # Two reads agree on the same short answer, so only the shape of the chunk can tell: its
    # bars stop about two weeks before the chunk ends. The mask is judged on M15, so a short M15
    # chunk stops the symbol before any day is judged, and nothing is served.
    with pytest.raises(ImportFailed, match=r"GOLD M15 2023 looks short \(last bar .* cut short"):
        import_history(
            StalledMt5(source), tmp_path, symbols=("GOLD",), start=START, clock=at(SATURDAY)
        )

    assert read_meta(tmp_path, "GOLD").mask is None
    with pytest.raises(CacheError):
        CachedBarSource(tmp_path).fetch("GOLD", Timeframe.M15)


def test_a_refresh_may_not_change_bars_on_days_already_judged(source, tmp_path):
    run_import(source, tmp_path)
    before = file_states(tmp_path, [2023, 2024])

    with pytest.raises(ImportFailed, match=r"GOLD M1 2024-01 would change bars on 2024-01-1"):
        refresh(StalledMt5(source), tmp_path, symbols=("GOLD",), timeframes=ALL,
                clock=at(SATURDAY))  # fmt: skip

    assert file_states(tmp_path, [2023, 2024]) == before


def test_a_stale_terminal_bounds_the_import_at_its_last_tick(source, tmp_path):
    # The terminal stopped receiving quotes on Tuesday at 17:00; the laptop's clock says
    # Wednesday noon. Judged by the clock, Tuesday would be a thin day, masked for good.
    last_tick = datetime(2024, 2, 20, 17, 0, tzinfo=UTC)
    report = import_history(
        FakeMt5(source, now=last_tick), tmp_path, symbols=("GOLD",), timeframes=ALL, start=START,
        clock=at(datetime(2024, 2, 21, 12, 0, tzinfo=UTC)),
    )  # fmt: skip

    [gold] = report.symbols
    mask = read_meta(tmp_path, "GOLD").mask
    assert gold.cutoff == last_tick and gold.judged
    assert mask.judged_through == date(2024, 2, 19)  # Tuesday is still open on the server
    assert date(2024, 2, 20) not in mask.masked_days
    assert read_meta(tmp_path, "GOLD").last_import == last_tick


class ReconnectedMt5(FakeMt5):
    """A terminal back online after a while: its last tick is live, at `tick`, but its history
    still stops at `now` until it has downloaded the rest."""

    def __init__(self, source, *, tick, **fake):
        super().__init__(source, **fake)
        self._tick = int(tick.timestamp())

    def symbol_info_tick(self, symbol):
        server = self._tick + int(server_offset(np.array([self._tick]))[0])
        tick = super().symbol_info_tick(symbol)
        return dataclasses.replace(tick, time=server, time_msc=server * 1000)


def test_history_lagging_a_live_tick_fails_and_judges_no_new_day(source, tmp_path):
    # The terminal reconnected on Wednesday with a live tick, but its history still stops on
    # the Friday before. Two reads agree on that and no chunk looks short, so judged by the
    # tick, Monday and Tuesday would be judged empty and served so; the next refresh, with the
    # full history, would then fail on them every night.
    wednesday = datetime(2024, 2, 21, 12, 0, tzinfo=UTC)
    run_import(source, tmp_path, clock=datetime(2024, 2, 15, 12, 0, tzinfo=UTC))
    before = read_meta(tmp_path, "GOLD")
    lagging = ReconnectedMt5(source, now=datetime(2024, 2, 17, tzinfo=UTC), tick=wednesday)

    with pytest.raises(
        ImportFailed,
        match=r"GOLD's M15 history ends 110\.0 hours before its cutoff .* still be downloading",
    ):
        refresh(lagging, tmp_path, symbols=("GOLD",), clock=at(wednesday))

    after = read_meta(tmp_path, "GOLD")
    assert after == before and after.mask.judged_through == date(2024, 2, 14)

    # Once the terminal has caught up, the refresh goes through and serves both days whole.
    report = refresh(
        FakeMt5(source, now=wednesday), tmp_path, symbols=("GOLD",), clock=at(wednesday)
    )

    assert report.symbols[0].judged_through == date(2024, 2, 20)
    monday, wednesday_open = (
        datetime(2024, 2, 18, 22, tzinfo=UTC),
        datetime(2024, 2, 20, 22, tzinfo=UTC),
    )
    for timeframe in ALL:
        served = CachedBarSource(tmp_path).fetch("GOLD", timeframe, monday, wednesday_open)
        assert np.array_equal(
            served.time, source.fetch("GOLD", timeframe, monday, wednesday_open).time
        )


class NoM15Mt5(FakeMt5):
    """A terminal that has every timeframe's history but M15's."""

    def copy_rates_range(self, symbol, timeframe, date_from, date_to):
        rates = super().copy_rates_range(symbol, timeframe, date_from, date_to)
        return rates[:0] if timeframe == self.TIMEFRAME_M15 else rates


def test_a_run_that_reads_no_m15_bar_judges_no_day(source, tmp_path):
    with pytest.raises(
        ImportFailed, match="read no closed M15 bar of GOLD .* still be downloading"
    ):
        import_history(
            NoM15Mt5(source, now=SATURDAY), tmp_path, symbols=("GOLD",), start=START,
            clock=at(SATURDAY),
        )  # fmt: skip

    assert read_meta(tmp_path, "GOLD").mask is None


def test_the_day_of_the_last_m15_bar_stays_open(source, tmp_path):
    # The history stops at 23:30 server time on Tuesday, and the last tick came at 00:30 on
    # Wednesday: an hour's lag, inside the limit. Judged by the tick, Tuesday would be served
    # for good without its last half hour, and the next refresh would fail on it.
    history_end = datetime(2024, 2, 20, 21, 29, tzinfo=UTC)  # the 23:29 M1 bar, server time
    tick = datetime(2024, 2, 20, 22, 30, tzinfo=UTC)
    report = import_history(
        ReconnectedMt5(source, now=history_end, tick=tick), tmp_path, symbols=("GOLD",),
        start=START, clock=at(tick),
    )  # fmt: skip

    assert report.symbols[0].judged_through == date(2024, 2, 19)

    later = datetime(2024, 2, 21, 12, 0, tzinfo=UTC)
    report = refresh(FakeMt5(source, now=later), tmp_path, symbols=("GOLD",), clock=at(later))

    assert report.symbols[0].judged_through == date(2024, 2, 20)


def test_chunks_after_the_last_tick_are_skipped_not_flagged(source, tmp_path):
    # The source ends on Monday 26 February; the clock is in March. The March M1 chunk lies
    # after the terminal's last tick, so it is not read, and nothing looks short.
    report = run_import(source, tmp_path, clock=datetime(2024, 3, 2, tzinfo=UTC))

    assert "GOLD M1 2024-03" not in {str(c.chunk) for c in report.chunks}
    assert report.flagged == ()
    assert report.symbols[0].judged


def test_a_run_without_m15_judges_no_new_day(source, tmp_path):
    wednesday = datetime(2024, 2, 21, 12, 0, tzinfo=UTC)
    run_import(source, tmp_path, clock=wednesday, now=wednesday)

    report = refresh(
        FakeMt5(source), tmp_path, symbols=("GOLD",), timeframes=(Timeframe.H1,),
        clock=at(SATURDAY),
    )  # fmt: skip

    [gold] = report.symbols
    assert not gold.judged
    assert gold.judged_through == read_meta(tmp_path, "GOLD").mask.judged_through
    assert gold.judged_through == date(2024, 2, 20)


def file_states(root, years):
    states = {}
    for timeframe in ALL:
        for year in years:
            path = root / "bars" / "GOLD" / timeframe.value / f"{year}.parquet"
            states[timeframe, year] = (
                path.stat().st_mtime_ns,
                hashlib.sha256(path.read_bytes()).hexdigest(),
            )
    return states


def spy_on_writes(monkeypatch):
    """Record the (timeframe, year, month) of every chunk the importer writes."""
    written = []
    write = importer.write_chunk

    def recording(root, bars, year, month=None):
        written.append((bars.timeframe, year, month))
        write(root, bars, year, month)

    monkeypatch.setattr(importer, "write_chunk", recording)
    return written


def test_a_refresh_after_new_year_rewrites_only_the_last_two_chunks(tmp_path, monkeypatch):
    source = SyntheticBarSource(7, date(2023, 11, 1), date(2025, 1, 10))
    old_cutoff = datetime(2024, 12, 31, 12, 0, tzinfo=UTC)  # a Tuesday
    new_cutoff = datetime(2025, 1, 2, 12, 0, tzinfo=UTC)
    run_import(source, tmp_path, clock=old_cutoff, now=old_cutoff)
    untouched = file_states(tmp_path, [2023])
    written = spy_on_writes(monkeypatch)

    refresh(
        FakeMt5(source, now=new_cutoff), tmp_path, symbols=("GOLD",), timeframes=ALL,
        clock=at(new_cutoff),
    )  # fmt: skip

    expected = {(Timeframe.M1, 2024, 12), (Timeframe.M1, 2025, 1)}
    expected |= {(tf, year, None) for tf in ALL[1:] for year in (2024, 2025)}
    # No D1 bar of 2025 has closed by the cutoff (the first opens 1 January 22:00 UTC and runs
    # to 2 January 22:00), so that empty chunk is not written.
    expected -= {(Timeframe.D1, 2025, None)}
    assert set(written) == expected
    assert file_states(tmp_path, [2023]) == untouched
    # The old year's last day is complete now: its M15 bars are the source's.
    day = datetime(2024, 12, 31, tzinfo=UTC), datetime(2025, 1, 1, tzinfo=UTC)
    got = read_unmasked(tmp_path, "GOLD", Timeframe.M15, *day).time
    assert np.array_equal(got, source.fetch("GOLD", Timeframe.M15, *day).time)
    assert got[-1] > old_cutoff.timestamp()
    assert read_meta(tmp_path, "GOLD").mask.judged_through == date(2025, 1, 1)


def test_a_refresh_covers_every_chunk_since_the_last_import(source, tmp_path, monkeypatch):
    run_import(source, tmp_path, clock=datetime(2023, 11, 20, tzinfo=UTC), timeframes=ALL)
    written = spy_on_writes(monkeypatch)

    refresh(
        FakeMt5(source, now=SATURDAY), tmp_path, symbols=("GOLD",), timeframes=ALL,
        clock=at(SATURDAY),
    )  # fmt: skip

    months = {(year, month) for tf, year, month in written if tf is Timeframe.M1}
    assert months == {(2023, 11), (2023, 12), (2024, 1), (2024, 2)}


def test_a_second_importer_is_refused_while_the_lock_is_held(source, tmp_path):
    with import_lock(tmp_path), pytest.raises(ImportLocked, match="another import holds"):
        run_import(source, tmp_path)

    run_import(source, tmp_path)  # the lock is gone once its holder finishes


def test_refusals_before_any_read(source, tmp_path):
    api = FakeMt5(source)
    with pytest.raises(ImportFailed, match="never been imported"):
        refresh(api, tmp_path, symbols=("GOLD",), clock=at(SATURDAY))
    with pytest.raises(ImportFailed, match="needs M15"):
        run_import(source, tmp_path, timeframes=(Timeframe.H1,))
    with pytest.raises(ImportFailed, match="initialize failed"):
        run_import(source, tmp_path, fail_initialize=True)
    with pytest.raises(ValueError, match="timezone-aware"):
        import_history(api, tmp_path, symbols=("GOLD",), clock=lambda: datetime(2024, 2, 24))
    assert not (tmp_path / "bars" / "import.lock").exists()


def test_a_run_that_judges_must_import_every_timeframe(source, tmp_path):
    # A judged day is served on every timeframe at once. Had M15 alone judged it, the M1, H1,
    # H4 and D1 bars imported later would change a judged day, and be refused for good.
    api = FakeMt5(source, now=SATURDAY)
    with pytest.raises(ImportFailed, match=r"without M1, H1, H4, D1\. .* must leave out M15"):
        import_history(
            api, tmp_path, symbols=("GOLD",), timeframes=(Timeframe.M15,), start=START,
            clock=at(SATURDAY),
        )  # fmt: skip
    assert api.calls == []  # refused before any read
    assert not (tmp_path / "bars").exists()

    run_import(source, tmp_path)
    with pytest.raises(ImportFailed, match=r"without H4\. "):
        refresh(
            api, tmp_path, symbols=("GOLD",), clock=at(SATURDAY),
            timeframes=(Timeframe.M1, Timeframe.M15, Timeframe.H1, Timeframe.D1),
        )  # fmt: skip
    assert api.calls == []


def test_the_report_holds_counts_never_prices():
    assert {f.name for f in dataclasses.fields(ChunkReport)} == {"chunk", "bars", "reads", "flag"}


@pytest.mark.slow
def test_twelve_years_of_m1_import_in_under_a_gigabyte(tmp_path):
    source = SyntheticBarSource(8, date(2014, 1, 1), date(2026, 1, 1))
    for timeframe in ALL:
        source.fetch("GOLD", timeframe)  # generate the data before measuring

    tracemalloc.start()
    report = import_history(
        FakeMt5(source), tmp_path, symbols=("GOLD",), start=datetime(2014, 1, 1, tzinfo=UTC),
        clock=at(datetime(2026, 1, 3, tzinfo=UTC)),
    )  # fmt: skip
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    print(f"12-year M1 import: {report.bars:,} bars, peak {peak / 2**20:.0f} MiB")
    assert len(read_unmasked(tmp_path, "GOLD", Timeframe.M1)) > 4_000_000
    assert peak < 2**30
