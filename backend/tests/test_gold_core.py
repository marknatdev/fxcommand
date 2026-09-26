"""Milestone 1 of spec-better-strategies.md: the GOLD strategies, the price-scaled cost model with swap,
next-tradable time, the Cost Check and multi-timeframe alignment. All pure."""

import numpy as np
import pandas as pd
import pytest

from fxcommand.learning.candidate import Candidate, generate, random_candidate
from fxcommand.learning.costs import (
    SWAP_CURRENCY_DEPOSIT,
    SWAP_INTEREST_CURRENT,
    SWAP_POINTS,
    CostModel,
    mt5_day_to_weekday,
    rollover_nights,
)
from fxcommand.risk import TradingHours, TradingWindow, estimate_cost, next_tradable
from fxcommand.strategies import STRATEGIES, get_strategy
from fxcommand.strategies import indicators as ind
from fxcommand.strategies.gold import next_entry_time
from fxcommand.strategies.mtf import align_closed

DAY = 86400
H = 3600
MON = 1_704_672_000  # 2024-01-08 00:00, a Monday (server time as epoch seconds)


def ts(day_offset: int, hour: int, minute: int = 0) -> int:
    return MON + day_offset * DAY + hour * H + minute * 60


# ------------------------------------------------------------------ bars helpers
def gold_h1_week(weeks: int = 3, seed: int = 1) -> pd.DataFrame:
    """GOLD-like H1 bars with XM's daily break: bars 01:00..23:00 Monday–Friday, nothing at 00:00 or at weekends."""
    rng = np.random.default_rng(seed)
    times = [ts(d + 7 * w, h) for w in range(weeks) for d in range(5) for h in range(1, 24)]
    close = 2000 + np.cumsum(rng.normal(0, 2, len(times)))
    open_ = np.r_[close[0], close[:-1]]
    return pd.DataFrame({"time": times, "open": open_, "high": np.maximum(open_, close) + 1, "low": np.minimum(open_, close) - 1, "close": close, "tick_volume": 100, "spread": 55})


def bars_from_closes(closes, start=MON, step=4 * H):
    closes = np.asarray(closes, dtype=float)
    opens = np.r_[closes[0], closes[:-1]]
    return pd.DataFrame({"time": start + np.arange(len(closes)) * step, "open": opens, "high": np.maximum(opens, closes) + 0.5, "low": np.minimum(opens, closes) - 0.5, "close": closes})


# ------------------------------------------------------------------ GOLD Trend
def reference_breakout(bars, entry, exit_, allow_short):
    """Loop reference: previous-N-bar channels, one decision per bar."""
    h, lo, c = bars["high"].to_numpy(), bars["low"].to_numpy(), bars["close"].to_numpy()
    rows = []
    for i in range(len(bars)):
        up = h[i - entry:i].max() if i >= entry else np.nan
        dn = lo[i - entry:i].min() if i >= entry else np.nan
        xup = h[i - exit_:i].max() if i >= exit_ else np.nan
        xdn = lo[i - exit_:i].min() if i >= exit_ else np.nan
        rows.append((c[i] > up, allow_short and c[i] < dn, c[i] < xdn, allow_short and c[i] > xup))
    return rows


@pytest.mark.parametrize("allow_short", [False, True])
def test_trend_breakout_matches_a_loop_reference(allow_short):
    rng = np.random.default_rng(3)
    bars = bars_from_closes(2000 + np.cumsum(rng.normal(0, 5, 600)))
    s = get_strategy("trend_breakout")
    p = s.resolve({"entry": 30, "exit": 12, "allow_short": allow_short})
    f = s.signals(bars, p)
    ref = reference_breakout(bars, 30, 12, allow_short)
    need = s.lookback(p)
    for i in range(need, len(bars)):
        got = tuple(bool(f.iloc[i][c]) for c in ("long", "short", "exit_long", "exit_short"))
        assert got == tuple(bool(x) for x in ref[i]), i
    assert f["long"].sum() > 3 and (f["short"].sum() > 3) == allow_short
    atr = ind.atr(bars, p["atr_period"])
    assert f["sl_dist"].iloc[-1] == pytest.approx(2.0 * atr.iloc[-1]) and f["tp_dist"].iloc[-1] == 0


