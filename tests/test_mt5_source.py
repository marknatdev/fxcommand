"""Mt5BarSource: bars from the MT5 terminal, in UTC, exactly as the source behind it made them.

FakeMt5 serves SyntheticBarSource bars on the server clock, the way the real terminal does, so
every bar read back through Mt5BarSource must equal the synthetic bar it came from.
"""

import inspect
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import numpy as np
import pytest

from vibedesk.market.bars import Bars, Timeframe
from vibedesk.market.fake_mt5 import NO_IPC, FakeMt5
from vibedesk.market.grant import RESEARCH_START
from vibedesk.market.mt5 import Mt5BarSource, Mt5Error
from vibedesk.market.server_time import DAY, check_clock, server_offset
from vibedesk.market.source import BarSource
from vibedesk.market.synthetic import SyntheticBarSource

# Five months over the European switch back (26 October 2025), New Year and the switch to daylight
# saving (29 March 2026).
START, END = date(2025, 10, 13), date(2026, 4, 6)
META = ("symbol", "timeframe", "point", "digits")
ARRAYS = ("time", "open", "high", "low", "close", "spread")
HOUR = 3600


@pytest.fixture(scope="module")
def source():
    return SyntheticBarSource(5, START, END)


def utc(*args) -> datetime:
    return datetime(*args, tzinfo=UTC)


def connected(source, **kwargs) -> FakeMt5:
    api = FakeMt5(source, **kwargs)
    assert api.initialize()
    return api


def same(a: Bars, b: Bars) -> bool:
    """Equal bars: the same symbol, timeframe, point and digits, and the same arrays."""
    return all(getattr(a, name) == getattr(b, name) for name in META) and all(
        np.array_equal(getattr(a, name), getattr(b, name)) for name in ARRAYS
    )


class WithHoliday:
    """The synthetic source with one server day gone from every timeframe: a market holiday."""

    def __init__(self, source: SyntheticBarSource, holiday: date) -> None:
        self._source = source
        self._day = (holiday - date(1970, 1, 1)).days

    def fetch(self, symbol, timeframe, start=None, end=None) -> Bars:
        bars = self._source.fetch(symbol, timeframe, start, end)
        keep = (bars.time + server_offset(bars.time)) // DAY != self._day
        return replace(bars, **{name: getattr(bars, name)[keep] for name in ARRAYS})


def test_it_is_a_bar_source():
    fetch = inspect.signature(Mt5BarSource.fetch).parameters
    assert list(fetch) == list(inspect.signature(BarSource.fetch).parameters)


@pytest.mark.parametrize("symbol", ["GOLD", "EURUSD"])
@pytest.mark.parametrize("timeframe", list(Timeframe))
def test_bars_read_through_mt5_equal_the_synthetic_bars(source, symbol, timeframe):
    """Every bar of every timeframe, across both US switches: the server clock runs UTC+3, then
    UTC+2, then UTC+3 again, and every bar comes back to its UTC open time. The range starts a
    day early, as the first D1 and H4 bars open on Sunday evening, before the source's start."""
    mt5 = Mt5BarSource(connected(source))

    bars = mt5.fetch(symbol, timeframe, utc(2025, 10, 12), utc(2026, 4, 6))

    expected = source.fetch(symbol, timeframe)
    assert len(bars) > 0
    assert same(bars, expected)
    assert set(server_offset(bars.time).tolist()) == {2 * HOUR, 3 * HOUR}


def test_converted_bars_pass_the_clock_checks(source):
    """D1 bars open at 17:00 New York and H4 bars at server hours divisible by 4, after the
    conversion, in both years."""
    mt5 = Mt5BarSource(connected(source))
    times = {
        tf: mt5.fetch("GOLD", tf, utc(2025, 10, 13), utc(2026, 3, 23)).time
        for tf in (Timeframe.D1, Timeframe.H4, Timeframe.M15)
    }

    report = check_clock(times)

    assert report == {year: dict.fromkeys(times, 0) for year in (2025, 2026)}


