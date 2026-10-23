"""The data-quality mask (spec §4): which server days of a symbol's history a Trial may see.

Real history often starts thin, a bar or two a day before the broker's intraday data begins,
and later days can be thin too (a feed outage, a half-day holiday). Both are judged on M15 bars
per server day, against the symbol's median over the Research Window (2014-2024):

- the first usable month is the first calendar month whose median count per server day reaches
  half that median; every bar before it is excluded;
- from then on, a thin day is a server day with fewer than half that median, and it is excluded
  from every timeframe of the symbol.

Only server days that have closed are judged, and each one only once. The mask is frozen at the
first import: the median comes from the Research Window alone, and a later import judges just
the days that closed since. So a Trial's bars, and their fingerprint in the Ledger, never change.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

import numpy as np

from vibedesk.market.bars import Bars, Timeframe
from vibedesk.market.grant import HOLDOUT_START, RESEARCH_START
from vibedesk.market.server_time import server_days

THIN_FRACTION = 0.5  # of the Research Window's median M15 bars per server day

_EPOCH = date(1970, 1, 1)


@dataclass(frozen=True)
class Mask:
    """A symbol's frozen mask. Every day in it is a server day (New York + 7 h)."""

    median: float  # M15 bars per server day over the Research Window
    first_usable_month: date  # the first day of that month
    judged_through: date  # the last server day judged; later ones wait for the next import
    masked_days: tuple[date, ...]  # thin days, from the first usable month on

    def keep(self, time: np.ndarray) -> np.ndarray:
        """True for each bar, by UTC open time, on a judged day that the mask lets through.

        A day not judged yet is withheld too, so a bar once served is never taken back.
        """
        days = server_days(time)
        keep = (days >= _day_number(self.first_usable_month)) & (
            days <= _day_number(self.judged_through)
        )
        return keep & ~np.isin(days, [_day_number(day) for day in self.masked_days])

    def to_json(self) -> dict:
        return {
            "median_m15_bars_per_day": self.median,
            "first_usable_month": self.first_usable_month.strftime("%Y-%m"),
            "judged_through": self.judged_through.isoformat(),
            "masked_days": [day.isoformat() for day in self.masked_days],
        }

    @classmethod
    def from_json(cls, data: dict) -> Mask:
        return cls(
            median=float(data["median_m15_bars_per_day"]),
            first_usable_month=date.fromisoformat(data["first_usable_month"] + "-01"),
            judged_through=date.fromisoformat(data["judged_through"]),
            masked_days=tuple(date.fromisoformat(day) for day in data["masked_days"]),
        )


def build_mask(m15: Bars, cutoff: datetime) -> Mask:
    """Judge every server day of `m15` that closed by `cutoff`, and freeze the median.

    The median counts only days with at least one bar, so weekends and holidays don't drag it.
    """
    days, counts, cutoff_day = _closed_day_counts(m15, cutoff)
    research = (days >= _day_number(RESEARCH_START.date())) & (
        days < _day_number(HOLDOUT_START.date())
    )
    if not research.any():
        raise ValueError(f"{m15.symbol} has no M15 bars in the Research Window to judge days by")
    median = float(np.median(counts[research]))
    threshold = THIN_FRACTION * median

    months = days.astype("datetime64[D]").astype("datetime64[M]")
    usable = [m for m in np.unique(months) if np.median(counts[months == m]) >= threshold]
    if not usable:
        raise ValueError(f"no month of {m15.symbol} reaches half its median M15 bars per day")
    first_month = usable[0].astype("datetime64[D]").item()
    thin = (days >= _day_number(first_month)) & (counts < threshold)
    return Mask(
        median=median,
        first_usable_month=first_month,
        judged_through=_date(cutoff_day - 1),
        masked_days=tuple(_date(day) for day in days[thin]),
    )


def extend_mask(mask: Mask, m15: Bars, cutoff: datetime) -> Mask:
    """`mask` plus a judgement of the server days of `m15` that closed after the last judged one
    and by `cutoff`, against the frozen median. Days judged before are never judged again."""
    days, counts, cutoff_day = _closed_day_counts(m15, cutoff)
    new = days > _day_number(mask.judged_through)
    thin = days[new & (counts < THIN_FRACTION * mask.median)]
    return Mask(
        median=mask.median,
        first_usable_month=mask.first_usable_month,
        judged_through=max(mask.judged_through, _date(cutoff_day - 1)),
        masked_days=mask.masked_days + tuple(_date(day) for day in thin),
    )


def _closed_day_counts(m15: Bars, cutoff: datetime) -> tuple[np.ndarray, np.ndarray, int]:
    """(server days, M15 bars on each) for the days that closed by `cutoff`, and its own day."""
    if m15.timeframe is not Timeframe.M15:
        raise ValueError(f"days are judged on M15 bars, not {m15.timeframe}")
    if cutoff.tzinfo is None or cutoff.utcoffset() is None:
        raise ValueError("cutoff must be a timezone-aware UTC datetime")
    cutoff_day = int(server_days(np.array([int(cutoff.timestamp())]))[0])
    days, counts = np.unique(server_days(m15.time), return_counts=True)
    closed = days < cutoff_day
    return days[closed], counts[closed], cutoff_day


def _day_number(day: date) -> int:
    return (day - _EPOCH).days


def _date(day_number: int) -> date:
    return _EPOCH + timedelta(days=int(day_number))