def test_trend_breakout_defaults_are_the_researched_ones():
    p = get_strategy("trend_breakout").resolve({})
    assert (p["entry"], p["exit"], p["atr_period"], p["sl_atr"], p["tp_atr"], p["allow_short"]) == (55, 20, 20, 2.0, 0.0, False)


def test_trend_breakout_run_explains_and_exits():
    closes = [2000.0] * 80 + [2030.0] + [2031.0] * 5 + [1950.0]
    bars = bars_from_closes(closes)
    s = get_strategy("trend_breakout")
    sig = s.run(bars.iloc[:81], {})
    assert sig.action == "long" and "55-bar high" in sig.reason and sig.sl_dist > 0
    assert s.run(bars, {}, position="long").action == "exit"
    assert s.run(bars, {}).action == "none"  # long-only: the breakdown is not a short


# ------------------------------------------------------------ GOLD Reopen Drift
def test_next_entry_time_skips_the_weekend():
    closes = np.array([ts(0, 0), ts(0, 1), ts(0, 2), ts(4, 23), ts(5, 0), ts(6, 12)])
    got = next_entry_time(closes, 1)
    assert list(got) == [ts(0, 1), ts(0, 1), ts(1, 1), ts(7, 1), ts(7, 1), ts(7, 1)]


def test_session_drift_buys_every_reopen_and_exits_at_the_exit_hour():
    bars = gold_h1_week()
    s = get_strategy("session_drift")
    f = s.signals(bars, {})
    t = bars["time"]
    signal_bars = t[f["long"]]
    # the signal bar is the 23:00 bar (it closes into the 00:00–01:00 break); the next bar opens at 01:00
    assert all(((x % DAY) // H) == 23 for x in signal_bars)
    nxt = [t.iloc[i + 1] for i in np.flatnonzero(f["long"].to_numpy()) if i + 1 < len(t)]
    assert all(((x % DAY) // H) == 1 for x in nxt)
    weekdays = sorted({((x // DAY) + 3) % 7 for x in nxt})
    assert weekdays == [0, 1, 2, 3, 4]  # Monday is included: the Friday 23:00 bar leads into Monday 01:00
    # Friday's 23:00 bar leads to Monday, never to a Saturday entry
    fri_23 = [i for i in np.flatnonzero(f["long"].to_numpy()) if ((t.iloc[i] // DAY) + 3) % 7 == 4]
    assert all(f["info_entry_at"].iloc[i] == t.iloc[i] - 4 * DAY - 23 * H + 7 * DAY + H for i in fri_23)
    # exits: bars whose close is at or after 04:00; the 03:00 bar closes at 04:00
    ex = f["exit_long"].to_numpy()
    for i in range(s.lookback(s.resolve({})) - 1, len(t)):  # nothing is signalled during warm-up
        close_min = ((t.iloc[i] + H) % DAY) // 60
        assert ex[i] == (close_min >= 240)
    assert not f["short"].any()
    assert s.fill_window_s == 300


def test_session_drift_on_m15_fires_once_per_reopen():
    rows = []
    for d in range(5):
        for m in range(60, 1440, 15):
            rows.append(ts(d, 0, m))
    close = 2000 + np.cumsum(np.random.default_rng(2).normal(0, 1, len(rows)))
    bars = pd.DataFrame({"time": rows, "open": close, "high": close + 1, "low": close - 1, "close": close})
    f = get_strategy("session_drift").signals(bars, {})
    fired = bars["time"][f["long"]]
    assert all(((x % DAY) // 60) == 23 * 60 + 45 for x in fired)  # the last bar before the break
    assert len(fired) == 5  # Mon..Thu lead to Tue..Fri; Friday's leads to next Monday


def test_session_drift_backtest_holds_three_hours_and_never_overnight():
    from fxcommand.learning.paper import Costs, ExitRules, PaperTrader

    bars = gold_h1_week()
    s = get_strategy("session_drift")
    f = s.signals(bars, {})
    tr = PaperTrader(Costs(spread=0.57, slippage=0.05), ExitRules(reverse_on_opposite=False))
    trades = []
    for i in range(len(bars)):
        row = f.iloc[i]
        sig = {k: row[k] for k in ("long", "short", "exit_long", "exit_short", "sl_dist", "tp_dist")}
        b = bars.iloc[i]
        _, closed = tr.on_bar(int(b.time), b.open, b.high, b.low, b.close, float(row["info_atr"]), sig)
        trades += closed
    assert len(trades) >= 10
    for x in trades:
        assert (x.open_time % DAY) // H == 1
        assert x.close_time // DAY == x.open_time // DAY  # flat the same server day
        if x.reason == "exit":
            assert (x.close_time % DAY) // H == 4


# ------------------------------------------------------------------ Learning search
def test_gold_candidates_stay_in_their_family_and_search_ranges():
    rng = np.random.default_rng(0)
    champ = Candidate.of("trend_breakout", {})
    cands = generate(champ, 80, rng)
    assert {c.strategy for c in cands} <= {"trend_breakout", "session_drift"}
    for c in cands:
        p = c.param_dict
        if c.strategy == "trend_breakout":
            assert 40 <= p["entry"] <= 100 and 10 <= p["exit"] <= 50 and p["exit"] < p["entry"] and 2.0 <= p["sl_atr"] <= 3.0
        else:
            assert p["entry_hour"] == 1 and 3 <= p["exit_hour"] <= 5 and 1.0 <= p["sl_atr"] <= 2.0
    classic = generate(Candidate.of("ema_cross", {}), 80, np.random.default_rng(0))
    assert {c.strategy for c in classic} <= {"ema_cross", "donchian_breakout", "rsi_reversion"}
    r = random_candidate("session_drift", np.random.default_rng(1)).param_dict
    assert r["entry_hour"] == 1 and r["atr_period"] == 24 and r["tp_atr"] == 0


# ------------------------------------------------------------------ cost model
def test_rollover_nights_count_the_triple_day():
    assert rollover_nights(ts(0, 1), ts(0, 4)) == 0  # intraday
    assert rollover_nights(ts(0, 22), ts(1, 2)) == 1  # Monday night
    assert rollover_nights(ts(2, 22), ts(3, 2)) == 3  # Wednesday night is the triple
    assert rollover_nights(ts(0, 1), ts(7, 1)) == 7  # a full week: Mon, Tue, Wed×3, Thu, Fri
    assert rollover_nights(ts(4, 22), ts(7, 2)) == 1  # Friday night; the weekend was paid on Wednesday
    assert rollover_nights(ts(1, 22), ts(2, 2), triple_weekday=1) == 3
    assert mt5_day_to_weekday(3) == 2 and mt5_day_to_weekday(0) == 6  # MT5 Wednesday, Sunday


def test_gold_swap_in_points_scaled_by_price():
    m = CostModel(spread=0.57, slippage=0.05, swap_long=-86.84, swap_short=19.79, swap_mode=SWAP_POINTS, point=0.01, value_per_price=100, ref_price=4264)
    assert m.swap_per_night("long", 4264) == pytest.approx(-0.8684)
    assert m.swap_per_night("long", 2132) == pytest.approx(-0.4342)  # half the price, half the swap (D34)
    assert m.swap_per_night("short", 4264) == pytest.approx(0.1979)
    assert m.swap_for("long", 4264, ts(2, 22), ts(3, 2)) == pytest.approx(-0.8684 * 3)
    assert m.swap_for("long", 4264, ts(0, 1), ts(0, 4)) == 0
    assert m.spread_at(2132) == pytest.approx(0.285) and m.slippage_at(4264) == pytest.approx(0.05)


def test_money_and_interest_swap_modes():
    money = CostModel(spread=0.8, swap_long=-1.21, swap_mode=SWAP_CURRENCY_DEPOSIT, point=0.01, value_per_price=1.0)
    assert money.swap_per_night("long", 7700) == pytest.approx(-1.21)  # $ per lot per night / $1 per point per lot
    interest = CostModel(spread=0.0001, swap_long=-3.6, swap_mode=SWAP_INTEREST_CURRENT)
    assert interest.swap_per_night("long", 100.0) == pytest.approx(-0.01)  # 3.6%/yr of 100, one of 360 days
    assert CostModel(spread=0.1).swap_per_night("long", 1.0) == 0.0
    unscaled = CostModel(spread=0.57)
    assert unscaled.spread_at(1000) == 0.57  # no ref price: no scaling


def test_cost_model_from_symbol_info():
    class Info:
        point, trade_tick_size, trade_tick_value = 0.01, 0.01, 1.0
        swap_long, swap_short, swap_mode, swap_rollover3days = -86.84, 19.79, 1, 3

    m = CostModel.from_symbol(Info(), spread=0.57, ref_price=4264)
    assert m.slippage == pytest.approx(0.01) and m.value_per_price == pytest.approx(100) and m.triple_weekday == 2
    assert m.swap_per_night("long", 4264) == pytest.approx(-0.8684)


# ------------------------------------------------------------------ next tradable time
def test_next_tradable_moves_a_midnight_signal_to_the_reopen():
    gold = TradingHours.daily("01:00", "23:57")
    default_window = TradingWindow()  # Mon 00:10 – Fri 23:00, blackout 23:55–00:10
    assert next_tradable(ts(1, 0), default_window, gold) == ts(1, 1)  # Tuesday 00:00 close → 01:00
    assert next_tradable(ts(1, 10), default_window, gold) == ts(1, 10)  # already open
    assert next_tradable(ts(5, 0), default_window, gold) == ts(7, 1)  # Saturday → Monday 01:00
    assert next_tradable(ts(4, 23, 30), default_window, gold) == ts(7, 1)  # Friday after the window closes
    assert next_tradable(ts(1, 0)) == ts(1, 0)  # no hours, no window: now
    never = TradingHours(tuple(() for _ in range(7)))
    assert next_tradable(ts(1, 0), None, never, horizon_days=2) is None


def test_trading_hours_round_trip():
    gold = TradingHours.daily("01:00", "23:57")
    assert TradingHours.from_dict(gold.to_dict()) == gold
    assert gold.is_open(ts(2, 12)) and not gold.is_open(ts(2, 0, 30)) and not gold.is_open(ts(5, 12))


# ------------------------------------------------------------------ Cost Check
def test_cost_check_blocks_gold_m1_and_passes_the_gold_pair():
    m1 = estimate_cost(spread=0.57, slippage=0.05, stop_dist=1.5 * 1.8)  # M1 ATR ~ $1.8
    assert m1.blocked and m1.cost_r > 0.15
    reopen = estimate_cost(spread=0.70, slippage=0.05, stop_dist=1.5 * 16.5)
    assert not reopen.blocked and reopen.cost_r == pytest.approx(0.8 / 24.75, abs=1e-4)
    trend = estimate_cost(spread=0.57, slippage=0.05, stop_dist=2 * 31.4, swap=-0.87 * 10)
    assert not trend.blocked and trend.swap_r < -0.1 and "not blocking" in trend.reason
    assert estimate_cost(0.5, 0, 0).blocked


# ------------------------------------------------------------------ multi-timeframe alignment
def test_align_closed_never_uses_a_forming_higher_bar():
    h4 = pd.DataFrame({"time": [ts(0, 0), ts(0, 4), ts(0, 8)], "v": [1.0, 2.0, 3.0]})
    h1 = pd.DataFrame({"time": [ts(0, h) for h in range(0, 10)]})
    got = align_closed(h1["time"], H, h4["time"], 4 * H, h4["v"])
    # H1 bars closing 01:00..03:00 see nothing yet; the 03:00 bar closes at 04:00 with the first H4 bar
    assert np.isnan(got.iloc[0]) and np.isnan(got.iloc[2])
    assert list(got.iloc[3:10]) == [1.0, 1.0, 1.0, 1.0, 2.0, 2.0, 2.0]


def test_strategy_describe_reports_family_and_fill_window():
    d = STRATEGIES["session_drift"].describe()
    assert d["family"] == "gold" and d["fill_window_s"] == 300
    assert STRATEGIES["ema_cross"].describe()["family"] == "classic"
    assert any(p["search_min"] == 3 for p in d["params"])


# ------------------------------------------------------------------ golden pin
def test_gold_golden_fixture_reproduced():
    import json
    from pathlib import Path

    from tests.make_golden_gold import rows

    pinned = json.loads((Path(__file__).parent / "fixtures" / "golden_gold_signals.json").read_text())
    now = rows()
    assert len(now) == len(pinned)
    diff = [(a, b) for a, b in zip(pinned, now) if a["flags"] != b["flags"] or (a["sl"] is None) != (b["sl"] is None) or (a["sl"] is not None and abs(a["sl"] - b["sl"]) > 1e-7)]
    assert not diff, f"{len(diff)} rows differ, e.g. {diff[:2]}"
    assert sum(r["flags"][0] == "1" for r in pinned) > 50  # the fixture exercises entries
