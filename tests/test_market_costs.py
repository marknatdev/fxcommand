"""M3 T3: the Spread Floor and swaps, measured from FakeMt5 into a CostSnapshot (ADR 0004)."""

import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pytest

from vibedesk.engine.costs import CostSnapshot, swap_nights
from vibedesk.market.bars import Timeframe
from vibedesk.market.costs import (
    CostError,
    current_cost,
    measure_cost,
    measure_floor,
    record_cost,
)
from vibedesk.market.fake_mt5 import FakeMt5
from vibedesk.market.synthetic import SyntheticBarSource, SyntheticConfig

# Saturday to Saturday: two whole trading weeks, across the European switch to daylight saving
# (Sunday 29 March 2026), so the server clock is UTC+2 at the start and UTC+3 at the end.
# In the mismatch week before the switch, Friday closes at 23:00 server time (1,380 minutes).
NOW = datetime(2026, 4, 4, 12, 0, tzinfo=UTC)
TICKS = (9 * 1440 + 1380) * 2  # 9 full days + 1 mismatch Friday, FakeMt5's 2 ticks each: 28,680

WINTER = 2 * 3600  # January: server clock = UTC + 2 h


@pytest.fixture(scope="module")
def source():
    no_gaps = SyntheticConfig(missing_bar_prob=0.0)
    return SyntheticBarSource(7, date(2026, 3, 16), date(2026, 4, 6), no_gaps)


def connected(api):
    assert api.initialize()
    return api


def server(*args) -> int:
    """UTC epoch seconds of a January 2026 server-clock time."""
    return int(datetime(*args, tzinfo=UTC).timestamp()) - WINTER


class BidsMissing(FakeMt5):
    """Every fourth tick has no bid (FakeMt5 itself only drops asks)."""

    def copy_ticks_range(self, symbol, date_from, date_to, flags):
        ticks = super().copy_ticks_range(symbol, date_from, date_to, flags)
        ticks["bid"][3::4] = 0.0
        return ticks


class Rollover(FakeMt5):
    """A symbol whose triple swap falls on `day` (FakeMt5 itself always says Wednesday)."""

    def __init__(self, source, day):
        super().__init__(source)
        self.day = day

    def symbol_info(self, symbol):
        info = super().symbol_info(symbol)
        return replace(info, swap_rollover3days=self.day)


class TicksFail(FakeMt5):
    def copy_ticks_range(self, symbol, date_from, date_to, flags):
        return None

    def last_error(self):
        return (-1, "Terminal: Call failed")


# The Spread Floor ------------------------------------------------------------------------------


def test_the_floor_is_the_median_of_whole_ticks_and_the_p90_is_kept(source):
    # Each block of ten ticks has these spreads, and its tenth tick (the 100) has no ask.
    spreads = (40, 45, 50, 50, 55, 55, 55, 60, 70, 100)
    api = connected(FakeMt5(source, tick_spreads=spreads, missing_side_every=10))

    floor = measure_floor(api, "GOLD", NOW)

    # 2,880 blocks of 9 counted ticks. Sorted, by position: 40 [0, 2880), 45 [2880, 5760),
    # 50 [5760, 11520), 55 [11520, 20160), 60 [20160, 23040), 70 [23040, 25920).
    # The median sits at 12,959.5, inside the 55s; the 90th percentile at 0.9 × 25,919 = 23,327.1,
    # inside the 70s. Counting the ask-less ticks would give 28,800 ticks, a median of 52.5 and
    # a p90 of 61.
    assert floor.ticks == TICKS - TICKS // 10 == 25_812
    assert (floor.median_points, floor.p90_points) == (55.0, 70.0)


def test_a_tick_missing_its_bid_is_not_counted_either(source):
    # Spreads 50, 55, 55, 60 in turn: the ticks without a bid are exactly the 60s.
    floor = measure_floor(connected(BidsMissing(source)), "GOLD", NOW)

    assert floor.ticks == TICKS * 3 // 4 == 21_510
    assert (floor.median_points, floor.p90_points) == (55.0, 55.0)


def test_the_window_is_the_14_days_before_now_in_server_wall_time(source):
    api = connected(FakeMt5(source))

    measure_floor(api, "GOLD", NOW)
    bangkok = measure_floor(api, "GOLD", NOW.astimezone(ZoneInfo("Asia/Bangkok")))

    name, symbol, date_from, date_to, flags = api.calls[-1]
    assert (name, symbol, flags) == ("copy_ticks_range", "GOLD", api.COPY_TICKS_INFO)
    # European time: UTC+2 on 21 March, UTC+3 on 4 April.
    assert date_from == datetime(2026, 3, 21, 14, tzinfo=UTC)
    assert date_to == datetime(2026, 4, 4, 15, tzinfo=UTC)
    assert api.calls[-1] == api.calls[-3] and bangkok.ticks == TICKS
    with pytest.raises(ValueError, match="timezone-aware"):
        measure_floor(api, "GOLD", NOW.replace(tzinfo=None))


def test_a_window_without_whole_ticks_has_no_floor(source):
    before_the_data = datetime(2026, 2, 1, tzinfo=UTC)

    with pytest.raises(CostError, match="GOLD: no tick with both a bid and an ask"):
        measure_floor(connected(FakeMt5(source)), "GOLD", before_the_data)
    with pytest.raises(CostError, match="GOLD: no tick"):
        measure_floor(connected(FakeMt5(source, missing_side_every=1)), "GOLD", NOW)


# Swap -----------------------------------------------------------------------------------------


def test_a_swap_not_in_points_is_refused_by_symbol_and_mode(source):
    api = connected(FakeMt5(source, swap_mode=2))

    with pytest.raises(CostError, match=r"^GOLD: swap_mode 2 is not 1 \(points per lot\)"):
        measure_cost(api, "GOLD", NOW)
    assert not [call for call in api.calls if call[0] == "copy_ticks_range"]


