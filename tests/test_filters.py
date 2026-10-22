"""Hand-checked filter Blocks, and entries combined with filters by AND."""

from datetime import UTC, datetime

import numpy as np
import pytest

from vibedesk.blocks.compile import compile_signals
from vibedesk.blocks.filters import (
    AdxParams,
    AtrRegimeParams,
    DayOfWeekParams,
    MaSlopeParams,
    SessionWindowParams,
    adx_filter,
    atr_regime,
    day_of_week,
    ma_slope,
    session_window,
)
from vibedesk.blocks.indicators import trailing_percentile
from vibedesk.blocks.spec import expand_grid, validate
from vibedesk.market.bars import Bars, Timeframe


def utc(*args) -> int:
    return int(datetime(*args, tzinfo=UTC).timestamp())


TUESDAY = utc(2026, 1, 13)


def bars_from(close, half_range=0.5, timeframe=Timeframe.M15, start=TUESDAY):
    close = np.asarray(close, dtype=float)
    n = len(close)
    return Bars(
        "GOLD", timeframe, 0.01, 2,
        time=start + timeframe.seconds * np.arange(n),
        open=close, high=close + half_range, low=close - half_range, close=close,
        spread=np.zeros(n),
    )  # fmt: skip


def hourly(start, hours):
    n = hours
    flat = np.full(n, 100.0)
    return Bars("GOLD", Timeframe.H1, 0.01, 2, start + 3600 * np.arange(n), flat, flat, flat, flat,
                np.zeros(n))  # fmt: skip


def test_ma_slope_on_a_steady_climb():
    # Closes rise 0.1 a bar with a range of 1.0, so ATR(14) = 1.0 (true range 1.0 each bar)
    # and the SMA rises 1.0 over 10 bars: a slope of exactly 1.0 ATR.
    bars = bars_from(100 + 0.1 * np.arange(60))

    allow_long, allow_short = ma_slope(bars, MaSlopeParams(period=20, min_slope_atr=0.5))
    steep_long, _ = ma_slope(bars, MaSlopeParams(period=20, min_slope_atr=1.5))
    _, falling_short = ma_slope(bars_from(100 - 0.1 * np.arange(60)), MaSlopeParams(period=20))

    defined = np.arange(60) >= 19 + 10  # SMA(20) needs 20 bars, the slope 10 more
    assert np.array_equal(allow_long, defined)
    assert not allow_short.any()
    assert not steep_long.any()
    assert np.array_equal(falling_short, defined)


def test_adx_is_high_in_a_clean_trend_and_low_in_chop():
    trend = bars_from(100 + np.arange(80.0))  # every bar higher: ADX = 100
    chop = bars_from(100 + np.tile([0.0, 1.0], 40))  # up, down, up, down: ADX near 0

    strong, _ = adx_filter(trend, AdxParams(period=10, min=25))
    calm, _ = adx_filter(chop, AdxParams(period=10, max=20))
    capped, _ = adx_filter(trend, AdxParams(period=10, max=50))

    assert strong[19:].all() and not strong[:19].any()  # defined from 2 * period - 1
    assert calm[19:].all()
    assert not capped.any()


def test_trailing_percentile_by_hand():
    values = [1, 2, 3, 4, 5, 3]

    rank = trailing_percentile(values, 3)

    # [1 2 3] -> 3 is above two, ties itself: (2 + 0.5) / 3. [4 5 3] -> 3 is lowest: 0.5 / 3.
    assert np.isnan(rank[:2]).all()
    assert rank[2:].tolist() == pytest.approx([250 / 3, 250 / 3, 250 / 3, 50 / 3])


