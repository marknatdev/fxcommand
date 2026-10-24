"""The XM server clock (EET/EEST, Europe/Athens), its inverse as tasks/plan.md defines it, and
the New York session clock (New York + 7 h) the market week runs on."""

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
    session_hours,
    session_offset,
    session_weekdays,
)
from vibedesk.market.synthetic import SyntheticBarSource

HOUR = 3600
SERVER = ZoneInfo("Europe/Athens")
NEW_YORK = ZoneInfo("America/New_York")

# Every European daylight-saving switch of 2014-2026: the last Sundays of March and October, at
# 01:00 UTC. XM's server clock switches on these.
SPRING = [date(2014, 3, 30), date(2015, 3, 29), date(2016, 3, 27), date(2017, 3, 26),
          date(2018, 3, 25), date(2019, 3, 31), date(2020, 3, 29), date(2021, 3, 28),
          date(2022, 3, 27), date(2023, 3, 26), date(2024, 3, 31), date(2025, 3, 30),
          date(2026, 3, 29)]  # fmt: skip
AUTUMN = [date(2014, 10, 26), date(2015, 10, 25), date(2016, 10, 30), date(2017, 10, 29),
          date(2018, 10, 28), date(2019, 10, 27), date(2020, 10, 25), date(2021, 10, 31),
          date(2022, 10, 30), date(2023, 10, 29), date(2024, 10, 27), date(2025, 10, 26),
          date(2026, 10, 25)]  # fmt: skip
SWITCHES = sorted(SPRING + AUTUMN)
# Every US switch of 2014-2026: the second Sunday of March, the first of November. The session
# clock, and the market week, switch on these.
US_SPRING = [date(2014, 3, 9), date(2015, 3, 8), date(2016, 3, 13), date(2017, 3, 12),
             date(2018, 3, 11), date(2019, 3, 10), date(2020, 3, 8), date(2021, 3, 14),
             date(2022, 3, 13), date(2023, 3, 12), date(2024, 3, 10), date(2025, 3, 9),
             date(2026, 3, 8)]  # fmt: skip
US_AUTUMN = [date(2014, 11, 2), date(2015, 11, 1), date(2016, 11, 6), date(2017, 11, 5),
             date(2018, 11, 4), date(2019, 11, 3), date(2020, 11, 1), date(2021, 11, 7),
             date(2022, 11, 6), date(2023, 11, 5), date(2024, 11, 3), date(2025, 11, 2),
             date(2026, 11, 1)]  # fmt: skip
US_SWITCHES = sorted(US_SPRING + US_AUTUMN)
# Over the first UTC hour of a switch Sunday, server_offset already uses the new offset (it
# looks up 12:00 UTC); the real clock switches at 01:00 UTC. Server Sunday, closed.
EARLY_SUNDAY = 1 * HOUR


def epoch(*args) -> int:
    return int(datetime(*args, tzinfo=UTC).timestamp())


def midnight(day: date) -> int:
    return epoch(day.year, day.month, day.day)


