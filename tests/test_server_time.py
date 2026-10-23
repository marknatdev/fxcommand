"""The XM server clock (New York + 7 h) and its inverse, as tasks/plan.md defines them."""

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pytest

from vibedesk.market import server_time
from vibedesk.market.bars import Timeframe
from vibedesk.market.server_time import (
    DAY,
    check_clock,
    server_days,
    server_hours,
    server_offset,
    server_to_utc,
    server_weekdays,
)
from vibedesk.market.synthetic import SyntheticBarSource

HOUR = 3600
NEW_YORK = ZoneInfo("America/New_York")

# Every US daylight-saving switch of 2014-2026: the second Sunday of March, the first of November.
SPRING = [date(2014, 3, 9), date(2015, 3, 8), date(2016, 3, 13), date(2017, 3, 12),
          date(2018, 3, 11), date(2019, 3, 10), date(2020, 3, 8), date(2021, 3, 14),
          date(2022, 3, 13), date(2023, 3, 12), date(2024, 3, 10), date(2025, 3, 9),
          date(2026, 3, 8)]  # fmt: skip
AUTUMN = [date(2014, 11, 2), date(2015, 11, 1), date(2016, 11, 6), date(2017, 11, 5),
          date(2018, 11, 4), date(2019, 11, 3), date(2020, 11, 1), date(2021, 11, 7),
          date(2022, 11, 6), date(2023, 11, 5), date(2024, 11, 3), date(2025, 11, 2),
          date(2026, 11, 1)]  # fmt: skip
SWITCHES = sorted(SPRING + AUTUMN)
# Over the first seven UTC hours of a switch Sunday, server_offset already uses the new offset
# (it looks up 12:00 UTC); the real clock switches at 06:00 or 07:00 UTC. Server Sunday, closed.
EARLY_SUNDAY = 7 * HOUR


def epoch(*args) -> int:
    return int(datetime(*args, tzinfo=UTC).timestamp())


def midnight(day: date) -> int:
    return epoch(day.year, day.month, day.day)