@pytest.mark.parametrize("day, wednesday_night, weekend", [(3, 3, 1), (5, 1, 3)])
def test_the_triple_swap_day_comes_from_swap_rollover3days(source, day, wednesday_night, weekend):
    snapshot = measure_cost(connected(Rollover(source, day)), "GOLD", NOW).snapshot()

    assert snapshot.triple_swap_day == day
    # MT5's day is the one whose rollover counts three times: Wednesday's covers Wed → Thu,
    # Friday's covers the weekend.
    entries = np.array([server(2026, 1, 14, 10), server(2026, 1, 16, 10)])  # Wed, Fri
    exits = np.array([server(2026, 1, 15, 10), server(2026, 1, 19, 10)])  # Thu, Mon
    nights = swap_nights(entries, exits, snapshot.triple_swap_day)
    assert nights.tolist() == [wednesday_night, weekend]


def test_a_rollover_day_that_is_no_weekday_is_refused(source):
    with pytest.raises(CostError, match="GOLD: swap_rollover3days 7"):
        measure_cost(connected(Rollover(source, 7)), "GOLD", NOW)


# The store ------------------------------------------------------------------------------------


def test_current_cost_is_the_latest_line_and_older_lines_stay(source, tmp_path):
    first = measure_cost(connected(FakeMt5(source)), "GOLD", NOW)
    later = measure_cost(
        connected(FakeMt5(source, tick_spreads=(60, 65, 65), swaps={"GOLD": (-83.83, 20.64)})),
        "GOLD",
        NOW + timedelta(days=1),
    )

    path = record_cost(tmp_path, first)
    before = path.read_bytes()
    record_cost(tmp_path, later)
    after = path.read_bytes()

    assert path == tmp_path / "costs" / "GOLD.jsonl"
    assert after.startswith(before) and after.count(b"\n") == 2
    assert current_cost(tmp_path, "GOLD") == CostSnapshot(
        point=0.01,
        spread_floor_points=65.0,
        slippage_points=1.0,
        swap_long_points=-83.83,
        swap_short_points=0.0,  # XM pays +20.64 on shorts today; no credit is counted
        triple_swap_day=3,
        as_of="2026-04-05",
    )
    assert current_cost(tmp_path, "GOLD") != first.snapshot()
    assert json.loads(after.splitlines()[-1])["swap_short_points"] == 20.64  # the log keeps it


def test_the_snapshot_round_trips_as_the_ledger_stores_it(source, tmp_path):
    api = connected(FakeMt5(source, swaps={"GOLD": (-83.83, 20.64)}))
    record_cost(tmp_path, measure_cost(api, "GOLD", NOW))

    snapshot = current_cost(tmp_path, "GOLD")

    assert CostSnapshot.from_json(snapshot.to_json()) == snapshot
    assert (snapshot.spread_floor_points, snapshot.slippage_points) == (55.0, 1.0)
    assert snapshot.as_of == "2026-04-04"


def test_a_line_keeps_spreads_and_swaps_and_never_a_price(source, tmp_path):
    path = record_cost(tmp_path, measure_cost(connected(FakeMt5(source)), "GOLD", NOW))

    line = json.loads(path.read_text(encoding="utf-8"))

    assert set(line) == {
        "symbol", "measured_at", "point", "spread_floor_points", "spread_p90_points", "ticks",
        "swap_long_points", "swap_short_points", "triple_swap_day",
    }  # fmt: skip
    bars = source.fetch("GOLD", Timeframe.M1)
    low, high = bars.low.min(), bars.high.max() + 1.0  # an ask sits at most 60 points over
    numbers = [value for value in line.values() if isinstance(value, int | float)]
    assert not [value for value in numbers if low <= value <= high]


def test_nothing_measured_yet_is_an_error_naming_the_symbol(tmp_path):
    with pytest.raises(CostError, match="GOLD: no cost measured yet"):
        current_cost(tmp_path, "GOLD")


# MT5 failures ---------------------------------------------------------------------------------


def test_an_mt5_failure_names_the_symbol_and_the_terminal_error(source):
    with pytest.raises(CostError, match=r"GOLD: symbol_info failed: .*No IPC connection"):
        measure_cost(FakeMt5(source), "GOLD", NOW)  # never initialized
    with pytest.raises(CostError, match="GOLD: copy_ticks_range failed: .*Call failed"):
        measure_floor(connected(TicksFail(source)), "GOLD", NOW)


def test_a_symbol_outside_the_universe_never_reaches_a_file_path(tmp_path):
    # The symbol becomes part of a file name, so `../x` must not climb out of the costs folder.
    for symbol in ("../../outside", "GOLD/../../x", "DOGEUSD"):
        with pytest.raises(CostError, match="not a symbol Vibe Desk trades"):
            current_cost(tmp_path, symbol)
    assert not any(tmp_path.rglob("*.jsonl"))


@pytest.mark.parametrize(
    "swaps, charged",
    [((-83.83, 20.64), (-83.83, 0.0)), ((5.0, -7.5), (0.0, -7.5)), ((-1.0, -2.0), (-1.0, -2.0))],
)
def test_a_swap_credit_counts_as_zero_and_a_charge_in_full(source, swaps, charged):
    # Today's swap is charged on every night back to 2014; a credit could flatter a strategy
    # that only holds the side XM pays, so the Cost Model counts it as zero (ADR 0004).
    api = connected(FakeMt5(source, swaps={"GOLD": swaps}))

    snapshot = measure_cost(api, "GOLD", NOW).snapshot()

    assert (snapshot.swap_long_points, snapshot.swap_short_points) == charged
