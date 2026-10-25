"""The data report's clock checks, on years built from first principles.

Schedules are written in New York time. GOLD: open from Sunday 18:00 to Friday 17:00, with a break
from 17:00 to 18:00 every weekday. FX: open from Sunday 17:00 to Friday 17:00, round the clock.
XM's server runs on European time (EET/EEST, UTC+2 in winter, UTC+3 in summer) and builds D1 bars
at its own midnight. Most of the year that midnight is 17:00 New York; in the weeks between the
US and the European daylight-saving switches it is 18:00, so the FX week opens at server Sunday
23:00 and MT5 builds a one-hour Sunday D1 bar. The importer stores `server_to_utc(stamp)`.

A broker on another clock is stamped here by its own rule and stored as if it were XM's: the old
assumption (New York + 7 h) or a fixed GMT+2. Built without SyntheticBarSource, which aggregates
on the server clock and would partly assume the answer.
"""

from datetime import UTC, date, datetime

import numpy as np
import pytest

from vibedesk.market.bars import Bars, Timeframe
from vibedesk.market.cache import record_import, write_chunk
from vibedesk.market.report import data_report, xm_clock_checks
from vibedesk.market.server_time import (
    DAY,
    server_days,
    server_offset,
    server_to_utc,
    server_weekdays,
    session_offset,
)

HOLIDAYS_2024 = (date(2024, 1, 1), date(2024, 12, 25))  # New York dates GOLD doesn't trade
# The weeks between the switches in 2024 open on these server Sundays (US 03-10, EU 03-31 and
# 10-27, US 11-03).
OPENING_SUNDAYS_2024 = [date(2024, 3, 10), date(2024, 3, 17), date(2024, 3, 24), date(2024, 10, 27)]


def utc(*args) -> int:
    return int(datetime(*args, tzinfo=UTC).timestamp())


