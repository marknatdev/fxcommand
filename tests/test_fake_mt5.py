"""FakeMt5 keeps the contract of the real terminal (market/mt5_api.py), quirks included."""

import inspect
from datetime import UTC, date, datetime, timedelta

import numpy as np
import pytest

from vibedesk.market.bars import Timeframe
from vibedesk.market.fake_mt5 import NO_IPC, FakeMt5
from vibedesk.market.mt5_api import (
    ALLOWED,
    ALLOWED_CALLS,
    RATE_DTYPE,
    SWAP_IN_POINTS,
    TICK_DTYPE,
    Mt5Api,
)
from vibedesk.market.server_time import server_offset
from vibedesk.market.synthetic import SyntheticBarSource

# Three weeks over the US switch to daylight saving (Sunday 8 March 2026).
START, END = date(2026, 2, 23), date(2026, 3, 16)
REAL_CONSTANTS = {
    "TIMEFRAME_M1": 1, "TIMEFRAME_M15": 15, "TIMEFRAME_H1": 16385, "TIMEFRAME_H4": 16388,
    "TIMEFRAME_D1": 16408, "COPY_TICKS_ALL": -1, "COPY_TICKS_INFO": 1,
}  # fmt: skip


@pytest.fixture(scope="module")
def source():
    return SyntheticBarSource(4, START, END)


def connected(source, **kwargs):
    api = FakeMt5(source, **kwargs)
    assert api.initialize()
    return api


def wall(*args):
    """A bound as MT5 reads it: server wall time written as UTC."""
    return datetime(*args, tzinfo=UTC)


def server_times(bars):
    return bars.time + server_offset(bars.time)


def test_the_fake_has_every_allowed_name_and_the_real_constants(source):
    api = FakeMt5(source)

    assert {name for name in ALLOWED if hasattr(api, name)} == ALLOWED
    assert {name: getattr(api, name) for name in REAL_CONSTANTS} == REAL_CONSTANTS
    for name in ALLOWED_CALLS:  # the same parameters as the Protocol
        protocol = list(inspect.signature(getattr(Mt5Api, name)).parameters)[1:]
        assert list(inspect.signature(getattr(api, name)).parameters) == protocol, name


def test_nothing_is_served_before_initialize(source):
    api = FakeMt5(source)
    day = (wall(2026, 3, 2), wall(2026, 3, 3))

    assert api.copy_rates_range("GOLD", api.TIMEFRAME_M15, *day) is None
    assert api.symbol_info("GOLD") is None
    assert api.last_error() == NO_IPC
    assert not FakeMt5(source, fail_initialize=True).initialize()


def test_bars_come_in_server_wall_time_with_the_real_dtype(source):
    api = connected(source)

    rates = api.copy_rates_range("GOLD", api.TIMEFRAME_M15, wall(2026, 3, 2), wall(2026, 3, 13))

    bars = source.fetch("GOLD", Timeframe.M15)
    server = server_times(bars)
    expected = (server >= wall(2026, 3, 2).timestamp()) & (server <= wall(2026, 3, 13).timestamp())
    assert rates.dtype == RATE_DTYPE
    assert np.array_equal(rates["time"], server[expected])
    assert np.array_equal(rates["close"], bars.close[expected])
    assert np.array_equal(rates["spread"], bars.spread[expected])
    # The server runs New York + 7 h: UTC+2 before the US switch, UTC+3 after it.
    offsets = set((rates["time"] - bars.time[expected]).tolist())
    assert offsets == {2 * 3600, 3 * 3600}


def test_d1_bars_open_at_server_midnight(source):
    api = connected(source)

    rates = api.copy_rates_range("GOLD", api.TIMEFRAME_D1, wall(2026, 3, 2), wall(2026, 3, 13))

    assert len(rates) == 10
    assert set((rates["time"] % 86400).tolist()) == {0}


def test_bounds_must_be_utc_datetimes(source):
    api = connected(source)

    with pytest.raises(ValueError, match="server wall time"):
        api.copy_rates_range("GOLD", api.TIMEFRAME_H1, datetime(2026, 3, 2), wall(2026, 3, 3))