def test_a_read_selects_the_symbol_then_asks_for_a_wider_range_on_the_server_clock(source):
    api = connected(source)
    mt5 = Mt5BarSource(api)

    mt5.fetch("GOLD", Timeframe.M15, utc(2026, 3, 23), utc(2026, 3, 31))
    mt5.fetch("GOLD", Timeframe.H1, utc(2026, 3, 23), utc(2026, 3, 31))

    # A day wider each side, as server wall time written as UTC: UTC+2 on 22 March, UTC+3 on
    # 1 April, after the switch.
    assert api.calls[1:] == [
        ("symbol_select", "GOLD", True),
        ("symbol_info", "GOLD"),
        ("copy_rates_range", "GOLD", api.TIMEFRAME_M15, utc(2026, 3, 22, 2), utc(2026, 4, 1, 3)),
        ("copy_rates_range", "GOLD", api.TIMEFRAME_H1, utc(2026, 3, 22, 2), utc(2026, 4, 1, 3)),
    ]
    assert api.selected == {"GOLD"}


# The ends of a range


def test_a_bar_opening_on_start_is_in_and_one_opening_on_end_is_out(source):
    mt5 = Mt5BarSource(connected(source))

    bars = mt5.fetch("GOLD", Timeframe.H1, utc(2026, 3, 4, 10), utc(2026, 3, 4, 12))

    assert [datetime.fromtimestamp(t, UTC) for t in bars.time] == [
        utc(2026, 3, 4, 10),
        utc(2026, 3, 4, 11),
    ]


def test_a_week_of_d1_bars_from_monday_midnight_utc(source):
    """Monday's D1 bar opened at 22:00 UTC on Sunday, before the range: four bars, not five."""
    mt5 = Mt5BarSource(connected(source))

    bars = mt5.fetch("GOLD", Timeframe.D1, utc(2026, 3, 2), utc(2026, 3, 7))

    assert [datetime.fromtimestamp(t, UTC) for t in bars.time] == [
        utc(2026, 3, 2, 22),
        utc(2026, 3, 3, 22),
        utc(2026, 3, 4, 22),
        utc(2026, 3, 5, 22),
    ]


@pytest.mark.parametrize(
    "start, end, first, last",
    [
        # Ends on Saturday: Friday's last M15 bar opens at 21:45 UTC in winter.
        (utc(2026, 3, 6, 21), utc(2026, 3, 7, 12), (3, 6, 21, 0), (3, 6, 21, 45)),
        # Ends on Sunday, as the week opens at 22:00 UTC (server Monday 00:00 in winter).
        (utc(2026, 3, 6, 21), utc(2026, 3, 8, 22), (3, 6, 21, 0), (3, 6, 21, 45)),
        (utc(2026, 3, 7), utc(2026, 3, 8, 22, 15), (3, 8, 22, 0), (3, 8, 22, 0)),
        # Between switches: Friday closes at 17:00 EDT (21:00 UTC, 23:00 server), last bar 20:45.
        (utc(2025, 10, 31, 20), utc(2025, 11, 2, 22), (10, 31, 20, 0), (10, 31, 20, 45)),
        (utc(2025, 11, 1), utc(2025, 11, 2, 22, 15), (11, 2, 22, 0), (11, 2, 22, 0)),
    ],
)
def test_ranges_that_end_inside_a_weekend(source, start, end, first, last):
    """`first` and `last` are the first and last bars' opens (month, day, hour, minute)."""
    bars = Mt5BarSource(connected(source)).fetch("GOLD", Timeframe.M15, start, end)

    opens = [datetime.fromtimestamp(t, UTC) for t in (bars.time[0], bars.time[-1])]
    assert same(bars, source.fetch("GOLD", Timeframe.M15, start, end))
    assert opens == [utc(start.year, *first), utc(start.year, *last)]


def test_ranges_that_end_inside_a_holiday(source):
    """Christmas 2025 (server Thursday 25 December) has no bars at all."""
    holiday = WithHoliday(source, date(2025, 12, 25))
    mt5 = Mt5BarSource(connected(holiday))

    m15 = mt5.fetch("GOLD", Timeframe.M15, utc(2025, 12, 24), utc(2025, 12, 25, 12))
    d1 = mt5.fetch("GOLD", Timeframe.D1, utc(2025, 12, 22), utc(2025, 12, 27))

    expected = holiday.fetch("GOLD", Timeframe.M15, utc(2025, 12, 24), utc(2025, 12, 25, 12))
    assert same(m15, expected)
    assert m15.time[-1] == utc(2025, 12, 24, 21, 45).timestamp()  # server 23:45 on the 24th
    # D1 bars for server 23, 24 and 26 December; the 25th's would open at 22:00 UTC on the 24th.
    assert [datetime.fromtimestamp(t, UTC) for t in d1.time] == [
        utc(2025, 12, 22, 22),
        utc(2025, 12, 23, 22),
        utc(2025, 12, 25, 22),
    ]


