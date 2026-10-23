"""Spec §4 and tasks/plan.md ("Data-quality mask"): the first usable month and thin days, judged
on M15 bars per server day against half the Research Window's median, frozen once judged."""

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import numpy as np
import pytest

from vibedesk.market.bars import Bars, Timeframe
from vibedesk.market.quality import Mask, build_mask, extend_mask, server_days
from vibedesk.market.server_time import DAY, server_offset
from vibedesk.market.synthetic import SyntheticBarSource

EPOCH = date(1970, 1, 1)
COLUMNS = ("time", "open", "high", "low", "close", "spread")

# Synthetic GOLD with a planted sparse start (one M15 bar a day through January) and planted
# thin days. A full synthetic day has 96 M15 bars, so half the median is 48: a day with 48 bars
# is not thin, one with 47 is. The range ends on a Saturday, so its last server day has closed.
PLANTED = SyntheticBarSource(seed=21, start=date(2024, 1, 1), end=date(2024, 5, 4))
CUTOFF = datetime(2024, 5, 4, tzinfo=UTC)
SPARSE_UNTIL = date(2024, 2, 1)
THIN = {date(2024, 2, 21): 48, date(2024, 3, 13): 47, date(2024, 4, 17): 20}
MASKED = (date(2024, 3, 13), date(2024, 4, 17))


def as_dates(day_numbers) -> list[date]:
    return [EPOCH + timedelta(days=int(day)) for day in day_numbers]


def take(bars: Bars, keep: np.ndarray) -> Bars:
    return replace(bars, **{name: getattr(bars, name)[keep] for name in COLUMNS})


def plant(bars: Bars, sparse_until: date = SPARSE_UNTIL, thin: dict | None = None) -> Bars:
    """Keep one bar a day before `sparse_until`, and the first `thin[day]` bars of each listed
    server day."""
    thin = THIN if thin is None else thin
    days = server_days(bars.time)
    unique, first, inverse = np.unique(days, return_index=True, return_inverse=True)
    rank = np.arange(len(days)) - first[inverse]
    limits = [1 if day < sparse_until else thin.get(day, len(bars)) for day in as_dates(unique)]
    return take(bars, rank < np.array(limits)[inverse])


def planted_m15() -> Bars:
    return plant(PLANTED.fetch("GOLD", Timeframe.M15))


def weekdays(start: date, end: date) -> list[date]:
    days = (start + timedelta(days=i) for i in range((end - start).days))
    return [day for day in days if day.weekday() < 5]


def m15_with(counts: dict[date, int]) -> Bars:
    """Flat M15 bars: `counts[day]` of them from the start of each server day."""
    times = []
    for day, count in sorted(counts.items()):
        number = (day - EPOCH).days
        midnight = number * DAY - int(server_offset(np.array([number * DAY]))[0])
        times.append(midnight + 900 * np.arange(count, dtype=np.int64))
    time = np.concatenate(times)
    flat = np.ones(len(time))
    return Bars("GOLD", Timeframe.M15, 0.01, 2, time, flat, flat, flat, flat, np.full(len(time), 9))


def test_the_mask_finds_the_planted_sparse_start_and_thin_days():
    m15 = planted_m15()
    days, counts = np.unique(server_days(m15.time), return_counts=True)
    by_day = dict(zip(as_dates(days), counts.tolist(), strict=True))
    assert by_day[date(2024, 1, 31)] == 1 and by_day[date(2024, 2, 1)] == 96  # the plant took

    mask = build_mask(m15, CUTOFF)

    assert mask.median == 96
    assert mask.first_usable_month == date(2024, 2, 1)
    assert mask.masked_days == MASKED  # not the 48-bar day, nor the sparse January
    assert mask.judged_through == date(2024, 5, 3)


def test_a_month_is_usable_once_its_median_day_reaches_half_the_median():
    counts = {day: 47 for day in weekdays(date(2024, 1, 1), date(2024, 2, 1))}
    counts |= {day: 48 for day in weekdays(date(2024, 2, 1), date(2024, 3, 1))}
    counts |= {day: 96 for day in weekdays(date(2024, 3, 1), date(2024, 7, 1))}

    mask = build_mask(m15_with(counts), datetime(2024, 7, 1, tzinfo=UTC))

    assert mask.median == 96
    assert mask.first_usable_month == date(2024, 2, 1)
    assert mask.masked_days == ()  # January's 47-bar days lie before the first usable month