def test_an_unknown_symbol_or_timeframe_fails_like_mt5(source):
    api = connected(source)

    assert api.symbol_info("DOGEUSD") is None
    assert api.copy_rates_range("GOLD", 7, wall(2026, 3, 2), wall(2026, 3, 3)) is None
    assert api.last_error()[0] < 0


def test_a_downloading_terminal_returns_part_of_a_range_first(source):
    api = connected(source, settling_reads=1)
    args = ("GOLD", api.TIMEFRAME_M15, wall(2026, 3, 2), wall(2026, 3, 7))

    first, second, third = (api.copy_rates_range(*args) for _ in range(3))

    assert 0 < len(first) < len(second)
    assert np.array_equal(second, third)


def test_a_capped_terminal_returns_only_the_latest_bars(source):
    api = connected(source, max_bars=100)

    rates = api.copy_rates_range("GOLD", api.TIMEFRAME_M1, wall(2026, 3, 2), wall(2026, 3, 7))
    full = connected(source).copy_rates_range(
        "GOLD", api.TIMEFRAME_M1, wall(2026, 3, 2), wall(2026, 3, 7)
    )

    assert len(rates) == 100
    assert np.array_equal(rates, full[-100:])


def test_a_live_clock_hides_bars_that_have_not_opened(source):
    clock = [datetime(2026, 3, 4, 10, 7, tzinfo=UTC)]
    api = connected(source, now=lambda: clock[0])
    args = ("GOLD", api.TIMEFRAME_M15, wall(2026, 3, 4), wall(2026, 3, 5))

    before = api.copy_rates_range(*args)
    clock[0] += timedelta(minutes=15)
    after = api.copy_rates_range(*args)

    utc_last = before["time"][-1] - 2 * 3600  # winter: server is UTC+2
    assert utc_last == datetime(2026, 3, 4, 10, 0, tzinfo=UTC).timestamp()  # still forming
    assert len(after) == len(before) + 1


def test_ticks_carry_the_configured_spreads(source):
    api = connected(source, tick_spreads=(40, 60), missing_side_every=5)

    ticks = api.copy_ticks_range("GOLD", wall(2026, 3, 4, 12), wall(2026, 3, 4, 13), 1)

    assert ticks.dtype == TICK_DTYPE
    assert len(ticks) == 2 * 60 + 1  # every half minute from 12:00:00 to 13:00:00 inclusive
    whole = ticks["ask"] > 0
    spreads = np.round((ticks["ask"][whole] - ticks["bid"][whole]) / 0.01).astype(int)
    assert set(spreads.tolist()) == {40, 60}
    assert (~whole).sum() == len(ticks) // 5
    assert np.array_equal(ticks["time_msc"], ticks["time"] * 1000)


def test_symbol_info_has_the_fields_m3_reads(source):
    api = connected(source, swaps={"GOLD": (-83.83, 20.64)})

    info = api.symbol_info("GOLD")

    assert (info.point, info.digits) == (0.01, 2)
    assert (info.swap_long, info.swap_short) == (-83.83, 20.64)
    assert (info.swap_mode, info.swap_rollover3days) == (SWAP_IN_POINTS, 3)
    assert api.symbol_select("GOLD") and "GOLD" in api.selected


def test_the_tick_is_live_while_the_market_is_open_and_stale_at_the_weekend(source):
    open_ = datetime(2026, 3, 4, 10, 7, 30, tzinfo=UTC)  # a Wednesday
    weekend = datetime(2026, 3, 7, 12, 0, tzinfo=UTC)  # a Saturday

    live = connected(source, now=open_).symbol_info_tick("GOLD")
    stale = connected(source, now=weekend).symbol_info_tick("GOLD")

    assert live.time == open_.timestamp() + 2 * 3600
    friday_close = datetime(2026, 3, 6, 22, 0, tzinfo=UTC).timestamp() + 2 * 3600
    assert friday_close - 3600 < stale.time <= friday_close