def server_weekday(utc: np.ndarray) -> np.ndarray:
    """0 = Monday ... 6 = Sunday on the server clock; 1970-01-01 was a Thursday."""
    return ((utc + server_offset(utc)) // DAY + 3) % 7


def plan_formula(server: int) -> int:
    """tasks/plan.md, one value at a time: utc = server - offset, the offset being the server
    zone's UTC offset at the server wall time."""
    wall = datetime.fromtimestamp(server, UTC).replace(tzinfo=None)
    return server - int(wall.replace(tzinfo=SERVER).utcoffset().total_seconds())


def real_server(utc: int) -> int:
    """The real XM clock at a UTC instant: Athens wall time."""
    offset = datetime.fromtimestamp(utc, UTC).astimezone(SERVER).utcoffset()
    return utc + int(offset.total_seconds())


@pytest.mark.parametrize(
    "utc, hours",
    [
        (epoch(2026, 10, 21, 12), 3),  # matches the 2026-10-21 probe: tick time ran UTC+3
        (epoch(2026, 1, 15, 12), 2),
        (epoch(2026, 3, 6, 20), 2),  # Friday before the US switch on 2026-03-08
        (epoch(2026, 3, 8, 21), 2),  # after the US switch, before the European one: still +2
        (epoch(2026, 3, 27, 20), 2),  # Friday before the European switch on 2026-03-29
        (epoch(2026, 3, 29, 21), 3),  # Sunday 21:00 UTC is server Monday 00:00 after it
        (epoch(2026, 10, 23, 20), 3),  # Friday before the European switch back on 2026-10-25
        (epoch(2026, 10, 26, 12), 2),  # Europe is back on winter time, New York is not yet
        (epoch(2026, 11, 1, 22), 2),  # Sunday 22:00 UTC is server Monday 00:00 in winter
    ],
)
def test_the_server_clock_is_european(utc, hours):
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


@pytest.mark.parametrize("switch", SWITCHES + US_SWITCHES, ids=str)
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
    utc = switch_weekend(date(2026, 3, 29))
    server_days(utc)
    server_days(utc[::-1])
    assert lookups == [4, len(utc)]


@pytest.mark.parametrize("name", HELPERS)
def test_each_helper_keeps_the_shape(name):
    """Like server_offset: no times, one time, a single number and a sorted grid."""
    helper, written_out = HELPERS[name]
    friday, sunday_open = epoch(2026, 3, 27, 20), epoch(2026, 3, 29, 21)
    for utc in (
        np.array([], dtype=np.int64),
        np.array([sunday_open]),
        sunday_open,
        np.array([[friday, friday + HOUR], [sunday_open, sunday_open + HOUR]]),
    ):
        got, expected = helper(utc), written_out(utc)
        assert np.shape(got) == np.shape(expected)
        assert np.array_equal(got, expected)


# The session clock and the weeks between the US and European switches


@pytest.mark.parametrize(
    "utc, hours",
    [
        (epoch(2026, 1, 15, 12), 2),  # New York EST + 7 h
        (epoch(2026, 7, 15, 12), 3),  # New York EDT + 7 h
        (epoch(2026, 3, 10, 12), 3),  # New York switched on 03-08
        (epoch(2026, 10, 27, 12), 3),  # and switches back on 11-01
        (epoch(2026, 11, 2, 12), 2),
    ],
)
def test_the_session_clock_is_new_york_plus_seven(utc, hours):
    assert session_offset(np.array([utc]))[0] == hours * HOUR


def test_the_session_clock_follows_new_york():
    """Every day of 2014-2026 at 12:00 UTC: New York's offset plus 7 h."""
    first = epoch(2014, 1, 1, 12)
    noon = np.arange(first, epoch(2027, 1, 1), DAY, dtype=np.int64)
    expected = [
        int(datetime.fromtimestamp(t, UTC).astimezone(NEW_YORK).utcoffset().total_seconds())
        + 7 * HOUR
        for t in noon.tolist()
    ]
    assert session_offset(noon).tolist() == expected


def mismatch_days() -> list[date]:
    """The days the server clock runs an hour behind the session clock: New York on summer time,
    Europe not (yet, or any more)."""
    days = []
    for us_spring, eu_spring, eu_autumn, us_autumn in zip(
        US_SPRING, SPRING, AUTUMN, US_AUTUMN, strict=True
    ):
        days += [us_spring + timedelta(days=i) for i in range((eu_spring - us_spring).days)]
        days += [eu_autumn + timedelta(days=i) for i in range((us_autumn - eu_autumn).days)]
    return days


def test_the_clocks_part_only_between_the_us_and_european_switches():
    """Server and session clock agree on every day of 2014-2026 but 3 to 4 weeks a year, and
    there the server runs one hour behind."""
    first = epoch(2014, 1, 1, 12)
    noon = np.arange(first, epoch(2027, 1, 1), DAY, dtype=np.int64)
    gap = session_offset(noon) - server_offset(noon)
    parted = [date(1970, 1, 1) + timedelta(days=int(t // DAY)) for t in noon[gap != 0]]
    assert set(gap.tolist()) == {0, HOUR}
    assert parted == mismatch_days()
    per_year = {year: sum(day.year == year for day in parted) // 7 for year in range(2014, 2027)}
    assert set(per_year.values()) <= {3, 4}


@pytest.mark.parametrize(
    "server, utc, new_york",
    [
        # 2024-03-10 to 03-31: New York opens the week at 17:00 EDT, server Sunday 23:00
        ((2024, 3, 10, 23), (2024, 3, 10, 21), (2024, 3, 10, 17)),
        ((2024, 3, 15, 23), (2024, 3, 15, 21), (2024, 3, 15, 17)),  # and closes it there
        ((2024, 3, 13, 0), (2024, 3, 12, 22), (2024, 3, 12, 18)),  # GOLD reopens after its break
        # 2024-10-27 to 11-03, the autumn week between the switches
        ((2024, 10, 27, 23), (2024, 10, 27, 21), (2024, 10, 27, 17)),
        # outside them the week opens at server Monday 00:00, 17:00 New York
        ((2024, 4, 1, 0), (2024, 3, 31, 21), (2024, 3, 31, 17)),
        ((2024, 11, 4, 0), (2024, 11, 3, 22), (2024, 11, 3, 17)),
    ],
)
def test_server_times_between_the_switches_are_new_york_market_hours(server, utc, new_york):
    """CCC-85: the real bars put the week's open, its close and GOLD's daily break at 17:00 or
    18:00 New York all year; these are their server stamps in the weeks between the switches."""
    got = int(server_to_utc(np.array([epoch(*server)]))[0])
    assert got == epoch(*utc)
    assert datetime.fromtimestamp(got, UTC).astimezone(NEW_YORK).replace(tzinfo=None) == (
        datetime(*new_york)
    )


@pytest.mark.parametrize(
    "new_york, weekday, hour",
    [
        ((2026, 3, 13, 16, 59), 4, 23),  # Friday 16:59 New York: the last minute of the week
        ((2026, 3, 13, 17, 0), 5, 0),  # Friday 17:00: the weekend
        ((2026, 3, 15, 16, 59), 6, 23),
        ((2026, 3, 15, 17, 0), 0, 0),  # Sunday 17:00: Monday on the session clock
        ((2026, 1, 15, 17, 0), 4, 0),  # in winter too
    ],
)
def test_session_weekdays_and_hours_turn_at_17_new_york(new_york, weekday, hour):
    utc = int(datetime(*new_york, tzinfo=NEW_YORK).timestamp())
    assert session_weekdays(np.array([utc]))[0] == weekday
    assert session_hours(np.array([utc]))[0] == hour


# Server clock to UTC


def test_the_named_switches_are_europes_and_new_yorks():
    """The switch lists the tests name, checked against the time-zone database."""
    first, last = date(2014, 1, 1), date(2026, 12, 31)
    days = [first + timedelta(days=i) for i in range((last - first).days + 1)]
    for zone, named in ((SERVER, SWITCHES), (NEW_YORK, US_SWITCHES)):
        noon = [datetime(d.year, d.month, d.day, 12, tzinfo=zone).utcoffset() for d in days]
        pairs = zip(days[1:], noon[:-1], noon[1:], strict=True)
        assert [day for day, before, after in pairs if before != after] == named


@pytest.mark.parametrize(
    "server, utc",
    [
        (epoch(2026, 1, 15, 0), epoch(2026, 1, 14, 22)),  # winter: server midnight is 22:00 UTC
        (epoch(2026, 7, 15, 0), epoch(2026, 7, 14, 21)),  # summer: 21:00 UTC
        (epoch(2026, 3, 30, 0), epoch(2026, 3, 29, 21)),  # the week opens after the spring switch
        (epoch(2026, 3, 28, 0), epoch(2026, 3, 27, 22)),  # the week closes before it
        (epoch(2026, 10, 26, 0), epoch(2026, 10, 25, 22)),  # the week opens after the autumn one
        (epoch(2026, 10, 24, 0), epoch(2026, 10, 23, 21)),  # the week closes before it
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
    back are the early UTC hours of each switch Sunday (1 h in spring, 2 h in autumn), all on
    server Sunday, when no bar opens: there server_offset is knowingly approximate."""
    checked, wrong = 0, []
    for year in range(2014, 2027):
        utc = np.arange(epoch(year, 1, 1), epoch(year + 1, 1, 1), 60, dtype=np.int64)
        back = server_to_utc(utc + server_offset(utc))
        wrong.append(utc[back != utc])
        checked += len(utc)
    wrong = np.concatenate(wrong)

    expected = np.concatenate(
        [
            np.arange(midnight(day), midnight(day) + (2 * HOUR if day in AUTUMN else 1 * HOUR), 60)
            for day in SWITCHES
        ]
    )
    assert checked == (date(2027, 1, 1) - date(2014, 1, 1)).days * 1440
    assert len(wrong) == 13 * 60 + 13 * 120
    assert np.array_equal(wrong, expected)
    assert set(server_weekday(wrong).tolist()) == {6}


@pytest.mark.parametrize("switch", SWITCHES, ids=str)
def test_every_trading_minute_round_trips_over_the_switch_weekend(switch):
    """Friday to Monday around the switch: Friday, Sunday from the open and Monday come back
    exactly; the early Sunday hours (1 h in spring, 2 h in autumn) are the only miss, and are
    closed. New York is on summer time at both European switches, so the week opens at 21:00 UTC:
    server Monday 00:00 after the spring switch, server Sunday 23:00 after the autumn one."""
    friday = midnight(switch) - 2 * DAY
    utc = np.arange(friday, friday + 4 * DAY, 60, dtype=np.int64)

    back = server_to_utc(utc + server_offset(utc))

    sunday = midnight(switch)
    early = 2 * HOUR if switch in AUTUMN else 1 * HOUR
    assert np.array_equal(utc[back != utc], np.arange(sunday, sunday + early, 60))
    open_ = sunday + 21 * HOUR  # 17:00 New York
    server_open = midnight(switch) + DAY - (0 if switch in SPRING else HOUR)
    assert server_offset(np.array([open_]))[0] + open_ == server_open
    trading = (utc < friday + DAY) | (utc >= open_)
    assert np.array_equal(back[trading], utc[trading])


@pytest.mark.parametrize("switch", SWITCHES, ids=str)
def test_server_to_utc_is_the_plan_formula_and_inverts_the_real_clock(switch):
    """Vectorised as the plan's formula one minute at a time, on every server minute from Friday
    to Monday. Against the real clock (Athens wall time at each instant) it is exact everywhere
    but the hour the autumn switch repeats: server 03:00 Sunday reads as its first pass."""
    friday = midnight(switch) - 2 * DAY
    server = np.arange(friday, friday + 4 * DAY, 60, dtype=np.int64)
    assert server_to_utc(server).tolist() == [plan_formula(s) for s in server.tolist()]

    utc = server  # the same minutes, now as UTC instants
    real = np.array([real_server(u) for u in utc.tolist()])
    back = server_to_utc(real)
    repeated = np.arange(midnight(switch) + 1 * HOUR, midnight(switch) + 2 * HOUR, 60)
    missed = repeated if switch in AUTUMN else repeated[:0]
    assert np.array_equal(utc[back != utc], missed)
    assert np.array_equal(back[back != utc], missed - HOUR)


# The structural clock checks


@pytest.fixture(scope="module")
def year_end():
    """Five months of synthetic bars over the autumn switches, New Year and the spring ones."""
    source = SyntheticBarSource(2, date(2025, 10, 13), date(2026, 4, 6))
    timeframes = (Timeframe.D1, Timeframe.H4, Timeframe.M15)
    return {tf: source.fetch("EURUSD", tf).time for tf in timeframes}


def test_bars_on_the_xm_clock_pass_every_check_in_every_year(year_end):
    assert check_clock(year_end) == {
        2025: {Timeframe.D1: 0, Timeframe.H4: 0, Timeframe.M15: 0},
        2026: {Timeframe.D1: 0, Timeframe.H4: 0, Timeframe.M15: 0},
    }


def test_synthetic_weeks_open_at_17_new_york_and_mt5_gives_them_a_sunday_bar(year_end):
    """As in XM's real bars: between the switches the week opens at server Sunday 23:00, so MT5
    builds a one-hour Sunday D1 bar there, and only there."""
    d1 = year_end[Timeframe.D1]
    sundays = d1[server_weekdays(d1) == 6]
    days = [date(1970, 1, 1) + timedelta(days=int(d)) for d in server_days(sundays)]
    assert days == [date(2025, 10, 26), date(2026, 3, 8), date(2026, 3, 15), date(2026, 3, 22)]
    m15 = year_end[Timeframe.M15]
    assert (session_weekdays(m15) >= 5).sum() == 0
    assert (server_weekdays(m15) == 6).sum() == 4 * len(days)


@pytest.mark.parametrize(
    "utc, timeframe, passes",
    [
        (epoch(2026, 3, 5, 22), Timeframe.D1, True),  # server Friday 00:00 in winter
        (epoch(2026, 3, 8, 22), Timeframe.D1, True),  # server Monday 00:00, 18:00 EDT
        (epoch(2026, 3, 8, 21), Timeframe.D1, False),  # 17:00 EDT is server Sunday 23:00
        (epoch(2026, 3, 29, 21), Timeframe.D1, True),  # server Monday after the spring switch
        (epoch(2026, 3, 29, 22), Timeframe.D1, False),
        (epoch(2026, 10, 25, 22), Timeframe.D1, True),  # server Monday after the autumn switch
        (epoch(2026, 10, 25, 21), Timeframe.D1, False),
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
