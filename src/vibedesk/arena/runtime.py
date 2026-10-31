"""Forward Arena runtime scheduler job and bar processor."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np

from vibedesk.arena.controls import ArenaControls
from vibedesk.arena.strategy import ArenaStepResult, ArenaStrategy, StrategyStatus, TwinType

logger = logging.getLogger(__name__)


class ArenaRuntime:
    """Manages strategy execution on closed bars, gap detection and replay."""

    def __init__(
        self,
        root: Any,
        ledger: Any = None,
        api: Any | None = None,
    ) -> None:
        self.root = Path(root) if root is not None else Path(".")
        self.ledger = ledger
        self.api = api
        self.strategies: dict[int, ArenaStrategy] = {}
        self.controls = ArenaControls(ledger=ledger, runtime=self)
        self._stopped: bool = False

    def register_strategy(self, strategy: ArenaStrategy) -> None:
        self.strategies[strategy.id] = strategy

    def get_strategy(self, strategy_id: int) -> ArenaStrategy | None:
        return self.strategies.get(strategy_id)

    def list_strategies(self) -> list[ArenaStrategy]:
        return list(self.strategies.values())

    def stop(self) -> None:
        """Global emergency stop: pause all strategies."""
        self._stopped = True
        for s in self.strategies.values():
            s.status = StrategyStatus.PAUSED

    def resume(self) -> None:
        """Resume runtime execution and reactivate non-removed strategies."""
        self._stopped = False
        for s in self.strategies.values():
            if s.status != StrategyStatus.REMOVED:
                s.status = StrategyStatus.ACTIVE

    def send_to_arena(
        self,
        hypothesis_id: int,
        spec: Any | None = None,
        symbol: str | None = None,
        timeframe: str | None = None,
        twin_type: TwinType = TwinType.STANDARD,
        initial_balance: float = 10_000.0,
    ) -> ArenaStrategy:
        """Enroll an eligible Hypothesis (CANDIDATE or PASS) into the Forward Arena."""
        strategy_spec = spec
        strat_symbol = symbol
        strat_tf = timeframe

        if self.ledger is not None:
            # Check eligibility from trials
            trials = self.ledger.trials_for(hypothesis_id)
            has_candidate = any(t.verdict == "CANDIDATE" for t in trials)
            has_pass = False
            for t in trials:
                if hasattr(self.ledger, "holdout_spend"):
                    sp = self.ledger.holdout_spend(t.id)
                    if sp and getattr(sp, "verdict", None) == "PASS":
                        has_pass = True
                        break
            if not (has_candidate or has_pass) and strategy_spec is None:
                raise ValueError(
                    "Only CANDIDATE or Holdout PASS Hypotheses may enter the Forward Arena"
                )

            if strategy_spec is None:
                hyp = self.ledger.hypothesis(hypothesis_id)
                spec_dict = (
                    json.loads(hyp.spec_json) if isinstance(hyp.spec_json, str) else hyp.spec_json
                )
                from vibedesk.blocks.spec import validate as validate_spec

                strategy_spec = validate_spec(spec_dict)
                strat_symbol = hyp.symbol
                strat_tf = hyp.timeframe
        elif strategy_spec is None:
            raise ValueError("Ledger required to send hypothesis to Arena without explicit spec")

        if strat_symbol is None:
            strat_symbol = getattr(strategy_spec, "symbol", "GOLD")
        if strat_tf is None:
            strat_tf = getattr(strategy_spec, "timeframe", "H1")

        if self.ledger is not None and hasattr(self.ledger, "insert_arena_strategy"):
            strat_id = int(
                self.ledger.insert_arena_strategy(
                    hypothesis_id=hypothesis_id,
                    symbol=strat_symbol,
                    timeframe=strat_tf,
                    twin_type=twin_type,
                    initial_balance=initial_balance,
                )
            )
        else:
            strat_id = len(self.strategies) + 1

        strategy = ArenaStrategy(
            strategy_id=strat_id,
            hypothesis_id=hypothesis_id,
            spec=strategy_spec,
            symbol=strat_symbol,
            timeframe=strat_tf,
            twin_type=twin_type,
            status=StrategyStatus.ACTIVE,
            initial_balance=initial_balance,
            created_at=datetime.now(UTC).isoformat(),
        )
        self.register_strategy(strategy)
        self.controls._record_action(
            strategy_id=strat_id,
            action="enrolled",
            reason=f"Hypothesis #{hypothesis_id} enrolled in Forward Arena",
        )
        return strategy

    def is_stopped(self) -> bool:
        """Check if Arena execution is globally stopped."""
        if self.ledger is not None and hasattr(self.ledger, "arena_stopped"):
            return bool(self.ledger.arena_stopped())
        return self._stopped

    def process_closed_bar(
        self,
        symbol: str,
        timeframe: str,
        bar: dict[str, Any],
        live_spread: float | None = None,
    ) -> list[ArenaStepResult]:
        """Step all active strategies matching symbol and timeframe."""
        if self.is_stopped():
            return []

        results: list[ArenaStepResult] = []
        for s in self.strategies.values():
            if (
                s.symbol == symbol
                and s.timeframe == timeframe
                and s.status == StrategyStatus.ACTIVE
            ):
                try:
                    res = s.step(bar, live_spread=live_spread)
                    results.append(res)
                except Exception as e:
                    logger.exception("Error stepping strategy #%s: %s", s.id, e)
        return results

    def poll_closed_bars(
        self,
        root: Path | None = None,
        api: Any | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> list[ArenaStepResult]:
        """Poll MT5 for newly closed bars, append to cache, and step strategies (spec §2.1)."""
        if self._stopped:
            return []

        data_root = Path(root or self.root)
        mt5_api = api or self.api
        if mt5_api is None:
            try:
                from vibedesk.market.mt5_api import real_api

                mt5_api = real_api()
            except Exception:
                return []

        clock_fn = clock or (lambda: datetime.now(UTC))
        now_dt = clock_fn()
        now_dt = now_dt.replace(tzinfo=UTC) if now_dt.tzinfo is None else now_dt.astimezone(UTC)
        now_ts = int(now_dt.timestamp())

        active_pairs: set[tuple[str, str]] = set()
        for s in self.strategies.values():
            if s.status == StrategyStatus.ACTIVE:
                active_pairs.add((s.symbol, s.timeframe))

        if not active_pairs:
            return []

        all_results: list[ArenaStepResult] = []
        from vibedesk.market.bars import Timeframe
        from vibedesk.market.mt5 import Mt5BarSource

        source = Mt5BarSource(mt5_api, clock=clock_fn)

        for symbol, tf_str in active_pairs:
            tf = Timeframe(tf_str)
            span = max(tf.seconds * 3, 3600)
            start_dt = now_dt - timedelta(seconds=span)
            try:
                fetch_bars = source.fetch
                bars = fetch_bars(symbol, tf, start=start_dt, end=now_dt)
            except Exception as e:
                logger.warning("Failed to fetch live bars for %s %s: %s", symbol, tf_str, e)
                continue

            live_spread = None
            try:
                tick = mt5_api.symbol_info_tick(symbol)
                if tick is not None and getattr(tick, "bid", 0) > 0 and getattr(tick, "ask", 0) > 0:
                    info = mt5_api.symbol_info(symbol)
                    pt = getattr(info, "point", 0.01) if info else 0.01
                    if pt > 0:
                        live_spread = round((tick.ask - tick.bid) / pt)
            except Exception:
                live_spread = None

            # Closed bars only: bar_open_time + tf.seconds <= now_ts
            closed_mask = (bars.time + tf.seconds) <= now_ts
            if not np.any(closed_mask):
                continue

            # Persist newly closed bars to cache
            self._append_closed_bars_to_cache(data_root, symbol, tf, bars, closed_mask)

            # Step strategies in chronological order
            for idx in np.where(closed_mask)[0]:
                bar_dict = {
                    "time": int(bars.time[idx]),
                    "open": float(bars.open[idx]),
                    "high": float(bars.high[idx]),
                    "low": float(bars.low[idx]),
                    "close": float(bars.close[idx]),
                    "spread": int(bars.spread[idx]),
                }
                step_res = self.process_closed_bar(
                    symbol, tf_str, bar_dict, live_spread=live_spread
                )
                all_results.extend(step_res)

        return all_results

    def _append_closed_bars_to_cache(
        self,
        root: Path,
        symbol: str,
        timeframe: Any,
        bars: Any,
        closed_mask: np.ndarray,
    ) -> None:
        import vibedesk.market.cache as cache_module
        from vibedesk.market.bars import Bars
        from vibedesk.market.cache import CacheError, read_meta, write_chunk

        read_cache_unmasked = cache_module.read_unmasked

        try:
            closed_times = bars.time[closed_mask]
            years = np.unique([datetime.fromtimestamp(t, tz=UTC).year for t in closed_times])
            for year in years:
                y_start = int(datetime(year, 1, 1, tzinfo=UTC).timestamp())
                y_end = int(datetime(year + 1, 1, 1, tzinfo=UTC).timestamp())
                y_mask = closed_mask & (bars.time >= y_start) & (bars.time < y_end)
                if not np.any(y_mask):
                    continue

                try:
                    existing = read_cache_unmasked(
                        root,
                        symbol,
                        timeframe,
                        start=datetime(year, 1, 1, tzinfo=UTC),
                        end=datetime(year + 1, 1, 1, tzinfo=UTC),
                    )
                except (CacheError, Exception):
                    existing = None

                new_times = bars.time[y_mask]
                if existing is not None and len(existing) > 0:
                    add_mask = ~np.isin(new_times, existing.time)
                    if not np.any(add_mask):
                        continue
                    comb_times = np.concatenate([existing.time, new_times[add_mask]])
                    comb_open = np.concatenate([existing.open, bars.open[y_mask][add_mask]])
                    comb_high = np.concatenate([existing.high, bars.high[y_mask][add_mask]])
                    comb_low = np.concatenate([existing.low, bars.low[y_mask][add_mask]])
                    comb_close = np.concatenate([existing.close, bars.close[y_mask][add_mask]])
                    comb_spread = np.concatenate([existing.spread, bars.spread[y_mask][add_mask]])
                    order = np.argsort(comb_times)
                    combined_bars = Bars(
                        symbol=symbol,
                        timeframe=timeframe,
                        point=existing.point,
                        digits=existing.digits,
                        time=comb_times[order],
                        open=comb_open[order],
                        high=comb_high[order],
                        low=comb_low[order],
                        close=comb_close[order],
                        spread=comb_spread[order],
                    )
                    write_chunk(root, combined_bars, year)
                else:
                    try:
                        meta = read_meta(root, symbol)
                        point, digits = meta.point, meta.digits
                    except Exception:
                        point, digits = bars.point, bars.digits
                    order = np.argsort(new_times)
                    chunk_bars = Bars(
                        symbol=symbol,
                        timeframe=timeframe,
                        point=point,
                        digits=digits,
                        time=new_times[order],
                        open=bars.open[y_mask][order],
                        high=bars.high[y_mask][order],
                        low=bars.low[y_mask][order],
                        close=bars.close[y_mask][order],
                        spread=bars.spread[y_mask][order],
                    )
                    write_chunk(root, chunk_bars, year)
        except Exception as e:
            logger.debug(
                "Failed appending closed bars to cache for %s %s: %s", symbol, timeframe, e
            )

    def check_and_recover_gaps(self) -> int:
        """Query cache for missed bars since last step and replay via catch_up (spec §2.4)."""
        total_replayed = 0
        import vibedesk.market.cache as cache_module
        from vibedesk.market.bars import Timeframe
        from vibedesk.market.cache import CacheError

        read_cache_unmasked = cache_module.read_unmasked

        for strategy in self.strategies.values():
            if strategy.status == StrategyStatus.REMOVED:
                continue

            last_time = strategy.last_bar_time
            if last_time <= 0:
                continue

            tf = Timeframe(strategy.timeframe)
            start_dt = datetime.fromtimestamp(last_time + 1, tz=UTC)
            now_dt = datetime.now(UTC)

            try:
                cached_bars = read_cache_unmasked(
                    Path(self.root), strategy.symbol, tf, start=start_dt, end=now_dt
                )
            except (CacheError, Exception) as e:
                logger.debug("No cached bars to recover for strategy #%s: %s", strategy.id, e)
                continue

            if len(cached_bars) == 0:
                continue

            missed_dicts = [
                {
                    "time": int(cached_bars.time[i]),
                    "open": float(cached_bars.open[i]),
                    "high": float(cached_bars.high[i]),
                    "low": float(cached_bars.low[i]),
                    "close": float(cached_bars.close[i]),
                    "spread": int(cached_bars.spread[i]),
                }
                for i in range(len(cached_bars))
                if cached_bars.time[i] > last_time
            ]

            if not missed_dicts:
                continue

            replayed = strategy.catch_up(missed_dicts)
            total_replayed += replayed

            self.controls._record_action(
                strategy_id=strategy.id,
                action="catch_up",
                reason=f"replayed {replayed} missed bars from cache",
            )
            logger.info("Strategy #%s caught up %d missed bars", strategy.id, replayed)

        return total_replayed
