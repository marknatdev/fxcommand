"""Arena strategy model and lifecycle definitions."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import numpy as np

from vibedesk.arena.account import PaperAccount, PaperTrade, Position
from vibedesk.arena.tracks import FillTrackKind, price_fill
from vibedesk.blocks.compile import compile_signals
from vibedesk.blocks.spec import Combination, StrategySpec, expand_grid
from vibedesk.engine.costs import swap_nights
from vibedesk.engine.simulate import _check
from vibedesk.market.bars import Bars, Timeframe
from vibedesk.market.server_time import server_weekdays
from vibedesk.market.sessions import local_minute_of_day
from vibedesk.market.synthetic import PROFILES

logger = logging.getLogger(__name__)

DEFAULT_SPREAD_FLOORS: dict[str, float] = {
    "GOLD": 55.0,
    "EURUSD": 20.0,
    "GBPUSD": 24.0,
    "USDJPY": 25.0,
    "AUDUSD": 24.0,
    "USDCAD": 30.0,
    "USDCHF": 26.0,
    "NZDUSD": 30.0,
}


def _minutes(hhmm: str) -> int:
    hours, minutes = hhmm.split(":")
    return int(hours) * 60 + int(minutes)


def _get_param(params: Any, key: str, default: Any = None) -> Any:
    if isinstance(params, dict):
        return params.get(key, default)
    return getattr(params, key, default)


def check_fill_bar_time_filters(spec: StrategySpec, bar_time: int) -> tuple[bool, str | None]:
    """Check if bar t+1 satisfies the Spec's time filters (CCC-71, ADR 0008)."""
    for filt in spec.filters:
        if filt.block == "session_window":
            zone = _get_param(filt.params, "zone", "london")
            start = _get_param(filt.params, "start", "08:00")
            end = _get_param(filt.params, "end", "16:00")
            minute = int(local_minute_of_day(np.array([bar_time], dtype=np.int64), zone)[0])
            s_min, e_min = _minutes(start), _minutes(end)
            if s_min < e_min:
                valid = (minute >= s_min) and (minute < e_min)
            else:
                valid = (minute >= s_min) or (minute < e_min)
            if not valid:
                return (
                    False,
                    f"session_window violated ({zone} minute {minute} not in [{start}, {end}))",
                )

        elif filt.block == "day_of_week":
            server_wd = int(server_weekdays(np.array([bar_time], dtype=np.int64))[0])
            allowed_days = [
                bool(_get_param(filt.params, "mon", True)),
                bool(_get_param(filt.params, "tue", True)),
                bool(_get_param(filt.params, "wed", True)),
                bool(_get_param(filt.params, "thu", True)),
                bool(_get_param(filt.params, "fri", True)),
                False,  # Saturday
                False,  # Sunday
            ]
            if not (0 <= server_wd < len(allowed_days) and allowed_days[server_wd]):
                return False, f"day_of_week violated (server weekday {server_wd})"

    return True, None


