"""Hand-checked signal positions for each entry Block, and DST-correct session opens."""

from datetime import UTC, datetime

import numpy as np
import pytest

from vibedesk.blocks.entries import (
    BollingerTouchParams,
    MaCrossParams,
    PullbackToMaParams,
    RangeBreakoutParams,
    RsiThresholdParams,
    bollinger_touch,
    ma_cross,
    pullback_to_ma,
    range_breakout,
    rsi_threshold,
)
from vibedesk.market.bars import Bars, Timeframe
from vibedesk.market.sessions import session_opens


def series(closes, lows=None, timeframe=Timeframe.M15, start=1704067200):
    closes = np.asarray(closes, dtype=float)
    lows = closes if lows is None else np.asarray(lows, dtype=float)
    n = len(closes)
    return Bars(
        "GOLD", timeframe, 0.01, 2,
        time=start + timeframe.seconds * np.arange(n),
        open=closes, high=closes, low=lows, close=closes, spread=np.zeros(n),
    )  # fmt: skip


def where(signal):
    return np.flatnonzero(signal).tolist()


def test_ma_cross():
    # SMA(2): 5 5 5 5.5 6.5 7 6.5 5.5 4.5 from index 1; SMA(3): 5 5 5.33 6 6.67 6.67 6 5 from 2.
    bars = series([5, 5, 5, 5, 6, 7, 7, 6, 5, 4])

    long, short = ma_cross(bars, MaCrossParams(fast=2, slow=3))

    assert where(long) == [4]  # 5.5 > 5.33 after 5 <= 5
    assert where(short) == [7]  # 6.5 < 6.67 after 7 > 6.67


def test_ma_cross_needs_fast_below_slow():
    with pytest.raises(ValueError):
        MaCrossParams(fast=30, slow=30)


# RSI(2) of closes 10 11 12 11 10 9 10 11 (Wilder): -, -, 100, 50, 25, 12.5, 56.25, 78.125.
RSI_CLOSES = [10, 11, 12, 11, 10, 9, 10, 11]


def test_rsi_cross_up_buys_the_recovery_and_sells_the_fall_from_overbought():
    long, short = rsi_threshold(series(RSI_CLOSES), RsiThresholdParams(period=2, level=30))

    assert where(long) == [6]  # 12.5 -> 56.25 crosses above 30
    assert where(short) == [3]  # 100 -> 50 crosses below 70


def test_rsi_cross_down_buys_the_dip_as_it_happens():
    params = RsiThresholdParams(period=2, level=30, mode="cross_down")

    long, short = rsi_threshold(series(RSI_CLOSES), params)

    assert where(long) == [4]  # 50 -> 25 crosses below 30
    assert where(short) == [7]  # 56.25 -> 78.125 crosses above 70


# Bands with SMA(3) and 1 population stdev: index 4 lower 8.39, index 6 upper 11.22.
BAND_CLOSES = [10, 10, 10, 10, 8, 8, 12, 10]


def test_bollinger_fade():
    params = BollingerTouchParams(period=3, stdev=1.0, mode="fade")

    long, short = bollinger_touch(series(BAND_CLOSES), params)

    assert where(long) == [4]
    assert where(short) == [6]


def test_bollinger_break():
    params = BollingerTouchParams(period=3, stdev=1.0, mode="break")

    long, short = bollinger_touch(series(BAND_CLOSES), params)

    assert where(long) == [6]
    assert where(short) == [4]


def test_pullback_to_ma():
    # SMA(4) trend from index 3: 11.5 12.5 13.125 13.75 14.25; SMA(2) from 1: 10.5 11.5 12.5
    # 13.5 13.75 14.0 14.75. Bar 5's low (13.0) dips below SMA(2) = 13.75; bar 6 closes back
    # above SMA(2) = 14.0 and above the trend.
    closes = [10, 11, 12, 13, 14, 13.5, 14.5, 15]
    lows = [10, 11, 12, 13, 14, 13.0, 14.5, 15]
    params = PullbackToMaParams(trend_period=4, pullback_period=2)

    long, short = pullback_to_ma(series(closes, lows), params)

    assert where(long) == [6]
    assert where(short) == []


def utc(*args) -> int:
    return int(datetime(*args, tzinfo=UTC).timestamp())


@pytest.mark.parametrize(
    "session, day, hour",
    [
        ("london_open", (2026, 1, 13), 8),  # GMT
        ("london_open", (2026, 3, 17), 8),  # US has switched, the UK hasn't
        ("london_open", (2026, 4, 7), 7),  # BST
        ("london_open", (2026, 10, 27), 8),  # UK back on GMT, US not yet
        ("ny_open", (2026, 1, 13), 13),  # EST
        ("ny_open", (2026, 3, 17), 12),  # EDT
        ("ny_open", (2026, 10, 27), 12),  # still EDT
        ("ny_open", (2026, 11, 3), 13),  # EST again
        ("asia", (2026, 3, 17), 0),  # Tokyo has no daylight saving
    ],
)
def test_session_opens_follow_each_zone_own_daylight_saving(session, day, hour):
    opens = session_opens(session, utc(*day), utc(*day) + 86400)

    assert utc(*day, hour) in opens.tolist()


def test_session_opens_skip_local_weekends():
    opens = session_opens("london_open", utc(2026, 1, 9), utc(2026, 1, 13))  # Fri .. Tue

    days = {datetime.fromtimestamp(t, UTC).strftime("%a") for t in opens}
    assert "Sat" not in days and "Sun" not in days


@pytest.mark.parametrize(
    "day, open_hour", [((2026, 1, 13), 8), ((2026, 3, 17), 8), ((2026, 4, 7), 7)]
)
def test_range_breakout_builds_the_range_from_the_london_open(day, open_hour):
    # M15 bars from an hour before the open. The pre-open hour is wild (highs of 110) and must
    # not be part of the range. The range is the open's first hour: high 101, low 99.
    start = utc(*day, open_hour - 1)
    pre_open = [(100, 110, 90, 100)] * 4
    opening_range = [(100, 101, 99, 100)] * 4
    after = [
        (100, 100.5, 99.5, 100.5),  # inside
        (100.5, 101.5, 100.5, 101.5),  # first close above 101: long
        (101.5, 102.0, 101.5, 102.0),  # higher again: no second long
        (102.0, 102.0, 100.0, 100.0),
        (100.0, 100.0, 98.5, 98.5),  # first close below 99: short
        (98.5, 98.5, 98.0, 98.0),
    ]
    o, h, low, c = zip(*(pre_open + opening_range + after), strict=True)
    n = len(o)
    bars = Bars("GOLD", Timeframe.M15, 0.01, 2, start + 900 * np.arange(n), o, h, low, c, [0] * n)

    long, short = range_breakout(bars, RangeBreakoutParams(session="london_open", range_bars=4))

    assert where(long) == [9]
    assert where(short) == [12]
