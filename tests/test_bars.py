from dataclasses import fields

import numpy as np
import pytest

from vibedesk.market.bars import Bars, Timeframe

ARRAYS = ("time", "open", "high", "low", "close", "spread")


def make_bars(**overrides):
    fields = dict(
        symbol="GOLD",
        timeframe=Timeframe.M15,
        point=0.01,
        digits=2,
        time=[0, 900, 1800],
        open=[100.0, 101.0, 102.0],
        high=[101.0, 102.0, 103.0],
        low=[99.0, 100.0, 101.0],
        close=[101.0, 102.0, 102.5],
        spread=[30, 35, 40],
    )
    fields.update(overrides)
    return Bars(**fields)


def test_bars_coerce_dtypes_and_report_length():
    bars = make_bars()

    assert len(bars) == 3
    assert bars.time.dtype == np.int64
    assert bars.close.dtype == np.float64
    assert bars.spread.dtype == np.int32


def test_bars_are_read_only_and_do_not_alias_the_input():
    source = np.array([100.0, 101.0, 102.0])
    bars = make_bars(open=source)

    source[0] = 0.0
    assert bars.open[0] == 100.0
    with pytest.raises(ValueError):
        bars.close[0] = 1.0


@pytest.mark.parametrize(
    "overrides, message",
    [
        (dict(close=[101.0, 102.0]), "length"),
        (dict(time=[0, 900, 900]), "increasing"),
        (dict(close=[101.0, 102.0, 103.5]), "high"),
        (dict(low=[99.0, 100.0, 102.1]), "low"),
        (dict(spread=[30, -1, 40]), "spread"),
        (dict(point=0.0), "point"),
        (dict(high=[101.0, np.nan, 103.0]), "high prices must be finite and positive"),
        (dict(low=[99.0, -np.inf, 101.0]), "low prices must be finite and positive"),
        (dict(open=[0.0, 101.0, 102.0], low=[0.0, 100.0, 101.0]), "open prices"),
        (dict(close=[101.0, 102.0, np.inf]), "close prices must be finite"),
    ],
)
def test_bars_reject_inconsistent_data(overrides, message):
    with pytest.raises(ValueError, match=message):
        make_bars(**overrides)


def test_between_is_half_open_on_time():
    bars = make_bars()

    window = bars.between(900, 1800)

    assert window.time.tolist() == [900]
    assert window.symbol == "GOLD"


def ten_bars():
    close = 100.0 + np.arange(10.0)
    return make_bars(
        time=np.arange(10) * 900,
        open=close - 0.5,
        high=close + 1.0,
        low=close - 1.0,
        close=close,
        spread=np.full(10, 30),
    )


def test_between_returns_read_only_views_of_the_bars():
    bars = ten_bars()

    window = bars.between(900, 5 * 900)
    inner = window.between(2 * 900, 3 * 900)

    assert len(window) == 4 and len(inner) == 1
    for name in ARRAYS:
        for part in (window, inner):
            assert np.shares_memory(getattr(part, name), getattr(bars, name))
            assert not getattr(part, name).flags.writeable
    with pytest.raises(ValueError):
        window.close[0] = 1.0


@pytest.mark.parametrize(
    "start, end", [(900, 5 * 900), (0, 10 * 900), (-(2**62), 2**62), (450, 451), (-1, 0)]
)
def test_between_equals_bars_built_by_the_copying_constructor(start, end):
    bars = ten_bars()
    keep = (bars.time >= start) & (bars.time < end)
    copied = Bars(
        bars.symbol,
        bars.timeframe,
        bars.point,
        bars.digits,
        *(getattr(bars, name)[keep] for name in ARRAYS),
    )

    window = bars.between(start, end)

    assert len(window) == len(copied)
    for f in fields(Bars):
        got, want = getattr(window, f.name), getattr(copied, f.name)
        if f.name in ARRAYS:
            assert got.dtype == want.dtype and np.array_equal(got, want)
        else:
            assert got == want


def test_timeframe_seconds():
    assert [tf.seconds for tf in Timeframe] == [60, 900, 3600, 14400, 86400]