def test_the_median_comes_from_the_research_window_alone():
    """Many thin Holdout days would halve a median taken over all days."""
    counts = {day: 96 for day in weekdays(date(2024, 11, 1), date(2025, 1, 1))}
    counts |= {day: 40 for day in weekdays(date(2025, 1, 1), date(2025, 6, 1))}

    mask = build_mask(m15_with(counts), datetime(2025, 6, 1, tzinfo=UTC))

    assert mask.median == 96
    assert mask.masked_days == tuple(weekdays(date(2025, 1, 1), date(2025, 6, 1)))


def test_only_closed_server_days_are_judged():
    """At 03:00 server time today has twelve bars; judged now, it would be thin for good."""
    counts = {day: 96 for day in weekdays(date(2024, 12, 2), date(2024, 12, 20))}
    today = date(2024, 12, 20)
    partial = m15_with(counts | {today: 12})
    cutoff = datetime(2024, 12, 20, 1, tzinfo=UTC)  # 03:00 server time on the 20th

    mask = build_mask(partial, cutoff)

    assert mask.judged_through == date(2024, 12, 19)
    assert mask.masked_days == ()
    assert not mask.keep(partial.time[-12:]).any()  # withheld until judged
    assert mask.keep(partial.time[:-12]).all()

    later = extend_mask(mask, m15_with(counts | {today: 96}), datetime(2024, 12, 21, tzinfo=UTC))

    assert later.judged_through == date(2024, 12, 20)
    assert later.masked_days == ()


def test_a_later_import_never_judges_an_old_day_again():
    research = {day: 96 for day in weekdays(date(2024, 10, 1), date(2025, 1, 1))}
    old_thin, old_full = date(2024, 11, 13), date(2024, 11, 14)
    mask = build_mask(m15_with(research | {old_thin: 40}), datetime(2025, 1, 1, tzinfo=UTC))
    assert mask.masked_days == (old_thin,)

    # The broker backfilled the thin day and lost most of another; new days arrived, two thin.
    new = {day: 96 for day in weekdays(date(2025, 1, 1), date(2025, 2, 1))}
    new |= {date(2025, 1, 15): 47, date(2025, 1, 16): 48, date(2025, 1, 29): 3}
    later = extend_mask(
        mask, m15_with(research | {old_full: 10} | new), datetime(2025, 2, 1, tzinfo=UTC)
    )

    assert later.median == mask.median
    assert later.first_usable_month == mask.first_usable_month
    assert later.masked_days == (old_thin, date(2025, 1, 15), date(2025, 1, 29))
    assert later.judged_through == date(2025, 1, 31)


def test_an_earlier_cutoff_changes_nothing():
    m15 = planted_m15()
    mask = build_mask(m15, CUTOFF)

    assert extend_mask(mask, m15, datetime(2024, 3, 1, tzinfo=UTC)) == mask
    assert extend_mask(mask, m15, CUTOFF) == mask


@pytest.mark.parametrize("timeframe", list(Timeframe))
def test_keep_drops_whole_server_days_on_every_timeframe(timeframe):
    """H4 and D1 bars open on server-clock boundaries, so a server day holds whole bars."""
    mask = build_mask(planted_m15(), CUTOFF)
    bars = PLANTED.fetch("GOLD", timeframe)

    kept = set(as_dates(server_days(bars.time[mask.keep(bars.time)])))
    every = set(as_dates(server_days(bars.time)))

    assert kept == {day for day in every if day >= SPARSE_UNTIL and day not in MASKED}


def test_the_mask_round_trips_through_json():
    mask = build_mask(planted_m15(), CUTOFF)

    assert Mask.from_json(mask.to_json()) == mask
    assert mask.to_json()["first_usable_month"] == "2024-02"
    assert mask.to_json()["masked_days"] == ["2024-03-13", "2024-04-17"]


def test_bad_inputs_are_refused():
    holdout_only = m15_with({day: 96 for day in weekdays(date(2025, 2, 3), date(2025, 2, 8))})
    with pytest.raises(ValueError, match="Research Window"):
        build_mask(holdout_only, datetime(2025, 3, 1, tzinfo=UTC))
    with pytest.raises(ValueError, match="M15"):
        build_mask(PLANTED.fetch("GOLD", Timeframe.H1), CUTOFF)
    with pytest.raises(ValueError, match="timezone-aware"):
        build_mask(planted_m15(), datetime(2024, 5, 4))


def test_server_days_of_sorted_times_match_a_lookup_per_time():
    rng = np.random.default_rng(3)
    start = int(datetime(2014, 1, 1, tzinfo=UTC).timestamp())
    sorted_times = np.sort(rng.integers(start, start + 13 * 365 * DAY, 20_000))

    expected = (sorted_times + server_offset(sorted_times)) // DAY

    assert np.array_equal(server_days(sorted_times), expected)
    assert np.array_equal(server_days(sorted_times[::-1]), expected[::-1])