class StrategyStatus(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    REMOVED = "removed"


class TwinType(StrEnum):
    STANDARD = "standard"
    REGIME_GATED = "regime_gated"


@dataclass
class ArenaStepResult:
    """Outcome of stepping an Arena strategy on one closed bar."""

    strategy_id: int
    bar_time: int
    signals_evaluated: int
    trades_opened: int
    trades_closed: int
    backtest_net_r: float
    live_net_r: float


class ArenaStrategy:
    """A CANDIDATE strategy executing on live closed MT5 bars in the Forward Arena."""

    def __init__(
        self,
        strategy_id: int,
        hypothesis_id: int,
        spec: StrategySpec,
        symbol: str,
        timeframe: str,
        twin_type: TwinType = TwinType.STANDARD,
        status: StrategyStatus = StrategyStatus.ACTIVE,
        initial_balance: float = 10_000.0,
        created_at: str = "",
        spread_floor: float | None = None,
        point: float | None = None,
        digits: int | None = None,
        risk_fraction: float | None = None,
        cost_snapshot: Any | None = None,
        swap_long: float | None = None,
        swap_short: float | None = None,
        triple_day: int = 3,
    ) -> None:
        self.id = strategy_id
        self.hypothesis_id = hypothesis_id
        self.spec = spec
        self.symbol = symbol
        self.timeframe = timeframe
        self.twin_type = twin_type
        self.status = status
        self.initial_balance = initial_balance
        self.created_at = created_at

        prof = PROFILES.get(symbol)
        if cost_snapshot is not None:
            self.point = point if point is not None else float(cost_snapshot.point)
            self.digits = digits if digits is not None else (prof.digits if prof else 2)
            self.spread_floor = (
                spread_floor
                if spread_floor is not None
                else float(cost_snapshot.spread_floor_points)
            )
            self.slippage_points = float(getattr(cost_snapshot, "slippage_points", 1.0))
            self.swap_long_points = (
                swap_long if swap_long is not None else float(cost_snapshot.swap_long_points)
            )
            self.swap_short_points = (
                swap_short if swap_short is not None else float(cost_snapshot.swap_short_points)
            )
            self.triple_day = int(getattr(cost_snapshot, "triple_swap_day", 3))
        else:
            self.point = point if point is not None else (prof.point if prof else 0.01)
            self.digits = digits if digits is not None else (prof.digits if prof else 2)
            self.spread_floor = (
                spread_floor
                if spread_floor is not None
                else DEFAULT_SPREAD_FLOORS.get(symbol, 20.0)
            )
            self.slippage_points = 1.0
            self.swap_long_points = (
                swap_long if swap_long is not None else (-83.83 if symbol == "GOLD" else -5.0)
            )
            self.swap_short_points = (
                swap_short if swap_short is not None else (20.64 if symbol == "GOLD" else 1.0)
            )
            self.triple_day = triple_day

        if risk_fraction is not None:
            self.risk_fraction = risk_fraction
        else:
            try:
                risk_pct = float(_get_param(spec.sizing.params, "risk_pct", 1.0))
                self.risk_fraction = risk_pct / 100.0
            except Exception:
                self.risk_fraction = 0.01

        combinations = expand_grid(spec)
        self.combination: Combination = combinations[0]

        # Two fill tracks running in parallel
        self.account_backtest = PaperAccount(initial_balance=initial_balance)
        self.account_live = PaperAccount(initial_balance=initial_balance)

        self._bars_history: list[dict[str, Any]] = []
        self.last_bar_time: int = 0
        self.pending_signal: dict[str, Any] | None = None
        self._next_trade_id: int = 1

    def step(self, bar: dict[str, Any], live_spread: float | None = None) -> ArenaStepResult:
        """Evaluate strategy on newly closed bar t and execute fills at bar t+1."""
        bar_time = int(bar.get("time", 0))

        if self.status == StrategyStatus.REMOVED:
            return ArenaStepResult(
                strategy_id=self.id,
                bar_time=bar_time,
                signals_evaluated=0,
                trades_opened=0,
                trades_closed=0,
                backtest_net_r=self.account_backtest.net_r,
                live_net_r=self.account_live.net_r,
            )

        if bar_time <= self.last_bar_time and self.last_bar_time > 0:
            return ArenaStepResult(
                strategy_id=self.id,
                bar_time=bar_time,
                signals_evaluated=0,
                trades_opened=0,
                trades_closed=0,
                backtest_net_r=self.account_backtest.net_r,
                live_net_r=self.account_live.net_r,
            )

        if "open" not in bar and "close" not in bar:
            return ArenaStepResult(
                strategy_id=self.id,
                bar_time=bar_time,
                signals_evaluated=0,
                trades_opened=0,
                trades_closed=0,
                backtest_net_r=self.account_backtest.net_r,
                live_net_r=self.account_live.net_r,
            )

        open_ = float(bar.get("open", bar.get("close", 0.0)))
        high_ = float(bar.get("high", max(open_, float(bar.get("close", open_)))))
        low_ = float(bar.get("low", min(open_, float(bar.get("close", open_)))))
        close_ = float(bar.get("close", open_))
        bar_spread = float(bar.get("spread", 0.0))

        trades_opened = 0
        trades_closed = 0

        # Step 1: Check pending entry signal from previous bar t-1 to fill at this bar's open (t)
        if self.pending_signal is not None:
            if self.status == StrategyStatus.ACTIVE:
                # Time filter checked on fill bar (ADR 0008, CCC-71)
                ok, skip_reason = check_fill_bar_time_filters(self.spec, bar_time)
                if not ok:
                    logger.info(
                        "Fill skipped for strategy #%s at bar %s: %s",
                        self.id,
                        bar_time,
                        skip_reason,
                    )
                    self.pending_signal = None
                else:
                    dir_ = self.pending_signal["direction"]
                    stop_dist = self.pending_signal["stop_dist"]
                    target_dist = self.pending_signal["target_dist"]
                    sig_spread = self.pending_signal.get("signal_spread")
                    track_live_spread = (
                        sig_spread
                        if sig_spread is not None
                        else (live_spread if live_spread is not None else bar_spread)
                    )

                    # Execute fill for both tracks independently
                    for acct, track_kind in (
                        (self.account_backtest, FillTrackKind.BACKTEST_COST),
                        (self.account_live, FillTrackKind.LIVE_SPREAD),
                    ):
                        if acct.open_position is None:
                            pricing = price_fill(
                                track=track_kind,
                                bar_open=open_,
                                direction=dir_,
                                point=self.point,
                                spread_floor=self.spread_floor,
                                bar_spread=bar_spread,
                                live_spread=track_live_spread,
                                slippage_points=self.slippage_points,
                            )
                            risk_amt = acct.equity * self.risk_fraction
                            pos_size = risk_amt / stop_dist if stop_dist > 0 else 1.0
                            if dir_ == 1:
                                stop_p = pricing.fill_price - stop_dist
                                target_p = (
                                    pricing.fill_price + target_dist
                                    if target_dist is not None and not np.isnan(target_dist)
                                    else None
                                )
                            else:
                                stop_p = pricing.fill_price + stop_dist
                                target_p = (
                                    pricing.fill_price - target_dist
                                    if target_dist is not None and not np.isnan(target_dist)
                                    else None
                                )

                            acct.open_position = Position(
                                symbol=self.symbol,
                                direction=dir_,
                                entry_time=bar_time,
                                entry_price=pricing.fill_price,
                                size=pos_size,
                                stop_price=stop_p,
                                target_price=target_p,
                                stop_distance=stop_dist,
                                trail_price=None,
                                entry_cost=pricing.total_cost,
                                bars_held=0,
                                opposite_due=False,
                            )
                    trades_opened += 1
                    self.pending_signal = None
            else:
                self.pending_signal = None

        # Step 2: Check open positions during this bar
        closed_any = False
        trade_id = self._next_trade_id
        for acct, track_kind in (
            (self.account_backtest, FillTrackKind.BACKTEST_COST),
            (self.account_live, FillTrackKind.LIVE_SPREAD),
        ):
            if acct.open_position is not None:
                pos = acct.open_position
                if track_kind == FillTrackKind.BACKTEST_COST:
                    sp_pts = max(bar_spread, self.spread_floor)
                else:
                    sp_pts = live_spread if live_spread is not None else bar_spread
                sp_price = sp_pts * self.point
                slip_price = self.slippage_points * self.point

                at_open = 0
                time_bars = (
                    int(_get_param(self.combination.time.params, "bars", 0))
                    if self.combination.time
                    else 0
                )
                if time_bars > 0 and pos.bars_held >= time_bars:
                    at_open = 6  # _TIME
                elif pos.opposite_due:
                    at_open = 7  # _OPPOSITE

                exit_reason_code, fill_price, exit_cost_price, _ = _check(
                    pos.direction,
                    open_,
                    high_,
                    low_,
                    sp_price,
                    pos.stop_price,
                    pos.target_price if pos.target_price is not None else np.nan,
                    pos.trail_price if pos.trail_price is not None else np.nan,
                    at_open,
                    slip_price,
                )

                if exit_reason_code != 0:
                    reasons_map = {
                        1: "stop",
                        2: "stop_gap",
                        3: "target",
                        4: "end",
                        5: "trail",
                        6: "time",
                        7: "opposite",
                    }
                    reason_str = reasons_map.get(exit_reason_code, "stop")

                    nights = int(
                        swap_nights(
                            np.array([pos.entry_time], dtype=np.int64),
                            np.array([bar_time], dtype=np.int64),
                            triple_day=self.triple_day,
                        )[0]
                    )
                    swap_rate = (
                        self.swap_long_points if pos.direction == 1 else self.swap_short_points
                    )
                    effective_swap = min(0.0, swap_rate)
                    swap_r = (
                        (effective_swap * nights * self.point) / pos.stop_distance
                        if pos.stop_distance > 0
                        else 0.0
                    )

                    r_raw = (
                        pos.direction * (fill_price - pos.entry_price) / pos.stop_distance
                        if pos.stop_distance > 0
                        else 0.0
                    )
                    r_total = r_raw + swap_r
                    risk_amt = pos.size * pos.stop_distance
                    trade_pnl = risk_amt * r_total
                    cost_paid = (pos.entry_cost + exit_cost_price) * pos.size + abs(
                        effective_swap * nights * self.point * pos.size
                    )

                    trade = PaperTrade(
                        id=trade_id,
                        symbol=self.symbol,
                        direction=pos.direction,
                        entry_time=pos.entry_time,
                        exit_time=bar_time,
                        entry_price=pos.entry_price,
                        exit_price=fill_price,
                        size=pos.size,
                        pnl=trade_pnl,
                        r=r_total,
                        cost_paid=cost_paid,
                        exit_reason=reason_str,
                    )
                    acct.closed_trades.append(trade)
                    acct.balance += trade_pnl
                    acct.equity = acct.balance
                    acct.peak_equity = max(acct.peak_equity, acct.equity)
                    acct.open_position = None
                    closed_any = True
                else:
                    # Still open: update bars held and unrealized equity
                    new_bars_held = pos.bars_held + 1
                    # Unrealized exit
                    if pos.direction == 1:
                        unrealized_exit = close_ - slip_price
                    else:
                        unrealized_exit = close_ + sp_price + slip_price
                    unrealized_r = (
                        pos.direction * (unrealized_exit - pos.entry_price) / pos.stop_distance
                        if pos.stop_distance > 0
                        else 0.0
                    )
                    unrealized_pnl = (pos.size * pos.stop_distance) * unrealized_r
                    acct.equity = acct.balance + unrealized_pnl
                    acct.peak_equity = max(acct.peak_equity, acct.equity)
                    acct.open_position = Position(
                        symbol=pos.symbol,
                        direction=pos.direction,
                        entry_time=pos.entry_time,
                        entry_price=pos.entry_price,
                        size=pos.size,
                        stop_price=pos.stop_price,
                        target_price=pos.target_price,
                        stop_distance=pos.stop_distance,
                        trail_price=pos.trail_price,
                        entry_cost=pos.entry_cost,
                        bars_held=new_bars_held,
                        opposite_due=pos.opposite_due,
                    )

        if closed_any:
            trades_closed += 1
            self._next_trade_id += 1

        # Step 3: Append bar to history and evaluate signals at bar close
        self._bars_history.append(
            {
                "time": bar_time,
                "open": open_,
                "high": high_,
                "low": low_,
                "close": close_,
                "spread": int(bar_spread),
            }
        )
        if len(self._bars_history) > 5000:
            self._bars_history = self._bars_history[-5000:]

        signals_evaluated = 0
        if self.status == StrategyStatus.ACTIVE:
            signals_evaluated = 1
            bars_obj = self._build_bars_object()
            if bars_obj is not None and len(bars_obj) > 0:
                try:
                    signals = compile_signals(self.combination, bars_obj)
                    if self.account_backtest.open_position is None:
                        is_long = bool(signals.long_entry[-1])
                        is_short = bool(signals.short_entry[-1])
                        stop_d = float(signals.stop_dist[-1])
                        target_d = (
                            float(signals.target_dist[-1])
                            if not np.isnan(signals.target_dist[-1])
                            else None
                        )
                        trail_d = (
                            float(signals.trail_dist[-1])
                            if signals.trail_dist is not None
                            and not np.isnan(signals.trail_dist[-1])
                            else None
                        )

                        sig_sp = live_spread if live_spread is not None else bar_spread
                        if is_long and not is_short and stop_d > 0:
                            self.pending_signal = {
                                "direction": 1,
                                "stop_dist": stop_d,
                                "target_dist": target_d,
                                "trail_dist": trail_d,
                                "signal_time": bar_time,
                                "signal_spread": sig_sp,
                            }
                        elif is_short and not is_long and stop_d > 0:
                            self.pending_signal = {
                                "direction": -1,
                                "stop_dist": stop_d,
                                "target_dist": target_d,
                                "trail_dist": trail_d,
                                "signal_time": bar_time,
                                "signal_spread": sig_sp,
                            }
                        else:
                            self.pending_signal = None
                    else:
                        # Position is open: update trailing stops and opposite signals independently
                        for acct, track_kind in (
                            (self.account_backtest, FillTrackKind.BACKTEST_COST),
                            (self.account_live, FillTrackKind.LIVE_SPREAD),
                        ):
                            pos = acct.open_position
                            if pos is not None:
                                if signals.trail_dist is not None:
                                    trail_d = float(signals.trail_dist[-1])
                                    if not np.isnan(trail_d):
                                        if pos.direction == 1:
                                            level = close_ - trail_d
                                            if pos.trail_price is None or level > pos.trail_price:
                                                self._set_acct_trail(acct, level)
                                        else:
                                            sp_pts = (
                                                max(bar_spread, self.spread_floor)
                                                if track_kind == FillTrackKind.BACKTEST_COST
                                                else (
                                                    live_spread
                                                    if live_spread is not None
                                                    else bar_spread
                                                )
                                            )
                                            sp_price = sp_pts * self.point
                                            level = close_ + sp_price + trail_d
                                            if pos.trail_price is None or level < pos.trail_price:
                                                self._set_acct_trail(acct, level)

                                if (
                                    signals.exit_long is not None
                                    and signals.exit_short is not None
                                    and (
                                        (pos.direction == 1 and bool(signals.exit_long[-1]))
                                        or (pos.direction == -1 and bool(signals.exit_short[-1]))
                                    )
                                ):
                                    self._set_acct_opposite(acct)
                except Exception as e:
                    logger.debug("Signal compilation skipped: %s", e)

        self.last_bar_time = bar_time

        return ArenaStepResult(
            strategy_id=self.id,
            bar_time=bar_time,
            signals_evaluated=signals_evaluated,
            trades_opened=trades_opened,
            trades_closed=trades_closed,
            backtest_net_r=self.account_backtest.net_r,
            live_net_r=self.account_live.net_r,
        )

    def _set_acct_trail(self, acct: PaperAccount, level: float) -> None:
        if acct.open_position is not None:
            p = acct.open_position
            acct.open_position = Position(
                symbol=p.symbol,
                direction=p.direction,
                entry_time=p.entry_time,
                entry_price=p.entry_price,
                size=p.size,
                stop_price=p.stop_price,
                target_price=p.target_price,
                stop_distance=p.stop_distance,
                trail_price=level,
                entry_cost=p.entry_cost,
                bars_held=p.bars_held,
                opposite_due=p.opposite_due,
            )

    def _set_acct_opposite(self, acct: PaperAccount) -> None:
        if acct.open_position is not None:
            p = acct.open_position
            acct.open_position = Position(
                symbol=p.symbol,
                direction=p.direction,
                entry_time=p.entry_time,
                entry_price=p.entry_price,
                size=p.size,
                stop_price=p.stop_price,
                target_price=p.target_price,
                stop_distance=p.stop_distance,
                trail_price=p.trail_price,
                entry_cost=p.entry_cost,
                bars_held=p.bars_held,
                opposite_due=True,
            )

    def _update_trail_price(self, level: float) -> None:
        for acct in (self.account_backtest, self.account_live):
            self._set_acct_trail(acct, level)

    def _set_opposite_due(self) -> None:
        for acct in (self.account_backtest, self.account_live):
            self._set_acct_opposite(acct)

    def _build_bars_object(self) -> Bars | None:
        if not self._bars_history:
            return None
        try:
            time_arr = np.array([b["time"] for b in self._bars_history], dtype=np.int64)
            open_arr = np.array([b["open"] for b in self._bars_history], dtype=np.float64)
            high_arr = np.array([b["high"] for b in self._bars_history], dtype=np.float64)
            low_arr = np.array([b["low"] for b in self._bars_history], dtype=np.float64)
            close_arr = np.array([b["close"] for b in self._bars_history], dtype=np.float64)
            spread_arr = np.array([b["spread"] for b in self._bars_history], dtype=np.int32)

            tf = Timeframe(self.timeframe)
            return Bars(
                symbol=self.symbol,
                timeframe=tf,
                point=self.point,
                digits=self.digits,
                time=time_arr,
                open=open_arr,
                high=high_arr,
                low=low_arr,
                close=close_arr,
                spread=spread_arr,
            )
        except Exception:
            return None

    def catch_up(self, bars: list[dict[str, Any]]) -> int:
        """Replay missed closed bars in sequence upon host restart or reconnection.

        Returns the number of replayed bars processed.
        """
        replayed = 0
        for b in bars:
            self.step(b)
            replayed += 1
        logger.info("Strategy #%s completed catch_up of %d bars", self.id, replayed)
        return replayed