def test_random_ranges_match_the_source_at_every_timeframe(source):
    """Bounds on bar opens, a second either side of them, and anywhere: weekends included."""
    rng = np.random.default_rng(7)
    lo, hi = int(utc(2025, 10, 13).timestamp()), int(utc(2026, 3, 23).timestamp())
    mt5 = Mt5BarSource(connected(source))
    for _ in range(40):
        a = int(rng.integers(lo, hi - 10 * DAY)) // 900 * 900 + int(rng.choice([0, -1, 1, 437]))
        b = a + int(rng.integers(900, 10 * DAY)) // 900 * 900 + int(rng.choice([0, -1, 1, 211]))
        start, end = datetime.fromtimestamp(a, UTC), datetime.fromtimestamp(b, UTC)
        for timeframe in Timeframe:
            got = mt5.fetch("EURUSD", timeframe, start, end)
            assert same(got, source.fetch("EURUSD", timeframe, start, end)), (start, end)


# Missing bounds


def test_missing_bounds_are_the_research_start_and_the_clock(source):
    now = utc(2026, 3, 4, 10, 7)
    api = connected(source, now=now)
    mt5 = Mt5BarSource(api, clock=lambda: now)

    bars = mt5.fetch("GOLD", Timeframe.M15)

    assert same(bars, source.fetch("GOLD", Timeframe.M15, end=now))
    assert bars.time[-1] == utc(2026, 3, 4, 10).timestamp()  # still forming: closing is T4's job
    _, _, _, date_from, date_to = api.calls[-1]
    assert date_from == RESEARCH_START - timedelta(days=1) + timedelta(hours=2)
    assert date_to == now + timedelta(days=1) + timedelta(hours=2)


def test_the_default_clock_is_now(source):
    bars = Mt5BarSource(connected(source)).fetch("GOLD", Timeframe.D1, utc(2026, 3, 2))

    assert same(bars, source.fetch("GOLD", Timeframe.D1, utc(2026, 3, 2)))


# Failures


def test_a_terminal_that_is_not_connected_fails_with_its_error(source):
    with pytest.raises(Mt5Error, match=r"symbol_select failed for GOLD: \(-10004, 'No IPC"):
        Mt5BarSource(FakeMt5(source)).fetch("GOLD", Timeframe.H1, utc(2026, 3, 2), utc(2026, 3, 3))


def test_an_unknown_symbol_fails_with_its_error(source):
    with pytest.raises(Mt5Error, match="symbol_select failed for DOGEUSD: .*Invalid params"):
        Mt5BarSource(connected(source)).fetch(
            "DOGEUSD", Timeframe.H1, utc(2026, 3, 2), utc(2026, 3, 3)
        )


def test_a_failed_read_names_the_range_and_the_error(source):
    api = connected(source)
    mt5 = Mt5BarSource(api)
    mt5.fetch("GOLD", Timeframe.H1, utc(2026, 3, 2), utc(2026, 3, 3))
    api.shutdown()

    with pytest.raises(Mt5Error) as failure:
        mt5.fetch("GOLD", Timeframe.H1, utc(2026, 3, 2), utc(2026, 3, 3))

    assert str(failure.value) == (
        f"copy_rates_range failed for GOLD H1 from 2026-03-02 00:00 to 2026-03-03 00:00 UTC: "
        f"{NO_IPC}"
    )


def test_missing_symbol_info_fails_with_its_error(source):
    class NoInfo(FakeMt5):
        def symbol_info(self, symbol):
            return None

    api = NoInfo(source)
    api.initialize()

    with pytest.raises(Mt5Error, match="symbol_info failed for GOLD"):
        Mt5BarSource(api).fetch("GOLD", Timeframe.H1, utc(2026, 3, 2), utc(2026, 3, 3))


@pytest.mark.parametrize(
    "start, end, message",
    [
        (datetime(2026, 3, 2), utc(2026, 3, 3), "start must be a timezone-aware"),
        (utc(2026, 3, 2), datetime(2026, 3, 3), "end must be a timezone-aware"),
        (utc(2026, 3, 3), utc(2026, 3, 2), "start must not be after end"),
    ],
)
def test_bad_bounds_are_refused_before_any_read(source, start, end, message):
    api = connected(source)

    with pytest.raises(ValueError, match=message):
        Mt5BarSource(api).fetch("GOLD", Timeframe.H1, start, end)

    assert api.calls == [("initialize",)]
