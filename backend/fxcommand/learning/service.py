"""LearningService: the self-improvement loop (ADR 0004), behind a small interface the engine calls.

Engine hooks (all called inside the engine lock, all cheap, never place orders):
    bars_needed(a)                      -> how many closed bars on_bar needs
    on_bar(s, a, bars, tick, info, now) -> Shadow Trade the Champion and Challengers on new closed bars
    screen(s, a, signal, bars, tick)    -> Signal Filter verdict for a live entry
    signal_outcome(record_id, ...)      -> taken (ticket) / rejected by the Risk Gate
    on_live_close(trade)                -> label the Signal with its R; auto-rollback watch
    pending(session_id, symbol)         -> Promotion / Rollback waiting for the Assignment to be flat
    apply(change, s, a, now)            -> Candidate to switch to (engine updates the Assignment)
    on_session_started / on_session_stopped / on_new_day
Operator commands: request_optimize, promote, rollback, set_auto_promote. Read side: overview, arena.

CPU-heavy work (Optimizer Runs) runs on a dedicated learner thread with pure inputs and outputs;
service state is only mutated on the event loop. Bars for Optimizer Runs are fetched through the
engine's BrokerThread. Nothing here can send, modify or close an order.
"""

from __future__ import annotations

import asyncio
import logging
import time
from concurrent.futures import Executor, ProcessPoolExecutor, ThreadPoolExecutor
from dataclasses import dataclass, field

import pandas as pd

from ..broker import BrokerThread, SymbolInfo, Tick, Timeframe
from ..engine.schemas import DomainError
from ..journal import Journal
from ..risk import TradingWindow
from ..store import AssignmentRow, SessionRow, Store, TradeRow
from ..store.models import ChallengerRow, PendingChangeRow
from ..strategies import Signal, get_strategy
from ..strategies.base import WARMUP_BARS
from . import signal_filter as sf
from .candidate import Candidate
from .features import market_frame, vector
from .objective import RStats, guardrails, r_stats, should_rollback
from .optimizer import optimize_job
from .paper import Costs, ExitRules, PaperTrader
from .repo import LearningRepo

log = logging.getLogger("fxcommand.learning")

MAX_CHALLENGERS = 3
RETRAIN_EVERY = 10
RETENTION_DAYS = 180
FEATURE_BARS = 160  # market features need ~100 bars of ATR rank + EMA50 warm-up
AUTO_KINDS = ("auto_promotion", "auto_rollback")
ACTIVE = ("running", "paused")


def filter_key(symbol: str, timeframe: str, strategy: str) -> str:
    return f"{symbol}|{timeframe}|{strategy}"


@dataclass
class Screen:
    allowed: bool
    p_win: float | None
    mode: str
    record_id: int | None
    note: str = ""


@dataclass
class _Arena:
    symbol: str
    timeframe: str
    traders: dict[str, PaperTrader] = field(default_factory=dict)
    open_rows: dict[str, int] = field(default_factory=dict)  # candidate key -> open ShadowTradeRow id
    last_bar: int | None = None


