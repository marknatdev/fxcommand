import numpy as np
import pytest

from vibedesk.market.bars import Bars, Timeframe


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


def test_timeframe_seconds():
    assert [tf.seconds for tf in Timeframe] == [60, 900, 3600, 14400, 86400]