def server_weekday(utc: np.ndarray) -> np.ndarray:
    """0 = Monday ... 6 = Sunday on the server clock; 1970-01-01 was a Thursday."""
    return ((utc + server_offset(utc)) // DAY + 3) % 7


def plan_formula(server: int) -> int:
    """tasks/plan.md, one value at a time: utc = server - offset, the offset being New York's
    UTC offset + 7 h at the New York wall time server - 7 h."""
    new_york = datetime.fromtimestamp(server, UTC).replace(tzinfo=None) - timedelta(hours=7)
    offset = new_york.replace(tzinfo=NEW_YORK).utcoffset() + timedelta(hours=7)
    return server - int(offset.total_seconds())


def real_server(utc: int) -> int:
    """The real XM clock at a UTC instant: New York wall time + 7 h."""
    offset = datetime.fromtimestamp(utc, UTC).astimezone(NEW_YORK).utcoffset()
    return utc + int(offset.total_seconds()) + 7 * HOUR


@pytest.mark.parametrize(
    "utc, hours",
    [
        (epoch(2026, 10, 21, 12), 3),  # matches the 2026-10-21 probe: tick time ran UTC+3
        (epoch(2026, 1, 15, 12), 2),
        (epoch(2026, 3, 6, 20), 2),  # Friday before the US switch on 2026-03-08
        (epoch(2026, 3, 8, 21), 3),  # Sunday 21:00 UTC is server Monday 00:00 after the switch
        (epoch(2026, 3, 9, 1), 3),
        (epoch(2026, 10, 30, 20), 3),  # Friday before the US switch back on 2026-11-01
        (epoch(2026, 11, 1, 22), 2),  # Sunday 22:00 UTC is server Monday 00:00 in winter
    ],
)
def test_server_clock_is_new_york_plus_seven(utc, hours):
    assert server_offset(np.array([utc]))[0] == hours * HOUR


def test_server_offset_handles_empty_input():
    assert server_offset(np.array([], dtype=np.int64)).shape == (0,)


# The server calendar

# Each helper with the expression it replaced, as metrics, walk-forward, the quality mask, the
# day_of_week filter, swap nights and the Coin-Flip Baseline wrote it out. They must agree to
# the bit.
HELPERS = {
    "days": (server_days, lambda utc: (utc + server_offset(utc)) // DAY),
    "weekdays": (server_weekdays, server_weekday),
    "hours": (server_hours, lambda utc: ((utc + server_offset(utc)) // HOUR) % 24),
}


def switch_weekend(switch: date) -> np.ndarray:
    """Sorted UTC times from the Friday before a switch to the Monday after: every minute, and
    random seconds between them, a hundred of them twice."""
    friday = midnight(switch) - 2 * DAY
    minutes = np.arange(friday, friday + 4 * DAY, 60, dtype=np.int64)
    seconds = np.random.default_rng(switch.toordinal()).integers(friday, friday + 4 * DAY, 2000)
    return np.sort(np.concatenate([minutes, seconds, seconds[:100]]))


@pytest.mark.parametrize("switch", SWITCHES, ids=str)
@pytest.mark.parametrize("name", HELPERS)
def test_each_helper_is_the_expression_it_replaced(name, switch):
    """On sorted times, as bars come, and on the same times shuffled: same values, same dtype."""
    helper, written_out = HELPERS[name]
    utc = switch_weekend(switch)
    for times in (utc, np.random.default_rng(0).permutation(utc)):
        got, expected = helper(times), written_out(times)
        assert got.dtype == expected.dtype
        assert np.array_equal(got, expected)


@pytest.mark.parametrize("name", HELPERS)
def test_the_sorted_fast_path_agrees_with_the_general_one(name):
    """Every switch weekend of 2014-2026 in one sorted array; reversed, it takes the general
    path, one offset lookup per time."""
    helper, _ = HELPERS[name]
    utc = np.concatenate([switch_weekend(switch) for switch in SWITCHES])
    assert np.array_equal(helper(utc[::-1])[::-1], helper(utc))


def test_sorted_times_look_up_one_offset_per_utc_day(monkeypatch):
    """The fast path really runs: Friday to Monday takes four lookups, unsorted one per time."""
    lookups = []

    def counted(utc):
        lookups.append(len(utc))
        return server_offset(utc)

    monkeypatch.setattr(server_time, "server_offset", counted)
    utc = switch_weekend(date(2026, 3, 8))
    server_days(utc)
    server_days(utc[::-1])
    assert lookups == [4, len(utc)]


@pytest.mark.parametrize("name", HELPERS)
def test_each_helper_keeps_the_shape(name):
    """Like server_offset: no times, one time, a single number and a sorted grid."""
    helper, written_out = HELPERS[name]
    friday, sunday_open = epoch(2026, 3, 6, 20), epoch(2026, 3, 8, 21)
    for utc in (
        np.array([], dtype=np.int64),
        np.array([sunday_open]),
        sunday_open,
        np.array([[friday, friday + HOUR], [sunday_open, sunday_open + HOUR]]),
    ):
        got, expected = helper(utc), written_out(utc)
        assert np.shape(got) == np.shape(expected)
        assert np.array_equal(got, expected)


# Server clock to UTC


def test_the_named_switches_are_new_yorks():
    """The switch list the tests below name, checked against the time-zone database."""
    first, last = date(2014, 1, 1), date(2026, 12, 31)
    days = [first + timedelta(days=i) for i in range((last - first).days + 1)]
    noon = [datetime(d.year, d.month, d.day, 12, tzinfo=NEW_YORK).utcoffset() for d in days]
    pairs = zip(days[1:], noon[:-1], noon[1:], strict=True)
    switches = [day for day, before, after in pairs if before != after]
    assert switches == SWITCHES


@pytest.mark.parametrize(
    "server, utc",
    [
        (epoch(2026, 1, 15, 0), epoch(2026, 1, 14, 22)),  # winter: server midnight is 22:00 UTC
        (epoch(2026, 7, 15, 0), epoch(2026, 7, 14, 21)),  # summer: 21:00 UTC
        (epoch(2026, 3, 9, 0), epoch(2026, 3, 8, 21)),  # the week opens after the spring switch
        (epoch(2026, 3, 7, 0), epoch(2026, 3, 6, 22)),  # the week closes before it
        (epoch(2026, 11, 2, 0), epoch(2026, 11, 1, 22)),  # the week opens after the autumn switch
        (epoch(2026, 10, 31, 0), epoch(2026, 10, 30, 21)),  # the week closes before it
    ],
)
def test_server_to_utc_at_the_week_edges(server, utc):
    assert server_to_utc(np.array([server]))[0] == utc


def test_server_to_utc_keeps_the_shape():
    assert server_to_utc(np.array([], dtype=np.int64)).shape == (0,)
    grid = np.array([[epoch(2026, 1, 15)], [epoch(2026, 7, 15)]])
    assert np.array_equal(server_to_utc(grid), grid - np.array([[2 * HOUR], [3 * HOUR]]))


def test_utc_to_server_to_utc_is_the_identity_on_every_minute_2014_to_2026():
    """Every minute, a year at a time to keep memory small. The only minutes that do not come
    back are the first seven UTC hours of each switch Sunday, all on server Sunday, when no bar
    opens: there server_offset is knowingly approximate (see EARLY_SUNDAY)."""
    checked, wrong = 0, []
    for year in range(2014, 2027):
        utc = np.arange(epoch(year, 1, 1), epoch(year + 1, 1, 1), 60, dtype=np.int64)
        back = server_to_utc(utc + server_offset(utc))
        wrong.append(utc[back != utc])
        checked += len(utc)
    wrong = np.concatenate(wrong)

    expected = np.concatenate(
        [np.arange(midnight(day), midnight(day) + EARLY_SUNDAY, 60) for day in SWITCHES]
    )
    assert checked == (date(2027, 1, 1) - date(2014, 1, 1)).days * 1440
    assert len(wrong) == 26 * 420
    assert np.array_equal(wrong, expected)
    assert set(server_weekday(wrong).tolist()) == {6}


@pytest.mark.parametrize("switch", SWITCHES, ids=str)
def test_every_trading_minute_round_trips_over_the_switch_weekend(switch):
    """Friday to Monday around the switch: Friday, Sunday from the open and Monday come back
    exactly; the early Sunday hours are the only miss, and are closed."""
    friday = midnight(switch) - 2 * DAY
    utc = np.arange(friday, friday + 4 * DAY, 60, dtype=np.int64)

    back = server_to_utc(utc + server_offset(utc))

    sunday = midnight(switch)
    assert np.array_equal(utc[back != utc], np.arange(sunday, sunday + EARLY_SUNDAY, 60))
    open_ = sunday + (21 if switch in SPRING else 22) * HOUR  # 17:00 New York, after the switch
    assert server_offset(np.array([open_]))[0] + open_ == midnight(switch) + DAY  # server Monday
    trading = (utc < friday + DAY) | (utc >= open_)
    assert np.array_equal(back[trading], utc[trading])


@pytest.mark.parametrize("switch", SWITCHES, ids=str)
def test_server_to_utc_is_the_plan_formula_and_inverts_the_real_clock(switch):
    """Vectorised as the plan's formula one minute at a time, on every server minute from Friday
    to Monday. Against the real clock (New York + 7 h at each instant) it is exact everywhere
    but the hour the autumn switch repeats: server 08:00 Sunday reads as its first pass."""
    friday = midnight(switch) - 2 * DAY
    server = np.arange(friday, friday + 4 * DAY, 60, dtype=np.int64)
    assert server_to_utc(server).tolist() == [plan_formula(s) for s in server.tolist()]

    utc = server  # the same minutes, now as UTC instants
    real = np.array([real_server(u) for u in utc.tolist()])
    back = server_to_utc(real)
    repeated = np.arange(midnight(switch) + 6 * HOUR, midnight(switch) + 7 * HOUR, 60)
    missed = repeated if switch in AUTUMN else repeated[:0]
    assert np.array_equal(utc[back != utc], missed)
    assert np.array_equal(back[back != utc], missed - HOUR)


# The structural clock checks


@pytest.fixture(scope="module")
def year_end():
    """Five months of synthetic bars over the autumn switch, New Year and the spring switch."""
    source = SyntheticBarSource(2, date(2025, 10, 13), date(2026, 3, 23))
    timeframes = (Timeframe.D1, Timeframe.H4, Timeframe.M15)
    return {tf: source.fetch("EURUSD", tf).time for tf in timeframes}


def test_bars_on_the_xm_clock_pass_every_check_in_every_year(year_end):
    assert check_clock(year_end) == {
        2025: {Timeframe.D1: 0, Timeframe.H4: 0, Timeframe.M15: 0},
        2026: {Timeframe.D1: 0, Timeframe.H4: 0, Timeframe.M15: 0},
    }


@pytest.mark.parametrize(
    "utc, timeframe, passes",
    [
        (epoch(2026, 3, 6, 22), Timeframe.D1, True),  # Friday 17:00 EST
        (epoch(2026, 3, 8, 21), Timeframe.D1, True),  # Sunday 17:00 EDT, the switch day
        (epoch(2026, 3, 8, 22), Timeframe.D1, False),  # Sunday 18:00 EDT
        (epoch(2026, 11, 1, 22), Timeframe.D1, True),  # Sunday 17:00 EST, the switch day
        (epoch(2026, 11, 1, 21), Timeframe.D1, False),  # Sunday 16:00 EST
        (epoch(2026, 7, 15, 1), Timeframe.H4, True),  # server 04:00 in summer
        (epoch(2026, 1, 15, 2), Timeframe.H4, True),  # server 04:00 in winter
        (epoch(2026, 1, 15, 1), Timeframe.H4, False),  # server 03:00
        (epoch(2026, 1, 15, 2, 0, 30), Timeframe.H4, False),  # off the hour
        (epoch(2026, 1, 15, 2, 45), Timeframe.M15, True),
        (epoch(2026, 1, 15, 2, 50), Timeframe.M15, False),
    ],
)
def test_each_rule_on_single_bars(utc, timeframe, passes):
    assert check_clock({timeframe: np.array([utc])}) == {2026: {timeframe: 0 if passes else 1}}


def test_a_year_with_a_shifted_clock_fails_by_name(year_end):
    """2026 read an hour late: D1 and H4 fail there, 2025 still passes. A whole-hour shift stays
    on the 15-minute grid; a 10-minute one leaves it."""

    def shifted(times, by):
        return np.where(times >= epoch(2026, 1, 1), times + by, times)

    hour = check_clock({tf: shifted(times, HOUR) for tf, times in year_end.items()})
    off_grid = check_clock({tf: shifted(times, 600) for tf, times in year_end.items()})

    in_2026 = {tf: int((times >= epoch(2026, 1, 1)).sum()) for tf, times in year_end.items()}
    assert hour[2025] == off_grid[2025] == {Timeframe.D1: 0, Timeframe.H4: 0, Timeframe.M15: 0}
    assert hour[2026] == {**in_2026, Timeframe.M15: 0}
    assert off_grid[2026] == in_2026


def test_years_are_utc_calendar_years():
    """The D1 bar of server 1 January 2026 opens at 22:00 UTC on 31 December, so in 2025."""
    d1 = np.array([epoch(2025, 12, 31, 22), epoch(2026, 1, 1, 23)])
    assert check_clock({Timeframe.D1: d1}) == {2025: {Timeframe.D1: 0}, 2026: {Timeframe.D1: 1}}


def test_a_timeframe_without_a_rule_is_refused():
    with pytest.raises(ValueError, match="no clock rule for H1"):
        check_clock({Timeframe.H1: np.array([epoch(2026, 1, 15)])})