def gold_m15(year: int, holidays=HOLIDAYS_2024, months=range(1, 13), *, fx=False) -> np.ndarray:
    """True UTC open times of GOLD's (or, with `fx`, an FX pair's) M15 bars in `year`, from the
    New York schedule."""
    t = np.arange(utc(year, 1, 1), utc(year + 1, 1, 1), 900, dtype=np.int64)
    ny = t + session_offset(t) - 7 * 3600  # New York wall time (the session clock less 7 h)
    weekday = ((ny // DAY) + 3) % 7  # Monday 0
    minute = (ny % DAY) // 60
    reopen = 17 * 60 if fx else 18 * 60
    weekday_open = np.full(len(t), True) if fx else (minute < 17 * 60) | (minute >= 18 * 60)
    trading = np.select(
        [weekday <= 3, weekday == 4, weekday == 6],
        [weekday_open, minute < 17 * 60, minute >= reopen],
        default=False,
    )
    ny_date = (ny // DAY).astype("datetime64[D]")
    trading &= ~np.isin(ny_date, np.array(holidays, dtype="datetime64[D]"))
    trading &= np.isin(ny_date.astype("datetime64[M]").astype(int) % 12 + 1, list(months))
    return t[trading]


def d1_on_server_days(m15: np.ndarray, stamp_offset) -> np.ndarray:
    """D1 open times as a broker builds them at its own midnight: `stamp_offset(utc)` turns UTC
    into that broker's server clock."""
    stamps = m15 + stamp_offset(m15)
    return np.unique(stamps // DAY) * DAY


def xm_stamps(m15: np.ndarray) -> np.ndarray:
    return server_offset(m15)


def ny7_stamps(m15: np.ndarray) -> np.ndarray:
    return session_offset(m15)


def gmt2_stamps(m15: np.ndarray) -> np.ndarray:
    return np.full(len(m15), 7200, dtype=np.int64)


def as_stored(stamps: np.ndarray) -> np.ndarray:
    """What the importer stores for server stamps: their UTC on XM's clock."""
    return np.sort(server_to_utc(stamps))


def xm_year(year: int, **kwargs) -> tuple[np.ndarray, np.ndarray]:
    m15 = gold_m15(year, **kwargs)
    return m15, as_stored(d1_on_server_days(m15, xm_stamps))


@pytest.mark.parametrize("fx", [False, True])
def test_an_xm_year_with_holidays_passes(fx):
    m15, d1 = xm_year(2024, fx=fx)

    checks = xm_clock_checks(m15, d1)
    xm = checks[2024]

    # Server 1 January 2024 opens at 22:00 UTC on 31 December, so 2023 holds that one D1 bar.
    assert set(checks) == {2023, 2024} and checks[2023].passed
    assert (xm.weekend_bars, xm.long_weeks) == (0, 0)
    # 17:00 New York is session 00:00, summer and winter; FX has no break.
    assert xm.summer_break == xm.winter_break == (() if fx else (0,))
    assert xm.passed


def test_fx_weeks_between_the_switches_open_on_a_server_sunday_and_pass():
    """XM's real bars (CCC-85): the FX week opens at server Sunday 23:00 in those weeks, so MT5
    builds a sixth, Sunday D1 bar. The New York week explains it; it is no failure."""
    m15, d1 = xm_year(2024, fx=True)

    sundays = d1[server_weekdays(d1) == 6]
    assert [datetime.fromtimestamp(int(t) + DAY, UTC).date() for t in sundays] == [
        date(2024, 3, 10), date(2024, 3, 17), date(2024, 3, 24), date(2024, 10, 27)
    ]  # fmt: skip
    assert (server_weekdays(m15) == 6).sum() == 4 * len(OPENING_SUNDAYS_2024)
    xm = xm_clock_checks(m15, d1)[2024]
    assert (xm.weekend_bars, xm.long_weeks) == (0, 0) and xm.passed


def test_a_server_sunday_bar_outside_those_weeks_fails():
    m15, d1 = xm_year(2024, fx=True)
    stray = utc(2024, 6, 15, 21)  # server Sunday 16 June 00:00: New York's weekend all day

    xm = xm_clock_checks(m15, np.sort(np.r_[d1, stray]))[2024]

    assert (xm.weekend_bars, xm.long_weeks) == (1, 1) and not xm.passed


def test_a_week_with_a_holiday_has_four_d1_bars_and_passes():
    m15, d1 = xm_year(2024)
    holiday = utc(2024, 7, 3, 21)  # server Thursday 4 July, the whole day
    m15 = m15[(m15 < holiday) | (m15 >= holiday + DAY)]
    d1 = d1[(d1 < holiday) | (d1 >= holiday + DAY)]

    xm = xm_clock_checks(m15, d1)[2024]

    assert 4 in np.unique((server_days(d1) + 3) // 7, return_counts=True)[1]
    assert xm.long_weeks == 0 and xm.passed


@pytest.mark.parametrize("fx", [False, True])
def test_a_new_york_plus_7_broker_fails_between_the_switches(fx):
    """The clock the importer used to assume: in the weeks between the switches its stamps are
    stored an hour late, so the week's last hour lands in New York's weekend."""
    m15 = gold_m15(2024, fx=fx)
    stored_m15 = as_stored(m15 + ny7_stamps(m15))
    stored_d1 = as_stored(d1_on_server_days(m15, ny7_stamps))

    xm = xm_clock_checks(stored_m15, stored_d1)[2024]

    assert xm.weekend_bars == 4 * 4  # the last hour of each of the four weeks
    assert not xm.passed


def test_a_fixed_gmt2_year_moves_golds_break():
    m15 = gold_m15(2024)
    stored_m15 = as_stored(m15 + gmt2_stamps(m15))
    stored_d1 = as_stored(d1_on_server_days(m15, gmt2_stamps))

    xm = xm_clock_checks(stored_m15, stored_d1)[2024]

    # Stored as EEST in European summer, a GMT+2 stamp reads an hour early: the break moves.
    assert xm.winter_break == (0,)
    assert xm.break_moved and not xm.passed


def test_a_fixed_gmt2_year_opens_the_fx_week_in_new_yorks_weekend():
    m15 = gold_m15(2024, fx=True)
    stored_m15 = as_stored(m15 + gmt2_stamps(m15))
    stored_d1 = as_stored(d1_on_server_days(m15, gmt2_stamps))

    xm = xm_clock_checks(stored_m15, stored_d1)[2024]

    # 17:00 New York on a summer Sunday reads as 16:00: still New York's weekend.
    assert xm.weekend_bars > 0
    assert not xm.passed


def test_a_season_with_too_few_days_leaves_the_break_unjudged():
    m15, d1 = xm_year(2024, months=(1, 2))  # winter only

    xm = xm_clock_checks(m15, d1)[2024]

    assert xm.summer_break is None and xm.break_moved is None and xm.passed


def test_a_symbol_trading_round_the_clock_has_no_break_to_move():
    m15, d1 = xm_year(2024, holidays=(), fx=True)

    xm = xm_clock_checks(m15, d1)[2024]

    assert xm.summer_break == xm.winter_break == ()
    assert xm.passed


def write_cache(root, m15, d1, *, year=2024, spread=30):
    for tf, times in ((Timeframe.M15, m15), (Timeframe.D1, d1)):
        n = len(times)
        price = np.full(n, 2000.0)
        bars = Bars("GOLD", tf, 0.01, 2, times, price, price + 1, price - 1, price,
                    np.full(n, spread))  # fmt: skip
        write_chunk(root, bars.between(utc(year, 1, 1), utc(year + 1, 1, 1)), year)
    record_import(root, "GOLD", datetime(year + 1, 1, 6, tzinfo=UTC))


def test_the_data_report_of_an_xm_year(tmp_path):
    write_cache(tmp_path, *xm_year(2024))

    report = data_report(tmp_path, "GOLD")

    [year] = report.years
    assert report.first_usable_month == date(2024, 1, 1)
    assert report.passed and year.passed
    # Christmas in New York leaves server 25 December six hours of bars: a thin day.
    assert date(2024, 12, 25) in year.thin_days
    assert year.bars[Timeframe.M15] > 20_000 and year.bars[Timeframe.D1] > 250


def test_the_hour_fx_trades_on_a_server_sunday_is_a_thin_day(tmp_path):
    """The mask judges server days: an FX opening Sunday holds one hour of bars, so it is thin
    and masked, with its D1 bar; the year still passes."""
    write_cache(tmp_path, *xm_year(2024, fx=True))

    [year] = data_report(tmp_path, "GOLD").years

    assert [day for day in year.thin_days if day.weekday() == 6] == OPENING_SUNDAYS_2024
    assert year.passed


def test_a_weekday_without_bars_is_reported_as_empty_not_thin(tmp_path):
    m15, d1 = xm_year(2024)
    day = utc(2024, 7, 3, 21)  # server Thursday 4 July
    write_cache(tmp_path, m15[(m15 < day) | (m15 >= day + DAY)], d1[d1 != day])

    [year] = data_report(tmp_path, "GOLD").years

    assert date(2024, 7, 4) in year.empty_weekdays
    assert date(2024, 7, 4) not in year.thin_days
    assert year.passed


def test_the_data_report_fails_a_fixed_gmt2_year_on_xms_clock_alone(tmp_path):
    m15 = gold_m15(2024)
    stored_m15 = as_stored(m15 + gmt2_stamps(m15))
    stored_d1 = as_stored(d1_on_server_days(m15, gmt2_stamps))
    write_cache(tmp_path, stored_m15, stored_d1)

    [year] = data_report(tmp_path, "GOLD").years

    assert not any(year.conversion.values())  # our conversion can't see it ...
    assert not year.xm.passed and not year.passed  # ... XM's clock can


def test_the_data_report_fails_a_shifted_clock_on_our_conversion(tmp_path):
    m15, d1 = xm_year(2024)
    write_cache(tmp_path, m15 + 600, d1 + 600)  # ten minutes off: a bug of our own

    [year] = data_report(tmp_path, "GOLD").years

    assert year.conversion[Timeframe.M15] > 0 and year.conversion[Timeframe.D1] > 0
    assert not year.passed


@pytest.mark.parametrize("field", ["open", "high", "low", "close"])
def test_the_data_report_holds_no_price(tmp_path, field):
    write_cache(tmp_path, *xm_year(2024))

    report = data_report(tmp_path, "GOLD")

    assert field not in {f for year in report.years for f in vars(year)}
    text = repr(report)
    assert "2000.0" not in text and "2001.0" not in text and "1999.0" not in text