class LearningService:
    def __init__(self, store: Store, broker: BrokerThread, journal: Journal, mode: str, processes: bool = False):
        self.store = store
        self.repo = LearningRepo(store)
        self.broker = broker
        self.journal = journal
        self.mode = mode
        self._arenas: dict[tuple[str, str], _Arena] = {}
        self._queue: asyncio.Queue | None = None
        self._queued: dict[tuple[str, str], int] = {}  # arena -> run id (queued or running)
        self._worker: asyncio.Task | None = None
        # Optimizer Runs are CPU-bound pure Python: in the app they run in a separate process so they
        # never compete with the engine for the GIL; tests may use a thread
        self._pool: Executor = ProcessPoolExecutor(max_workers=1) if processes else ThreadPoolExecutor(max_workers=1, thread_name_prefix="learner")
        self._server_time = 0
        self._known: set[str] = set()  # Candidate keys already stored
        self._filters: dict[str, sf.FilterState] = {}  # cache of learning_filters rows

    # =============================================================== lifecycle
    @property
    def enabled(self) -> bool:
        return bool(self.store.app_settings().get("learning_enabled", True))

    def start(self) -> None:
        """Called once the event loop runs. In sim the market is regenerated on every start, so open
        Shadow Trades from a previous process refer to prices that never existed: void them."""
        if self.mode == "sim":
            n = self.repo.void_open_shadows()
            if n:
                log.info("voided %d open Shadow Trades from a previous simulated market", n)
        for r in self.repo.runs(limit=200):
            if r.status in ("queued", "running"):
                self.repo.update_run(r.id, status="failed", note="interrupted by restart", finished_wall=time.time())
        self._queue = asyncio.Queue()
        self._worker = asyncio.create_task(self._work(), name="learner")

    async def stop(self) -> None:
        if self._worker:
            self._worker.cancel()
            try:
                await self._worker
            except (asyncio.CancelledError, Exception):
                pass
        self._pool.shutdown(wait=False, cancel_futures=True)

    def _j(self, kind: str, msg: str, s: SessionRow | None = None, symbol: str | None = None, **kw) -> None:
        self.journal.record(kind, msg, ts=self._server_time or int(time.time()), session_id=s.id if s else None, symbol=symbol, **kw)

    # ============================================================ engine hooks
    def bars_needed(self, a: AssignmentRow) -> int:
        need = FEATURE_BARS
        keys = [c.candidate_key for c in self.repo.challengers(a.symbol, a.timeframe)]
        for c in [Candidate.of(a.strategy, a.params), *self.repo.candidates(keys).values()]:
            s = get_strategy(c.strategy)
            need = max(need, s.lookback(s.resolve(c.param_dict)) + WARMUP_BARS)
        return need

    def _arena(self, symbol: str, timeframe: str) -> _Arena:
        key = (symbol, timeframe)
        if key not in self._arenas:
            arena = _Arena(symbol, timeframe)
            for row in self.repo.open_shadows(symbol, timeframe):  # MT5 mode: resume open Shadow Trades
                tr = PaperTrader(Costs(0.0))
                tr.restore(
                    {
                        "position": {
                            "side": row.side, "entry": row.entry, "sl": row.sl, "tp": row.tp, "risk": row.risk,
                            "open_time": row.open_ts, "signal_time": row.signal_ts, "features": row.features, "p_win": row.p_win,
                        }
                    }
                )
                arena.traders[row.candidate_key] = tr
                arena.open_rows[row.candidate_key] = row.id
            self._arenas[key] = arena
        return self._arenas[key]

    def _filter(self, symbol: str, timeframe: str, strategy: str) -> sf.FilterState:
        key = filter_key(symbol, timeframe, strategy)
        if key not in self._filters:
            row = self.repo.filter_state(key)
            self._filters[key] = sf.FilterState.from_dict(row.state if row else None)
        return self._filters[key]

    def _ensure(self, c: Candidate) -> None:
        if c.key not in self._known:
            self.repo.ensure_candidate(c)
            self._known.add(c.key)

    def champion_of(self, a: AssignmentRow) -> Candidate:
        return Candidate.of(a.strategy, a.params)

    def on_bar(self, s: SessionRow, a: AssignmentRow, bars: pd.DataFrame, tick: Tick, info: SymbolInfo, now: int, is_demo: bool) -> None:
        if not self.enabled or bars.empty:
            return
        self._server_time = now
        arena = self._arena(a.symbol, a.timeframe)
        bars = bars.reset_index(drop=True)
        times = bars["time"].to_numpy()
        # like the live engine, never act on the first bar seen: Shadow Trading starts at the next close
        if arena.last_bar is None:
            arena.last_bar = int(times[-1])
            return
        # bars not yet shadow-traded (the engine may skip bars between passes; replay them in order)
        new_idx = [i for i in range(len(bars)) if times[i] > arena.last_bar]
        if not new_idx:
            return
        arena.last_bar = int(times[-1])

        champion = self.champion_of(a)
        self._ensure(champion)
        challengers = self.repo.challengers(a.symbol, a.timeframe)
        cands = {champion.key: champion, **self.repo.candidates([c.candidate_key for c in challengers])}

        profile = self.store.to_profile(self.store.risk_profile(a.risk_profile_id))
        rules = ExitRules.from_profile(profile, a.reverse_on_opposite)
        window = TradingWindow.from_dict(s.window)
        tf_secs = Timeframe(a.timeframe).seconds
        spread = max(tick.ask - tick.bid, 0.0)
        spread_ok = tick.spread_points <= profile.max_spread_points
        costs = Costs(spread=spread, slippage=info.point)
        # market features are only needed when some Candidate schedules an entry: compute lazily, once,
        # on a short tail of the window (same index as ``bars``)
        _mf: list[pd.DataFrame] = []

        def mf_row(i: int) -> pd.Series:
            if not _mf:
                _mf.append(market_frame(bars.iloc[max(0, new_idx[0] - FEATURE_BARS) :]))
            return _mf[0].loc[i]

        o, h, l, c = (bars[k].to_numpy(dtype=float) for k in ("open", "high", "low", "close"))
        filters = {st: self._filter(a.symbol, a.timeframe, st) for st in {x.strategy for x in cands.values()}}
        touched: set[str] = set()

        for key, cand in cands.items():
            tr = arena.traders.get(key)
            if tr is None:
                tr = arena.traders[key] = PaperTrader(costs, rules)
            tr.costs, tr.rules = costs, rules
            tr.allow_entry = lambda ts: spread_ok and window.is_open(ts + tf_secs)
            frame = get_strategy(cand.strategy).signals(bars, cand.param_dict)
            cols = {k: frame[k].to_numpy() for k in ("long", "short", "exit_long", "exit_short", "sl_dist", "tp_dist")}
            atr = frame["info_atr"].to_numpy(dtype=float)
            fstate = filters[cand.strategy]
            for i in new_idx:

                def make_features(side, i=i, tr=tr, fstate=fstate):
                    vec = vector(mf_row(i), side, spread, tr.last_r)
                    return vec, sf.predict(fstate, vec)

                opened, closed = tr.on_bar(
                    int(times[i]), o[i], h[i], l[i], c[i], float(atr[i]) if atr[i] == atr[i] else 0.0,
                    {k: v[i] for k, v in cols.items()}, make_features,
                )
                for t in closed:
                    sid = arena.open_rows.pop(key, None)
                    if sid is not None:
                        self.repo.close_shadow(sid, close_ts=t.close_time, exit=t.exit, r=round(t.r, 5), reason=t.reason)
                        touched.add(cand.strategy)
                if opened is not None:
                    row = self.repo.add_shadow(
                        symbol=a.symbol, timeframe=a.timeframe, candidate_key=key, side=opened.side, signal_ts=opened.signal_time,
                        open_ts=opened.open_time, entry=opened.entry, sl=opened.sl, tp=opened.tp, risk=opened.risk,
                        features=opened.features, p_win=opened.p_win,
                    )
                    arena.open_rows[key] = row.id

        for strategy in touched:
            self._maybe_retrain(a.symbol, a.timeframe, strategy, champion)
        self._consider_auto_promotion(s, a, champion, is_demo)

    def _maybe_retrain(self, symbol: str, timeframe: str, strategy: str, champion: Candidate) -> None:
        key = filter_key(symbol, timeframe, strategy)
        row = self.repo.filter_state(key)
        samples = self.repo.shadow_samples(symbol, timeframe, strategy)
        trained_on = row.trained_on if row else 0
        prev = sf.FilterState.from_dict(row.state if row else None)
        if len(samples) - trained_on < RETRAIN_EVERY and row is not None:
            return
        state = sf.train(samples, prev)
        self._filters.pop(key, None)
        if strategy == champion.strategy:
            recent = [(r.p_win, r.r) for r in self.repo.shadow_closed(symbol, timeframe, champion.key)[-sf.DISABLE_WINDOW:] if r.p_win is not None]
            state = sf.review(state, recent)
        if state.mode != prev.mode:
            level = "warn" if state.mode == "disabled" else "info"
            self._j("learning", f"Signal Filter {symbol} {timeframe} {strategy}: {prev.mode} → {state.mode} ({state.note})", symbol=symbol, level=level)
        self.repo.save_filter(key, state.to_dict(), len(samples))

    # ------------------------------------------------------------ live signals
    def screen(self, s: SessionRow, a: AssignmentRow, sig: Signal, bars: pd.DataFrame, tick: Tick) -> Screen:
        if not self.enabled or bars.empty:
            return Screen(True, None, "off", None)
        champion = self.champion_of(a)
        state = self._filter(a.symbol, a.timeframe, a.strategy)
        mf = market_frame(bars.reset_index(drop=True).iloc[-FEATURE_BARS:]).iloc[-1]
        tr = self._arenas.get((a.symbol, a.timeframe))
        prev_r = tr.traders[champion.key].last_r if tr and champion.key in tr.traders else 0.0
        vec = vector(mf, sig.action, max(tick.ask - tick.bid, 0.0), prev_r)
        p = sf.predict(state, vec)
        blocked = sf.blocks(state, p)
        rec = self.repo.add_signal(
            session_id=s.id, symbol=a.symbol, timeframe=a.timeframe, strategy=a.strategy, candidate_key=champion.key,
            ts=int(bars["time"].iloc[-1]), side=sig.action, features=vec, p_win=p, filter_mode=state.mode,
            decision="blocked" if blocked else "taken",
        )
        note = f"P(win) {p:.2f} < {state.threshold:.2f}" if blocked and p is not None else ""
        return Screen(not blocked, p, state.mode, rec.id, note)

    def signal_outcome(self, record_id: int | None, ticket: int | None = None, rejected: bool = False) -> None:
        if record_id is None:
            return
        if rejected:
            self.repo.update_signal(record_id, decision="rejected")
        elif ticket is not None:
            self.repo.update_signal(record_id, ticket=ticket)

    def on_live_close(self, trade: TradeRow, s: SessionRow | None) -> None:
        rec = self.repo.signal_by_ticket(trade.ticket)
        if rec is None or trade.profit is None or not trade.risk_amount:
            return
        self.repo.update_signal(rec.id, r=round(trade.profit / trade.risk_amount, 5))
        if s is None:
            return
        versions = self.repo.versions(s.id, trade.symbol)
        last = versions[-1] if versions else None
        if not last or last.kind not in ("promotion", "auto_promotion") or not last.previous_key:
            return
        if self.repo.pending(s.id, trade.symbol):
            return
        live = self.repo.live_rs(s.id, trade.symbol, last.server_ts)
        old = self.repo.shadow_closed(trade.symbol, last.timeframe, last.previous_key, since=last.server_ts)
        if len(old) < 5:
            old = self.repo.shadow_closed(trade.symbol, last.timeframe, last.previous_key)[-50:]
        baseline = r_stats(x.r for x in old).mean if old else 0.0
        if should_rollback(live, baseline):
            self.repo.add_pending(
                session_id=s.id, symbol=trade.symbol, candidate_key=last.previous_key, kind="auto_rollback",
                reason=f"last {len(live)} live trades averaged {sum(live[-20:]) / 20:+.2f}R vs previous champion {baseline:+.2f}R",
            )
            self._j("rollback", f"Auto-rollback queued for {trade.symbol}: promotion underperformed", s, trade.symbol, level="warn", alert=True)

    # ---------------------------------------------------- promotions & rollback
    def pending(self, session_id: int, symbol: str) -> PendingChangeRow | None:
        return self.repo.pending(session_id, symbol)

    def cancel(self, change: PendingChangeRow, reason: str) -> None:
        self.repo.update_pending(change.id, status="cancelled", reason=f"{change.reason} — cancelled: {reason}")

    def apply(self, change: PendingChangeRow, s: SessionRow, a: AssignmentRow, now: int) -> Candidate | None:
        """Record the new Champion Version and reshuffle Challengers. Returns the Candidate to trade,
        or None if the change is no longer valid. The engine updates the Assignment itself."""
        new = self.repo.candidate(change.candidate_key)
        old = self.champion_of(a)
        if new is None:
            self.cancel(change, "candidate unknown")
            return None
        self._server_time = now
        self.repo.update_pending(change.id, status="applied", applied_ts=now)
        self.repo.add_version(
            session_id=s.id, symbol=a.symbol, timeframe=a.timeframe, candidate_key=new.key, previous_key=old.key,
            kind=change.kind, reason=change.reason, server_ts=now,
        )
        active = self.repo.challengers(a.symbol, a.timeframe)
        for ch in active:
            if ch.candidate_key == new.key:
                self.repo.update_challenger(ch.id, status="promoted", ended_ts=now)
        # the previous Champion keeps Shadow Trading as a Challenger: evidence for a Rollback
        if old.key != new.key and not any(ch.candidate_key == old.key for ch in active):
            self.repo.add_challenger(symbol=a.symbol, timeframe=a.timeframe, candidate_key=old.key, started_ts=now, note="previous champion")
            self._trim_challengers(a.symbol, a.timeframe, now)
        verb = "Rolled back" if "rollback" in change.kind else "Promoted"
        self._j(
            "promotion" if "promotion" in change.kind else "rollback",
            f"{verb} {a.symbol} {a.timeframe}: {old.label()} → {new.label()} ({change.kind.replace('_', ' ')}{': ' + change.reason if change.reason else ''})",
            s, a.symbol, level="warn", alert=True, data={"from": old.to_dict(), "to": new.to_dict(), "kind": change.kind},
        )
        return new

    def _trim_challengers(self, symbol: str, timeframe: str, now: int) -> None:
        active = self.repo.challengers(symbol, timeframe)
        while len(active) > MAX_CHALLENGERS:
            weakest = min(active, key=lambda ch: (self._shadow(symbol, timeframe, ch.candidate_key, ch.started_ts).n >= 30, self._shadow(symbol, timeframe, ch.candidate_key, ch.started_ts).mean))
            self.repo.update_challenger(weakest.id, status="retired", ended_ts=now, note="replaced")
            active = [x for x in active if x.id != weakest.id]

    def _shadow(self, symbol: str, timeframe: str, key: str, since: int) -> RStats:
        return r_stats(t.r for t in self.repo.shadow_closed(symbol, timeframe, key, since=since))

    def challenger_report(self, ch: ChallengerRow, champion: Candidate) -> dict:
        mine = self._shadow(ch.symbol, ch.timeframe, ch.candidate_key, ch.started_ts)
        champ = self._shadow(ch.symbol, ch.timeframe, champion.key, ch.started_ts)
        oos = RStats(**ch.oos) if ch.oos else None
        champ_oos = RStats(**ch.champion_oos) if ch.champion_oos else None
        checks = guardrails(mine, champ, oos, champ_oos, ch.trials, ch.robust)
        return {
            "shadow": mine.to_dict(),
            "champion_shadow": champ.to_dict(),
            "checks": [c.to_dict() for c in checks],
            "promotable": all(c.ok for c in checks),
        }

    def _consider_auto_promotion(self, s: SessionRow, a: AssignmentRow, champion: Candidate, is_demo: bool) -> None:
        if not is_demo or not self.repo.slot(s.id, a.symbol).auto_promote or self.repo.pending(s.id, a.symbol):
            return  # never automatic on a live account (checked again when applying)
        best = None
        for ch in self.repo.challengers(a.symbol, a.timeframe):
            if ch.candidate_key == champion.key:
                continue
            rep = self.challenger_report(ch, champion)
            if rep["promotable"] and (best is None or rep["shadow"]["sqn"] > best[1]["shadow"]["sqn"]):
                best = (ch, rep)
        if best:
            ch, rep = best
            self.repo.add_pending(
                session_id=s.id, symbol=a.symbol, candidate_key=ch.candidate_key, kind="auto_promotion", challenger_id=ch.id,
                reason=f"all Guardrails passed: shadow {rep['shadow']['mean']:+.2f}R/trade over {rep['shadow']['n']} trades vs champion {rep['champion_shadow']['mean']:+.2f}R",
            )
            self._j("promotion", f"Auto-promotion queued for {a.symbol} {a.timeframe} (applies when flat)", s, a.symbol)

    # ------------------------------------------------------------ operator commands
    def _slot_assignment(self, session_id: int, symbol: str) -> tuple[SessionRow, AssignmentRow]:
        s = self.store.get_session(session_id)
        a = next((x for x in self.store.assignments(session_id) if x.symbol == symbol), None)
        if a is None:
            raise DomainError("not_found", f"session {s.name!r} does not trade {symbol}", 404)
        return s, a

    def promote(self, challenger_id: int, session_id: int, force: bool = False) -> PendingChangeRow:
        ch = self.repo.challenger(challenger_id)
        if ch is None or ch.status != "active":
            raise DomainError("not_found", f"challenger {challenger_id} is not active", 404)
        s, a = self._slot_assignment(session_id, ch.symbol)
        if a.timeframe != ch.timeframe:
            raise DomainError("wrong_arena", f"{s.name} trades {a.symbol} on {a.timeframe}, the challenger is for {ch.timeframe}", 422)
        rep = self.challenger_report(ch, self.champion_of(a))
        if not rep["promotable"] and not force:
            failed = [c["detail"] for c in rep["checks"] if not c["ok"]]
            raise DomainError("guardrails_failed", "Guardrails not passed: " + "; ".join(failed), 409)
        cand = self.repo.candidate(ch.candidate_key)
        row = self.repo.add_pending(
            session_id=s.id, symbol=a.symbol, candidate_key=ch.candidate_key, kind="promotion", challenger_id=ch.id,
            reason=("operator override of failed Guardrails" if not rep["promotable"] else "operator, all Guardrails passed"),
        )
        self._j("promotion", f"Promotion queued for {a.symbol} {a.timeframe}: {cand.label()} (applies when flat)", s, a.symbol)
        return row

    def rollback(self, session_id: int, symbol: str) -> PendingChangeRow:
        s, a = self._slot_assignment(session_id, symbol)
        versions = self.repo.versions(session_id, symbol)
        target = next((v.previous_key for v in reversed(versions) if v.previous_key), None)
        if not target:
            raise DomainError("nothing_to_roll_back", f"{symbol} has no earlier Champion Version", 409)
        row = self.repo.add_pending(session_id=s.id, symbol=symbol, candidate_key=target, kind="rollback", reason="operator")
        self._j("rollback", f"Rollback queued for {symbol}: back to {self.repo.candidate(target).label()} (applies when flat)", s, symbol)
        return row

    def set_auto_promote(self, session_id: int, symbol: str, enabled: bool, is_live: bool) -> dict:
        s, a = self._slot_assignment(session_id, symbol)
        if enabled and is_live:
            raise DomainError("live_account", "auto-promotion is never allowed on a live account; promote manually", 409)
        self.repo.set_auto_promote(session_id, symbol, enabled)
        self._j("learning", f"Auto-promotion {'enabled' if enabled else 'disabled'} for {symbol}", s, symbol)
        return {"session_id": session_id, "symbol": symbol, "auto_promote": enabled}

    # -------------------------------------------------------- session lifecycle
    def on_session_started(self, s: SessionRow, assigns: list[AssignmentRow], now: int) -> None:
        self._server_time = now
        for a in assigns:
            champ = self.champion_of(a)
            self.repo.ensure_candidate(champ)
            self.repo.slot(s.id, a.symbol)
            versions = self.repo.versions(s.id, a.symbol)
            if not versions or versions[-1].candidate_key != champ.key:
                kind = "initial" if not versions else "manual"
                self.repo.add_version(session_id=s.id, symbol=a.symbol, timeframe=a.timeframe, candidate_key=champ.key, kind=kind, server_ts=now, reason="session start" if kind == "initial" else "edited in the session editor")
            if self.enabled and not self.repo.challengers(a.symbol, a.timeframe) and not self.repo.runs(a.symbol, a.timeframe, limit=1):
                self.request_optimize(a.symbol, a.timeframe, "first_start")

    def on_session_stopped(self, s: SessionRow) -> None:
        if self.enabled:
            for a in self.store.assignments(s.id):
                self.request_optimize(a.symbol, a.timeframe, "session_stop")

    def on_new_day(self, now: int) -> None:
        self._server_time = now
        if not self.enabled:
            return
        removed = self.repo.cleanup(now - RETENTION_DAYS * 86400)
        if any(removed.values()):
            log.info("learning retention cleanup: %s", removed)
        for s in self.store.list_sessions():
            if s.status in ACTIVE:
                for a in self.store.assignments(s.id):
                    self.request_optimize(a.symbol, a.timeframe, "daily")

    # ----------------------------------------------------------- optimizer queue
    def request_optimize(self, symbol: str, timeframe: str, trigger: str = "manual") -> int:
        key = (symbol, timeframe)
        if key in self._queued:
            return self._queued[key]  # merge duplicate triggers
        run = self.repo.add_run(symbol=symbol, timeframe=timeframe, trigger=trigger)
        self._queued[key] = run.id
        if self._queue is not None:
            self._queue.put_nowait((symbol, timeframe, run.id))
        return run.id

    async def drain(self) -> None:
        """Wait until every queued Optimizer Run has finished (tests, API 'learn now and wait')."""
        if self._queue is not None:
            await self._queue.join()

    def _arena_assignment(self, symbol: str, timeframe: str) -> tuple[SessionRow, AssignmentRow] | None:
        best = None
        for s in self.store.list_sessions():
            for a in self.store.assignments(s.id):
                if a.symbol == symbol and a.timeframe == timeframe:
                    if s.status in ACTIVE:
                        return s, a
                    best = best or (s, a)
        return best

    async def _work(self) -> None:
        while True:
            symbol, timeframe, run_id = await self._queue.get()
            try:
                await self._run(symbol, timeframe, run_id)
            except Exception as e:  # a failed run must never take the worker (or the engine) down
                log.exception("optimizer run %s failed", run_id)
                self.repo.update_run(run_id, status="failed", note=str(e)[:500], finished_wall=time.time())
            finally:
                self._queued.pop((symbol, timeframe), None)
                self._queue.task_done()

    async def _run(self, symbol: str, timeframe: str, run_id: int) -> None:
        found = self._arena_assignment(symbol, timeframe)
        if found is None:
            self.repo.update_run(run_id, status="failed", note="no session trades this arena", finished_wall=time.time())
            return
        s, a = found
        champion = self.champion_of(a)
        settings = self.store.app_settings()
        n_bars, n_cands = int(settings["learning_bars"]), int(settings["learning_candidates"])
        self.repo.update_run(run_id, status="running", started_wall=time.time(), champion_key=champion.key)
        tf = Timeframe(timeframe)
        bars, tick, info, now = await self.broker.run(
            lambda b: (b.closed_bars(symbol, tf, n_bars), b.tick(symbol), b.symbol_info(symbol), b.server_time())
        )
        profile = self.store.to_profile(self.store.risk_profile(a.risk_profile_id))
        rules = ExitRules.from_profile(profile, a.reverse_on_opposite)
        window = TradingWindow.from_dict(s.window)
        costs = Costs(spread=max(tick.ask - tick.bid, 0.0), slippage=info.point)
        exclude = {c.candidate_key for c in self.repo.challengers(symbol, timeframe)} | {champion.key}
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            self._pool, optimize_job, bars, champion, costs, rules, n_cands, run_id, window.to_dict(), tf.seconds, exclude
        )
        self._server_time = now
        if result.insufficient:
            self.repo.update_run(run_id, status="insufficient", note=result.insufficient, result=result.to_dict(), finished_wall=time.time())
            self._j("learning", f"Optimizer {symbol} {timeframe}: {result.insufficient}", symbol=symbol)
            return
        installed = self._install(symbol, timeframe, run_id, result, now)
        note = f"{result.evaluated} backtests on {result.bars} bars; {len(result.picks)} qualified; {len(installed)} new challenger(s)"
        self.repo.update_run(run_id, status="done", note=note, result=result.to_dict(), finished_wall=time.time())
        self._j("learning", f"Optimizer {symbol} {timeframe}: {note}", symbol=symbol)

    def _install(self, symbol: str, timeframe: str, run_id: int, result, now: int) -> list[str]:
        active = self.repo.challengers(symbol, timeframe)
        mature = sorted(
            (ch for ch in active if self._shadow(symbol, timeframe, ch.candidate_key, ch.started_ts).n >= 30),
            key=lambda ch: self._shadow(symbol, timeframe, ch.candidate_key, ch.started_ts).mean,
        )
        installed = []
        champ_oos = result.champion.wf.oos.to_dict() if result.champion else {}
        for pick in result.picks:
            if any(ch.candidate_key == pick.candidate.key for ch in active):
                continue
            if len(active) >= MAX_CHALLENGERS:
                if not mature:
                    break
                out = mature.pop(0)
                self.repo.update_challenger(out.id, status="retired", ended_ts=now, note=f"replaced by run #{run_id}")
                active = [x for x in active if x.id != out.id]
            self.repo.ensure_candidate(pick.candidate)
            ch = self.repo.add_challenger(
                symbol=symbol, timeframe=timeframe, candidate_key=pick.candidate.key, started_ts=now, run_id=run_id,
                oos=pick.wf.oos.to_dict(), champion_oos=champ_oos, trials=result.trials, robust=pick.robust,
            )
            active.append(ch)
            installed.append(pick.candidate.key)
        # refresh Walk-forward evidence for challengers that are still competing
        for ch in active:
            match = next((f for f in result.finalists if f.candidate.key == ch.candidate_key), None)
            if match:
                self.repo.update_challenger(ch.id, oos=match.wf.oos.to_dict(), champion_oos=champ_oos, trials=result.trials, robust=match.robust)
        return installed

    # ================================================================ read side
    def overview(self, is_live: bool) -> dict:
        arenas: dict[tuple[str, str], dict] = {}
        for s in self.store.list_sessions():
            for a in self.store.assignments(s.id):
                key = (a.symbol, a.timeframe)
                current = arenas.get(key)
                if current is None or (s.status in ACTIVE and current["session"]["status"] not in ACTIVE):
                    arenas[key] = self._arena_summary(s, a, is_live)
        return {
            "enabled": self.enabled,
            "mode": self.mode,
            "live_account": is_live,
            "queue": [{"symbol": k[0], "timeframe": k[1], "run_id": v} for k, v in self._queued.items()],
            "arenas": sorted(arenas.values(), key=lambda x: (x["session"]["status"] not in ACTIVE, x["symbol"], x["timeframe"])),
            "pending": [p.model_dump() for p in self.repo.all_pending()],
        }

    def _arena_summary(self, s: SessionRow, a: AssignmentRow, is_live: bool) -> dict:
        champion = self.champion_of(a)
        champ_stats = r_stats(t.r for t in self.repo.shadow_closed(a.symbol, a.timeframe, champion.key)[-200:])
        cands = self.repo.candidates([c.candidate_key for c in self.repo.challengers(a.symbol, a.timeframe)])
        challengers = []
        for ch in self.repo.challengers(a.symbol, a.timeframe):
            c = cands.get(ch.candidate_key)
            if c is None:
                continue
            challengers.append({"id": ch.id, "candidate": c.to_dict(), "started_ts": ch.started_ts, "note": ch.note, "oos": ch.oos, "robust": ch.robust, **self.challenger_report(ch, champion)})
        runs = self.repo.runs(a.symbol, a.timeframe, limit=1)
        fstate = self._filter(a.symbol, a.timeframe, a.strategy)
        pend = self.repo.pending(s.id, a.symbol)
        return {
            "symbol": a.symbol,
            "timeframe": a.timeframe,
            "session": {"id": s.id, "name": s.name, "status": s.status},
            "champion": {**champion.to_dict(), "shadow": champ_stats.to_dict()},
            "challengers": challengers,
            "filter": {k: v for k, v in fstate.to_dict().items() if k not in ("mean", "std")},
            "last_run": runs[0].model_dump(exclude={"result"}) if runs else None,
            "pending": pend.model_dump() if pend else None,
            "auto_promote": self.repo.slot(s.id, a.symbol).auto_promote,
            "auto_promote_allowed": not is_live,
            "versions": len(self.repo.versions(s.id, a.symbol)),
        }

    def arena(self, symbol: str, timeframe: str, is_live: bool) -> dict:
        found = self._arena_assignment(symbol, timeframe)
        if found is None:
            raise DomainError("not_found", f"no session trades {symbol} {timeframe}", 404)
        s, a = found
        summary = self._arena_summary(s, a, is_live)
        keys = [summary["champion"]["key"], *[c["candidate"]["key"] for c in summary["challengers"]]]
        curves = {}
        for k in keys:
            cum, pts = 0.0, []
            for t in self.repo.shadow_closed(symbol, timeframe, k)[-500:]:
                cum += t.r
                pts.append({"ts": t.close_ts, "r": round(cum, 3)})
            curves[k] = pts
        cands = self.repo.candidates([v.candidate_key for v in self.repo.arena_versions(symbol, timeframe)])
        versions = [
            {**v.model_dump(), "label": cands[v.candidate_key].label() if v.candidate_key in cands else v.candidate_key}
            for v in self.repo.arena_versions(symbol, timeframe)
        ]
        runs = [r.model_dump() for r in self.repo.runs(symbol, timeframe, limit=10)]
        signals = [r.model_dump(exclude={"features"}) for r in self.repo.signals(symbol, timeframe, limit=30)]
        return {**summary, "curves": curves, "history": versions, "runs": runs, "signals": signals}