def test_atr_regime_uses_only_past_bars_and_inclusive_bounds():
    # Ranges stay at 1.0 for 40 bars and then double: ATR rises, so its percentile jumps to
    # the top while a constant ATR sits at 50.
    close = np.full(70, 100.0)
    half = np.r_[np.full(40, 0.5), np.full(30, 1.0)]
    bars = Bars("GOLD", Timeframe.M15, 0.01, 2, utc(2026, 1, 13) + 900 * np.arange(70),
                close, close + half, close - half, close, np.zeros(70))  # fmt: skip

    middle, _ = atr_regime(bars, AtrRegimeParams(period=5, lookback=20, percentile_max=50))
    high, _ = atr_regime(bars, AtrRegimeParams(period=5, lookback=20, percentile_min=90))

    assert middle[23:40].all()  # flat ATR: exactly 50, and the bound is inclusive
    assert not middle[40:45].any()
    assert high[40:45].all()  # rising ATR is the highest value of its window
    assert not high[:40].any()


@pytest.mark.parametrize(
    "zone, day, utc_hours",
    [
        ("london", (2026, 1, 13), [8, 9, 10, 11]),
        ("london", (2026, 3, 17), [8, 9, 10, 11]),  # the UK is still on GMT
        ("london", (2026, 4, 7), [7, 8, 9, 10]),  # BST
        ("new_york", (2026, 3, 17), [12, 13, 14, 15]),  # EDT already
        ("new_york", (2026, 1, 13), [13, 14, 15, 16]),
    ],
)
def test_session_window_follows_local_time(zone, day, utc_hours):
    bars = hourly(utc(*day), 24)

    allowed, _ = session_window(bars, SessionWindowParams(zone=zone, start="08:00", end="12:00"))

    assert np.flatnonzero(allowed).tolist() == utc_hours


def test_session_window_can_wrap_past_midnight():
    bars = hourly(utc(2026, 1, 13), 24)  # Tokyo is UTC+9: 22:00-02:00 local = 13:00-17:00 UTC

    allowed, _ = session_window(bars, SessionWindowParams(zone="tokyo", start="22:00", end="02:00"))

    assert np.flatnonzero(allowed).tolist() == [13, 14, 15, 16]


def test_session_window_rejects_bad_times():
    with pytest.raises(ValueError):
        SessionWindowParams(start="8am")
    with pytest.raises(ValueError):
        SessionWindowParams(start="09:00", end="09:00")


def test_day_of_week_uses_server_days():
    # D1 bars open at server midnight, 22:00 UTC the evening before in winter.
    starts = [utc(2026, 1, d, 22) for d in (11, 12, 13, 14, 15)]  # server Mon .. Fri
    flat = np.full(5, 100.0)
    bars = Bars("GOLD", Timeframe.D1, 0.01, 2, starts, flat, flat, flat, flat, np.zeros(5))

    allowed, _ = day_of_week(bars, DayOfWeekParams(tue=False, fri=False))

    assert allowed.tolist() == [True, False, True, True, False]
    with pytest.raises(ValueError):
        DayOfWeekParams(mon=False, tue=False, wed=False, thu=False, fri=False)


WINDOW = {"start": "08:00", "end": "12:00"}


def test_entry_and_filter_combine_with_and():
    # Hourly closes that make a new high every bar from 06:00 to 13:00 UTC. Donchian(2) fires
    # long on every bar from index 2; the London 08:00-12:00 window keeps only 08:00-11:00.
    start = utc(2026, 1, 13, 6)
    close = 100 + np.arange(8.0)
    bars = Bars("GOLD", Timeframe.H1, 0.01, 2, start + 3600 * np.arange(8), close, close + 0.5,
                close - 0.5, close, np.zeros(8))  # fmt: skip
    spec = validate(
        {
            "name": "combined",
            "symbol": "GOLD",
            "timeframe": "H1",
            "direction": "long",
            "entry": {"block": "donchian_breakout", "params": {"period": 2}},
            "filters": [{"block": "session_window", "params": {"zone": "london"} | WINDOW}],
            "exit": {"stop": {"block": "atr_stop", "params": {"period": 2, "mult": 1.0}}},
            "sizing": {"block": "fixed_risk", "params": {}},
        }
    )
    [combination] = expand_grid(spec)

    signals = compile_signals(combination, bars)

    assert np.flatnonzero(signals.long_entry).tolist() == [2, 3, 4, 5]  # 08:00 .. 11:00 UTC
    assert not signals.short_entry.any()
