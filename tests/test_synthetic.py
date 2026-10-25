import hashlib
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import numpy as np
import pytest

from vibedesk.market.bars import Timeframe
from vibedesk.market.server_time import server_offset
from vibedesk.market.synthetic import PLANTED_EDGE, PROFILES, SyntheticBarSource, SyntheticConfig

# Two weeks around the US daylight-saving switch on 2026-03-08.
START, END = date(2026, 3, 2), date(2026, 3, 16)
NEW_YORK = ZoneInfo("America/New_York")

# sha256 over every M1 array for seed 7 from START to END, taken before the planted edge existed.
PRE_EDGE_DIGESTS = {
    "GOLD": "298c0710010947c7c9f36632fe2f10ccdf469d3b81a5c5da55e26c467591448a",
    "EURUSD": "a7a247adf904a9018b3e75a64afa7cfc7dd152662d017c84f1c746a2c01160c5",
}


# Every structure test runs on the random walk and on the planted edge: the edge moves prices,
# never the shape of the bars.
@pytest.fixture(scope="module", params=[SyntheticConfig(), PLANTED_EDGE], ids=["walk", "edge"])
def config(request):
    return request.param


@pytest.fixture(scope="module")
def source(config):
    return SyntheticBarSource(seed=7, start=START, end=END, config=config)


def _digest(bars):
    digest = hashlib.sha256()
    for name in ("time", "open", "high", "low", "close", "spread"):
        digest.update(getattr(bars, name).tobytes())
    return digest.hexdigest()


@pytest.mark.parametrize("symbol", ["GOLD", "EURUSD"])
def test_edge_off_reproduces_the_bars_from_before_the_edge(symbol):
    bars = SyntheticBarSource(seed=7, start=START, end=END).fetch(symbol, Timeframe.M1)

    assert _digest(bars) == PRE_EDGE_DIGESTS[symbol]


def test_edge_moves_prices_but_no_other_draw():
    walk = SyntheticBarSource(seed=7, start=START, end=END).fetch("GOLD", Timeframe.M1)
    edge = SyntheticBarSource(seed=7, start=START, end=END, config=PLANTED_EDGE).fetch(
        "GOLD", Timeframe.M1
    )

    assert np.array_equal(walk.time, edge.time)  # the same missing minutes
    assert np.array_equal(walk.spread, edge.spread)  # the same spreads and spikes
    assert not np.array_equal(walk.close, edge.close)


def test_same_seed_gives_identical_bars_and_another_seed_differs(source, config):
    again = SyntheticBarSource(seed=7, start=START, end=END, config=config)
    other = SyntheticBarSource(seed=8, start=START, end=END, config=config)
    bars = source.fetch("GOLD", Timeframe.M15)
    again, other = again.fetch("GOLD", Timeframe.M15), other.fetch("GOLD", Timeframe.M15)

    assert np.array_equal(bars.close, again.close)
    assert np.array_equal(bars.spread, again.spread)
    assert not np.array_equal(bars.close[:100], other.close[:100])


@pytest.mark.parametrize("timeframe", [Timeframe.M15, Timeframe.H1, Timeframe.H4, Timeframe.D1])
def test_higher_timeframes_aggregate_m1_exactly(source, timeframe):
    m1 = source.fetch("GOLD", Timeframe.M1)
    bars = source.fetch("GOLD", timeframe)
    bounds = np.searchsorted(m1.time, np.append(bars.time, m1.time[-1] + 1))

    for i in range(len(bars)):
        lo, hi = bounds[i], bounds[i + 1]
        assert hi > lo
        assert bars.open[i] == m1.open[lo]
        assert bars.close[i] == m1.close[hi - 1]
        assert bars.high[i] == m1.high[lo:hi].max()
        assert bars.low[i] == m1.low[lo:hi].min()
        assert bars.spread[i] == m1.spread[lo:hi].min()  # MT5 stores the minimum spread


def test_bar_boundaries_follow_the_server_clock(source):
    h4 = source.fetch("GOLD", Timeframe.H4)
    d1 = source.fetch("GOLD", Timeframe.D1)

    server_hours = ((h4.time + server_offset(h4.time)) // 3600) % 24
    assert set(server_hours.tolist()) <= {0, 4, 8, 12, 16, 20}

    ny_opens = {
        datetime.fromtimestamp(t, UTC).astimezone(NEW_YORK).strftime("%H:%M") for t in d1.time
    }
    # 17:00 New York outside the mismatch weeks, 18:00 between the US and European switches.
    assert ny_opens == {"17:00", "18:00"}


def test_market_is_closed_at_weekends(source):
    m1 = source.fetch("GOLD", Timeframe.M1)
    server_days = (m1.time + server_offset(m1.time)) // 86400
    weekday = (server_days + 3) % 7  # 1970-01-01 was a Thursday

    assert weekday.max() <= 4
    # The range ends at Monday 00:00 UTC, already Monday morning on the server: two weekends.
    assert (np.diff(m1.time) > 47 * 3600).sum() == 2


def test_some_minutes_are_missing_inside_trading_days(source):
    m1 = source.fetch("GOLD", Timeframe.M1)
    gaps = np.diff(m1.time)

    assert ((gaps > 60) & (gaps < 3600)).sum() > 5


def test_spreads_have_spikes_and_prices_sit_on_the_point_grid(source):
    m1 = source.fetch("GOLD", Timeframe.M1)
    profile = PROFILES["GOLD"]

    assert m1.spread.max() >= profile.spike_points
    assert m1.spread.min() >= 1
    ticks = m1.close / profile.point
    assert np.allclose(ticks, np.round(ticks))


def test_fetch_filters_the_window(source):
    start = datetime(2026, 3, 10, tzinfo=UTC)
    end = datetime(2026, 3, 11, tzinfo=UTC)

    bars = source.fetch("EURUSD", Timeframe.H1, start, end)

    assert bars.symbol == "EURUSD"
    assert bars.time.min() >= start.timestamp()
    assert bars.time.max() < end.timestamp()
    assert bars.point == PROFILES["EURUSD"].point


def test_unknown_symbol_is_rejected(source):
    with pytest.raises(KeyError):
        source.fetch("DOGEUSD", Timeframe.M15)
