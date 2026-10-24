"""The data report (M3 plan, "The desk CLI"): structural facts about a cached symbol, per year.

Two kinds of clock check answer different questions, and the report keeps them apart:
- **Our conversion** (`server_time.check_clock`): D1 bars open at server midnight (00:00),
  H4 bars on server hours divisible by 4, M15 bars on the 15-minute grid. These catch our own bugs,
  times shifted between MT5 and the cache. MT5 builds its bars on its own clock, so they pass
  whatever whole-hour offset XM's clock had.
- **XM's clock** (`xm_clock_checks`): XM's server runs on European time (EET/EEST) while the
  market week runs on New York time (market/server_time.py). If our model of either clock were
  wrong in some year, bars would land outside the New York trading week or GOLD's daily break
  would move between US summer and winter. So: no M15 bar opens between Friday 17:00 and Sunday
  17:00 New York; no D1 bar covers a server day wholly outside that week (a server Sunday bar is
  right only in the weeks between the US and European switches, when the week opens at server
  Sunday 23:00); no server week has more than 5 D1 bars besides such a Sunday bar; and the daily
  break sits at the same New York hours in US summer and winter.

Everything here comes from bar times, spreads and the data-quality mask, never from prices, and it
is the same for Holdout years as for any other: the report is no side door into the Holdout
(ADR 0002).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

import numpy as np

from vibedesk.market.bars import Timeframe
from vibedesk.market.cache import read_meta, read_unmasked, summarize
from vibedesk.market.server_time import (
    DAY,
    HOUR,
    check_clock,
    server_days,
    server_to_utc,
    server_weekdays,
    session_hours,
    session_offset,
    session_weekdays,
)

BREAK_SHARE = 0.10  # a session hour with M15 bars on fewer days than this is a break hour
MIN_SEASON_DAYS = 20  # Tuesdays to Thursdays a season needs before its break hours are judged
_US_SUMMER = 3 * HOUR  # the session clock's UTC offset in US daylight saving (New York + 7 h)
_EPOCH = date(1970, 1, 1)


@dataclass(frozen=True)
class XmClock:
    """One year of XM's clock: what was found, and whether it fits a European server clock with
    a New York market week."""

    weekend_bars: int  # M15 bars in the New York weekend, D1 bars on a server day outside the week
    long_weeks: int  # server weeks with more than 5 D1 bars, besides a Sunday bar the week opens in
    summer_break: tuple[int, ...] | None  # session hours of the daily break; None: too few days
    winter_break: tuple[int, ...] | None

    @property
    def break_moved(self) -> bool | None:
        """None when a season has too few days to judge."""
        if self.summer_break is None or self.winter_break is None:
            return None
        return self.summer_break != self.winter_break

    @property
    def passed(self) -> bool:
        return self.weekend_bars == 0 and self.long_weeks == 0 and self.break_moved is not True


def xm_clock_checks(m15: np.ndarray, d1: np.ndarray, *, break_m15: np.ndarray | None = None):
    """XM's clock per UTC calendar year, from bar open times in UTC.

    `break_m15` is the M15 bars to find the daily break in (by default all of `m15`); the data
    report passes only the days the mask serves, so a sparse start or a thin day can't fake one.
    The break is read on the session clock (00h is 17:00 New York), on Tuesdays to Thursdays
    only, away from the week's open and close.
    """
    m15 = np.asarray(m15, dtype=np.int64)
    d1 = np.asarray(d1, dtype=np.int64)
    break_m15 = m15 if break_m15 is None else np.asarray(break_m15, dtype=np.int64)
    report: dict[int, XmClock] = {}
    for year in sorted(set(_years(m15).tolist()) | set(_years(d1).tolist())):
        in_m15, in_d1 = m15[_years(m15) == year], d1[_years(d1) == year]
        outside = _outside_week(in_d1)
        weekend = int((session_weekdays(in_m15) >= 5).sum() + outside.sum())
        breaks = break_m15[_years(break_m15) == year]
        summer = session_offset(breaks) == _US_SUMMER
        opening_sundays = (server_weekdays(in_d1) == 6) & ~outside
        report[year] = XmClock(
            weekend_bars=weekend,
            long_weeks=_long_weeks(in_d1[~opening_sundays]),
            summer_break=_break_hours(breaks[summer]),
            winter_break=_break_hours(breaks[~summer]),
        )
    return report


@dataclass(frozen=True)
class YearReport:
    year: int
    bars: dict[Timeframe, int]  # bars in the cache, per timeframe
    thin_days: tuple[date, ...]  # masked server days that had some M15 bars
    empty_weekdays: tuple[date, ...]  # masked server weekdays with no M15 bars (holidays too)
    gaps: int  # M15 stretches of 2 hours or more without a bar, inside a server week
    spread_median: float | None  # M15, in points
    conversion: dict[Timeframe, int]  # bars breaking each of our conversion's rules
    xm: XmClock | None  # None for a year without M15 or D1 bars

    @property
    def passed(self) -> bool:
        return not any(self.conversion.values()) and (self.xm is None or self.xm.passed)


@dataclass(frozen=True)
class DataReport:
    symbol: str
    first_usable_month: date | None
    judged_through: date | None
    last_import: datetime | None
    years: tuple[YearReport, ...]

    @property
    def passed(self) -> bool:
        return all(year.passed for year in self.years)


def data_report(root, symbol: str) -> DataReport:
    """The data report of one cached symbol. Reads M15, H4 and D1 times and the summary; never M1
    bars, never prices."""
    meta = read_meta(root, symbol)
    summary = summarize(root, symbol)
    times = {tf: read_unmasked(root, symbol, tf).time for tf in (Timeframe.M15, Timeframe.H4,
                                                                  Timeframe.D1)}  # fmt: skip
    m15 = times[Timeframe.M15]
    served = m15[meta.mask.keep(m15)] if meta.mask else m15
    conversion = check_clock(times)
    xm = xm_clock_checks(m15, times[Timeframe.D1], break_m15=served)

    m15_days = set(server_days(m15).tolist())
    masked = meta.mask.masked_days if meta.mask else ()
    rows: dict[int, dict] = {}
    for row in summary.years:
        entry = rows.setdefault(row.year, {"bars": {}})
        entry["bars"][row.timeframe] = row.bars
        if row.timeframe is Timeframe.M15:
            entry["gaps"], entry["spread"] = row.gaps, row.spread_median
    years = []
    for year in sorted(set(rows) | set(conversion) | set(xm)):
        in_year = [day for day in masked if day.year == year]
        thin = tuple(day for day in in_year if (day - _EPOCH).days in m15_days)
        entry = rows.get(year, {"bars": {}})
        years.append(
            YearReport(
                year=year,
                bars=entry["bars"],
                thin_days=thin,
                empty_weekdays=tuple(day for day in in_year if day not in thin),
                gaps=entry.get("gaps", 0),
                spread_median=entry.get("spread"),
                conversion=conversion.get(year, {}),
                xm=xm.get(year),
            )
        )
    mask = meta.mask
    return DataReport(
        symbol=symbol,
        first_usable_month=mask.first_usable_month if mask else None,
        judged_through=mask.judged_through if mask else None,
        last_import=meta.last_import,
        years=tuple(years),
    )


def _years(utc: np.ndarray) -> np.ndarray:
    return utc.astype("datetime64[s]").astype("datetime64[Y]").astype(np.int64) + 1970


def _outside_week(d1: np.ndarray) -> np.ndarray:
    """True for each D1 bar whose server day lies wholly in the New York weekend (Friday 17:00 to
    Sunday 17:00). The weekend is 48 hours and a server day at most 25, so a day overlaps the
    week exactly when its first or last second does."""
    if not len(d1):
        return np.zeros(0, dtype=bool)
    end = server_to_utc((server_days(d1) + 1) * DAY) - 1
    return (session_weekdays(d1) >= 5) & (session_weekdays(end) >= 5)


def _long_weeks(d1: np.ndarray) -> int:
    """Server weeks (Monday to Sunday) with more than 5 D1 bars."""
    if not len(d1):
        return 0
    _, counts = np.unique((server_days(d1) + 3) // 7, return_counts=True)
    return int((counts > 5).sum())


def _break_hours(m15: np.ndarray) -> tuple[int, ...] | None:
    """Session hours with M15 bars on fewer than BREAK_SHARE of the Tuesdays to Thursdays that
    have any bars; None with fewer than MIN_SEASON_DAYS such days."""
    weekday = session_weekdays(m15)
    m15 = m15[(weekday >= 1) & (weekday <= 3)]
    days = (m15 + session_offset(m15)) // DAY
    n_days = len(np.unique(days))
    if n_days < MIN_SEASON_DAYS:
        return None
    day_hours = np.unique(days * 24 + session_hours(m15))
    per_hour = np.bincount(day_hours % 24, minlength=24)
    return tuple(int(h) for h in np.flatnonzero(per_hour < BREAK_SHARE * n_days))
