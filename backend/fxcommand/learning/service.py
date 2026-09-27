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
Operator commands: request_optimize, promote, rollback, set_auto_promote, request_evidence.
Read side: overview, arena, evidence, evidence_for, assignment_evidence, scorecard.

CPU-heavy work (Optimizer Runs, Evidence Runs) runs in the learning pool with pure inputs and
outputs; service state is only mutated on the event loop. One queue serves both, Optimizer Runs
first: an Evidence Run is low priority. Bars are fetched through the engine's BrokerThread; an
Evidence Run reads its history in pages of at most ``CHUNK_BARS``, each its own short broker call,
and waits while the engine has an order in flight (``order_busy``). Nothing here can send, modify
or close an order.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import time
from concurrent.futures import Executor, ProcessPoolExecutor, ThreadPoolExecutor
from dataclasses import asdict, dataclass, field

import pandas as pd

from ..broker import BrokerError, BrokerThread, SymbolInfo, Tick, Timeframe
from ..engine.schemas import DomainError
from ..journal import Journal
from ..risk import TradingWindow
from ..risk.window import WeekendClose
from ..risk.spreads import SpreadBook
from ..store import AssignmentRow, SessionRow, Store, TradeRow
from ..store.models import ChallengerRow, PendingChangeRow
from ..strategies import Signal, get_strategy
from ..strategies.base import WARMUP_BARS
from ..tasks import cancel_and_wait
from . import signal_filter as sf
from .candidate import Candidate
from .features import market_frame, vector
from .objective import RStats, guardrails, r_stats, should_rollback
from .optimizer import TOP_K, evaluate_job, optimize_job
from . import evidence as ev
from . import research
from .costs import CostModel
from .paper import Costs, EntryGate, ExitRules, PaperTrader, bar_spreads
from .repo import LearningRepo
from .seeds import SEEDS

log = logging.getLogger("fxcommand.learning")

MAX_CHALLENGERS = 3
RETRAIN_EVERY = 10
RETENTION_DAYS = 180
FEATURE_BARS = 160  # market features need ~100 bars of ATR rank + EMA50 warm-up
AUTO_KINDS = ("auto_promotion", "auto_rollback")
ACTIVE = ("running", "paused")
BUSY_WAIT = 60.0  # seconds an Evidence page waits for an order in flight before reading anyway
OPTIMIZE, EVIDENCE = 0, 1  # queue priorities: lower runs first


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
        self._queue: asyncio.PriorityQueue | None = None
        self._seq = itertools.count()
        self._evidence_queued: dict[str, int] = {}  # evidence key -> row id (queued or running)
        self._submitted: dict[tuple[str, str], dict[str, int]] = {}  # arena -> reviewer Candidate key -> run id (queued)
        self.order_busy = lambda: False  # set by the engine: True while an entry or close is in flight
        self._queued: dict[tuple[str, str], int] = {}  # arena -> run id (queued or running)
        self._worker: asyncio.Task | None = None
        # Optimizer Runs are CPU-bound pure Python: in the app they run in a separate process so they
        # never compete with the engine for the GIL; tests may use a thread
        self._pool: Executor = ProcessPoolExecutor(max_workers=1) if processes else ThreadPoolExecutor(max_workers=1, thread_name_prefix="learner")
        self._server_time = 0
        self._known: set[str] = set()  # Candidate keys already stored
        self._filters: dict[str, sf.FilterState] = {}  # cache of learning_filters rows
        self.spreads = SpreadBook(store)  # typical spreads (shared with the Cost Check through the store)

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
        if self.mode == "mt5":  # research on the real feed; a simulated market has no such history
            self.seed_research()
        for r in self.repo.runs(limit=200):
            if r.status in ("queued", "running"):
                self.repo.update_run(r.id, status="failed", note="interrupted by restart", finished_wall=time.time())
        self.repo.fail_unfinished_evidence("interrupted by restart")
        self._queue = asyncio.PriorityQueue()
        self._worker = asyncio.create_task(self._work(), name="learner")

    async def stop(self) -> None:
        await cancel_and_wait(self._worker)
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

    def _typical_spread(self, symbol: str, tick: Tick, now: int) -> float:
        """The spread Shadow Trades and Optimizer Runs are priced at (``SpreadBook``: recent good
        quotes, never one from the daily break). Falls back to the current quote until one is known."""
        typical = self.spreads.spread_for(symbol, tick, now)
        return typical if typical is not None else max(tick.ask - tick.bid, 0.0)

    @staticmethod
    def _job_args(bars, champion, costs, rules, n_cands, run_id, window, tf, exclude, weekend_close: str | None = None) -> tuple:
        """The Optimizer Run's arguments, as sent to the worker process (every one must pickle)."""
        return (bars, champion, costs, rules, n_cands, run_id, window.to_dict(), tf.seconds, exclude, weekend_close)

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
        tf_secs = Timeframe(a.timeframe).seconds
        spread = self._typical_spread(a.symbol, tick, now)
        spread_ok = round(spread / info.point, 1) <= profile.max_spread_points if info.point else True  # as Tick.spread_points
        costs = CostModel.from_symbol(info, spread=spread, ref_price=float(bars["close"].iloc[-1]))
        gate = EntryGate(s.window, tf_secs)
        weekend = WeekendClose(s.weekend_close_time, tf_secs) if s.weekend_close else None
        # market features are only needed when some Candidate schedules an entry: compute lazily, once,
        # on a short tail of the window (same index as ``bars``)
        _mf: list[pd.DataFrame] = []

        def mf_row(i: int) -> pd.Series:
            if not _mf:
                _mf.append(market_frame(bars.iloc[max(0, new_idx[0] - FEATURE_BARS) :]))
            return _mf[0].loc[i]

        o, h, l, c = (bars[k].to_numpy(dtype=float) for k in ("open", "high", "low", "close"))
        recorded = bar_spreads(bars, costs)  # the bars' own spreads: a floor where the symbol's profile asks for one
        filters = {st: self._filter(a.symbol, a.timeframe, st) for st in {x.strategy for x in cands.values()}}
        touched: set[str] = set()

        for key, cand in cands.items():
            tr = arena.traders.get(key)
            if tr is None:
                tr = arena.traders[key] = PaperTrader(costs, rules)
            tr.costs, tr.rules = costs, rules
            tr.weekend = weekend
            cand_gate = gate.for_strategy(cand.strategy)
            tr.allow_entry = lambda ts, g=cand_gate: spread_ok and g(ts)
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
                    {k: v[i] for k, v in cols.items()}, make_features, float(recorded[i]) if recorded is not None else 0.0,
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

    def signal_outcome(
        self, record_id: int | None, ticket: int | None = None, rejected: bool = False,
        fill_delay_s: int | None = None, fill_spread_points: float | None = None,
    ) -> None:
        if record_id is None:
            return
        if rejected:
            self.repo.update_signal(record_id, decision="rejected")
        elif ticket is not None:
            self.repo.update_signal(record_id, ticket=ticket, fill_delay_s=fill_delay_s, fill_spread_points=fill_spread_points)

    def on_live_close(self, trade: TradeRow, s: SessionRow | None) -> None:
        rec = self.repo.signal_by_ticket(trade.ticket)
        if rec is None or trade.profit is None or not trade.risk_amount:
            return
        self.repo.update_signal(rec.id, r=round(trade.profit / trade.risk_amount, 5))
        if s is None:
            return
        tf = trade.timeframe
        versions = self.repo.versions(s.id, trade.symbol, tf)
        last = versions[-1] if versions else None
        if not last or last.kind not in ("promotion", "auto_promotion") or not last.previous_key:
            return
        if self.repo.pending(s.id, trade.symbol, tf):
            return
        live = self.repo.live_rs(s.id, trade.symbol, last.server_ts, tf)
        old = self.repo.shadow_closed(trade.symbol, last.timeframe, last.previous_key, since=last.server_ts)
        if len(old) < 5:
            old = self.repo.shadow_closed(trade.symbol, last.timeframe, last.previous_key)[-50:]
        baseline = r_stats(x.r for x in old).mean if old else 0.0
        if should_rollback(live, baseline):
            self.repo.add_pending(
                session_id=s.id, symbol=trade.symbol, timeframe=tf, candidate_key=last.previous_key, kind="auto_rollback",
                reason=f"last {len(live)} live trades averaged {sum(live[-20:]) / 20:+.2f}R vs previous champion {baseline:+.2f}R",
            )
            self._j("rollback", f"Auto-rollback queued for {trade.symbol}: promotion underperformed", s, trade.symbol, level="warn", alert=True)

    # ---------------------------------------------------- promotions & rollback
    def pending(self, session_id: int, symbol: str, timeframe: str | None = None) -> PendingChangeRow | None:
        return self.repo.pending(session_id, symbol, timeframe)

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
        trials = ch.trials
        if ch.source == "reviewer":  # trials logged after the submission still raise its penalty
            trials = max(trials, self.repo.trial_count(ch.symbol, ch.timeframe), TOP_K)
        checks = guardrails(mine, champ, oos, champ_oos, trials, ch.robust)
        return {
            "shadow": mine.to_dict(),
            "champion_shadow": champ.to_dict(),
            "checks": [c.to_dict() for c in checks],
            "promotable": all(c.ok for c in checks),
        }

    def _consider_auto_promotion(self, s: SessionRow, a: AssignmentRow, champion: Candidate, is_demo: bool) -> None:
        if not is_demo or not self.repo.slot(s.id, a.symbol, a.timeframe).auto_promote or self.repo.pending(s.id, a.symbol, a.timeframe):
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
                session_id=s.id, symbol=a.symbol, timeframe=a.timeframe, candidate_key=ch.candidate_key, kind="auto_promotion", challenger_id=ch.id,
                reason=f"all Guardrails passed: shadow {rep['shadow']['mean']:+.2f}R/trade over {rep['shadow']['n']} trades vs champion {rep['champion_shadow']['mean']:+.2f}R",
            )
            self._j("promotion", f"Auto-promotion queued for {a.symbol} {a.timeframe} (applies when flat)", s, a.symbol)

    # ------------------------------------------------------------ operator commands
    def _slot_assignment(self, session_id: int, symbol: str, timeframe: str | None = None) -> tuple[SessionRow, AssignmentRow]:
        """The Assignment for (symbol, timeframe). The timeframe may be left out while the Symbol
        appears only once in the Session."""
        s = self.store.get_session(session_id)
        same = [x for x in self.store.assignments(session_id) if x.symbol == symbol]
        exact = [x for x in same if x.timeframe == timeframe]
        if exact:
            return s, exact[0]
        if not same:
            raise DomainError("not_found", f"session {s.name!r} does not trade {symbol}", 404)
        if len(same) > 1:
            tfs = ", ".join(x.timeframe for x in same)
            raise DomainError("ambiguous_slot", f"session {s.name!r} trades {symbol} on {tfs}: say which timeframe", 422)
        return s, same[0]

    def promote(self, challenger_id: int, session_id: int, force: bool = False) -> PendingChangeRow:
        ch = self.repo.challenger(challenger_id)
        if ch is None or ch.status != "active":
            raise DomainError("not_found", f"challenger {challenger_id} is not active", 404)
        s, a = self._slot_assignment(session_id, ch.symbol, ch.timeframe)
        if a.timeframe != ch.timeframe:
            raise DomainError("wrong_arena", f"{s.name} trades {a.symbol} on {a.timeframe}, the challenger is for {ch.timeframe}", 422)
        rep = self.challenger_report(ch, self.champion_of(a))
        if not rep["promotable"] and not force:
            failed = [c["detail"] for c in rep["checks"] if not c["ok"]]
            raise DomainError("guardrails_failed", "Guardrails not passed: " + "; ".join(failed), 409)
        cand = self.repo.candidate(ch.candidate_key)
        row = self.repo.add_pending(
            session_id=s.id, symbol=a.symbol, timeframe=a.timeframe, candidate_key=ch.candidate_key, kind="promotion", challenger_id=ch.id,
            reason=("operator override of failed Guardrails" if not rep["promotable"] else "operator, all Guardrails passed"),
        )
        self._j("promotion", f"Promotion queued for {a.symbol} {a.timeframe}: {cand.label()} (applies when flat)", s, a.symbol)
        return row

    def rollback(self, session_id: int, symbol: str, timeframe: str | None = None) -> PendingChangeRow:
        s, a = self._slot_assignment(session_id, symbol, timeframe)
        versions = self.repo.versions(session_id, symbol, a.timeframe)
        target = next((v.previous_key for v in reversed(versions) if v.previous_key), None)
        if not target:
            raise DomainError("nothing_to_roll_back", f"{symbol} has no earlier Champion Version", 409)
        row = self.repo.add_pending(session_id=s.id, symbol=symbol, timeframe=a.timeframe, candidate_key=target, kind="rollback", reason="operator")
        self._j("rollback", f"Rollback queued for {symbol}: back to {self.repo.candidate(target).label()} (applies when flat)", s, symbol)
        return row

    def set_auto_promote(self, session_id: int, symbol: str, enabled: bool, is_live: bool, timeframe: str | None = None) -> dict:
        s, a = self._slot_assignment(session_id, symbol, timeframe)
        if enabled and is_live:
            raise DomainError("live_account", "auto-promotion is never allowed on a live account; promote manually", 409)
        self.repo.set_auto_promote(session_id, symbol, enabled, a.timeframe)
        self._j("learning", f"Auto-promotion {'enabled' if enabled else 'disabled'} for {symbol} {a.timeframe}", s, symbol)
        return {"session_id": session_id, "symbol": symbol, "timeframe": a.timeframe, "auto_promote": enabled}

    # -------------------------------------------------------- session lifecycle
    def on_session_started(self, s: SessionRow, assigns: list[AssignmentRow], now: int) -> None:
        self._server_time = now
        for a in assigns:
            champ = self.champion_of(a)
            self.repo.ensure_candidate(champ)
            self.repo.slot(s.id, a.symbol, a.timeframe)
            versions = self.repo.versions(s.id, a.symbol, a.timeframe)
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
            self._queue.put_nowait((OPTIMIZE, next(self._seq), ("optimize", (symbol, timeframe, run.id))))
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
            _, _, (kind, job) = await self._queue.get()
            try:
                if kind == "optimize":
                    await self._work_optimize(*job)
                elif kind == "challenger":
                    await self._work_submission(*job)
                else:
                    await self._work_evidence(job)
            finally:
                self._queue.task_done()

    async def _work_submission(self, symbol: str, timeframe: str, run_id: int, cand: Candidate, note: str) -> None:
        try:
            await self._run_submission(symbol, timeframe, run_id, cand, note)
        except Exception as e:
            log.exception("reviewer submission %s failed", run_id)
            self.repo.update_run(run_id, status="failed", note=str(e)[:500], finished_wall=time.time())
        finally:
            self._submitted.get((symbol, timeframe), {}).pop(cand.key, None)

    async def _work_optimize(self, symbol: str, timeframe: str, run_id: int) -> None:
        try:
            await self._run(symbol, timeframe, run_id)
        except Exception as e:  # a failed run must never take the worker (or the engine) down
            log.exception("optimizer run %s failed", run_id)
            self.repo.update_run(run_id, status="failed", note=str(e)[:500], finished_wall=time.time())
        finally:
            self._queued.pop((symbol, timeframe), None)

    async def _work_evidence(self, evidence_id: int) -> None:
        row = self.repo.evidence_row(evidence_id)
        try:
            await self._run_evidence(evidence_id)
        except Exception as e:
            log.exception("evidence run %s failed", evidence_id)
            self.repo.update_evidence(evidence_id, status="failed", note=str(e)[:500], finished_wall=time.time())
        finally:
            if row is not None:
                self._evidence_queued.pop(row.key, None)

    # ------------------------------------------------------------ evidence runs
    def spec_for(self, s: SessionRow, a: AssignmentRow) -> ev.EvidenceSpec:
        """The Evidence an Assignment's settings call for: its Candidate, its Risk Profile's exit rules,
        its Session's Trading Window and Weekend Close."""
        profile = self.store.to_profile(self.store.risk_profile(a.risk_profile_id))
        return ev.EvidenceSpec.of(
            a.symbol, a.timeframe, a.strategy, a.params, ExitRules.from_profile(profile, a.reverse_on_opposite),
            s.window, s.weekend_close_time if s.weekend_close else None,
        )

    def request_evidence(self, spec: ev.EvidenceSpec, trigger: str = "manual") -> dict:
        """Queue an Evidence Run (low priority: after every queued Optimizer Run). The same settings
        already queued or running are merged. Refused while the engine has an order in flight."""
        if self.order_busy():
            raise DomainError("busy", "an order is being sent right now: try the Evidence Run again in a moment", 409)
        if spec.key in self._evidence_queued:
            return ev.summary(self.repo.evidence_row(self._evidence_queued[spec.key]))
        row = self.repo.add_evidence(**spec.row_fields(), trigger=trigger)
        self._evidence_queued[spec.key] = row.id
        if self._queue is not None:
            self._queue.put_nowait((EVIDENCE, next(self._seq), ("evidence", row.id)))
        return ev.summary(row)

    async def _wait_for_orders(self) -> None:
        waited = 0.0
        while self.order_busy() and waited < BUSY_WAIT:
            await asyncio.sleep(0.2)
            waited += 0.2

    async def _read_history(self, symbol: str, tf: Timeframe, warm_before: tuple[int, int] | None = None) -> tuple[pd.DataFrame, str, bool]:
        """Every closed bar the feed has (up to ``MAX_EVIDENCE_BARS``, newest kept), read newest page
        first. Returns (bars, note, complete): a page that fails or times out ends the read, and the
        run is marked incomplete with the bars received. ``warm_before=(ts, n)`` stops once ``n`` bars
        older than ``ts`` have been read."""
        pages: list[pd.DataFrame] = []
        got, note, complete, older = 0, "", True, 0
        while True:
            if warm_before is not None and older >= warm_before[1]:
                break
            if got >= ev.MAX_EVIDENCE_BARS:
                note = f"history capped at the most recent {ev.MAX_EVIDENCE_BARS:,} bars"
                break
            n = min(ev.CHUNK_BARS, ev.MAX_EVIDENCE_BARS - got)
            await self._wait_for_orders()
            try:
                page = await self.broker.run(lambda b, n=n, o=got: b.closed_bars(symbol, tf, n, o), timeout=ev.CHUNK_TIMEOUT)
            except (BrokerError, asyncio.TimeoutError) as e:
                note, complete = f"history read stopped after {got:,} bars: {type(e).__name__} {e}".strip(), False
                break
            if len(page) == 0:
                break
            pages.append(page)
            got += len(page)
            if warm_before is not None:
                older += int((page["time"].to_numpy() < warm_before[0]).sum())
            if len(page) < n:
                break
        if not pages:
            return pd.DataFrame(columns=["time", "open", "high", "low", "close", "volume"]), note, complete
        bars = pd.concat(pages[::-1], ignore_index=True).drop_duplicates("time").sort_values("time").reset_index(drop=True)
        return bars, note, complete

    async def _run_evidence(self, evidence_id: int) -> None:
        row = self.repo.evidence_row(evidence_id)
        spec = ev.EvidenceSpec.from_row(row)
        self.repo.update_evidence(evidence_id, status="running")
        tf = Timeframe(spec.timeframe)
        symbol = spec.symbol
        tick, info, now = await self.broker.run(lambda b: (b.tick(symbol), b.symbol_info(symbol), b.server_time()), timeout=ev.CHUNK_TIMEOUT)
        spread = self.spreads.spread_for(symbol, tick, now)
        if spread is None:
            self.repo.update_evidence(evidence_id, status="failed", run_ts=now, finished_wall=time.time(),
                                      note="no typical spread is known yet and the market is shut: run it when the market is open")
            return
        bars, note, complete = await self._read_history(symbol, tf)
        if len(bars) == 0:
            self.repo.update_evidence(evidence_id, status="failed", run_ts=now, finished_wall=time.time(), note=note or "the feed returned no history")
            return
        costs = CostModel.from_symbol(info, spread=ev.SPREAD_MULTIPLIER * spread, ref_price=float(bars["close"].iloc[-1]), floor_mult=ev.SPREAD_MULTIPLIER)
        gate = EntryGate(spec.window, tf.seconds)
        weekend = WeekendClose(spec.weekend_close, tf.seconds) if spec.weekend_close else None
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(self._pool, ev.evidence_job, bars, spec.candidate, costs, spec.rules, gate, weekend, spec.history_from)
        status = "done" if complete else "incomplete"
        self.repo.update_evidence(evidence_id, status=status, run_ts=now, finished_wall=time.time(), costs=costs.to_dict(), note=note, **result)
        self._server_time = now
        label = f"{symbol} {spec.timeframe} {spec.candidate.label()}"
        self._j("learning", f"Evidence {label}: {result['trades']} trades, {result['mean_r']:+.3f}R per trade on {result['bars']:,} bars"
                + ("" if complete else " (incomplete)"), symbol=symbol)

    def seed_research(self, seeds: tuple = SEEDS) -> int:
        """Load research done outside the app into the trial ledger, once per seed (spec-btc-strategies
        D17): its trials raise the selection penalty, and its holdout row makes a second look refused."""
        added = 0
        for seed in seeds:
            source = f"seed:{seed['id']}"
            if self.repo.has_trial_source(source):
                continue
            for t in seed["trials"]:
                self.repo.add_trial(
                    symbol=seed["symbol"], timeframe=t["timeframe"], hypothesis=t["hypothesis"], strategy="", params={},
                    params_hash=research.hypothesis_hash(None, None, t["hypothesis"]), data_from=seed["data_from"], data_to=seed["data_to"],
                    result=t["result"], source=source, ts=seed["data_to"],
                )
                added += 1
            h = seed["holdout"]
            cand = Candidate.of(h["strategy"], h["params"])
            self.repo.add_trial(
                symbol=seed["symbol"], timeframe=h["timeframe"], hypothesis=h["hypothesis"], strategy=h["strategy"], params=cand.param_dict,
                params_hash=cand.key, data_from=h["data_from"], data_to=h["data_to"], result=h["result"], holdout_used=True,
                status=h["status"], source=source, ts=h["data_to"],
            )
            added += 1
            log.info("research seed %s: %d ledger rows", seed["id"], len(seed["trials"]) + 1)
        return added

    def evidence(self, symbol: str | None = None, timeframe: str | None = None, strategy: str | None = None) -> list[dict]:
        """The latest Evidence per key (the Strategies page's table)."""
        seen, out = set(), []
        for r in self.repo.evidence_rows(symbol=symbol, timeframe=timeframe, strategy=strategy, limit=1000):
            if r.key not in seen:
                seen.add(r.key)
                out.append(ev.summary(r))
        return out

    def evidence_for(self, spec: ev.EvidenceSpec) -> dict:
        """The badge: ``match`` (Evidence for exactly these settings), ``running``, ``mismatch``
        (Evidence exists for this Strategy on this Arena, but for other settings) or ``none``."""
        rows = self.repo.evidence_rows(key=spec.key, limit=20)
        done = next((r for r in rows if r.status in ("done", "incomplete")), None)
        if done is not None:
            return {"status": "match", "key": spec.key, "evidence": ev.summary(done)}
        if any(r.status in ("queued", "running") for r in rows):
            return {"status": "running", "key": spec.key, "evidence": None}
        other = self.repo.evidence_rows(symbol=spec.symbol, timeframe=spec.timeframe, strategy=spec.candidate.strategy, status=("done", "incomplete"), limit=1)
        if other:
            return {"status": "mismatch", "key": spec.key, "evidence": ev.summary(other[0])}
        return {"status": "none", "key": spec.key, "evidence": None}

    def assignment_evidence(self, s: SessionRow, a: AssignmentRow) -> dict:
        """The badge for an Assignment; with Weekend Close on for H4/D1, also the Evidence without it
        (spec D6: Weekend Close cuts a trend edge — show by how much)."""
        spec = self.spec_for(s, a)
        out = self.evidence_for(spec)
        if s.weekend_close and a.timeframe in ("H4", "D1"):
            out["without_weekend_close"] = self.evidence_for(spec.with_weekend_close(None))
        return out

    # ================================================= Claude Strategy Review
    async def _server_now(self) -> int:
        now = await self.broker.run(lambda b: b.server_time(), timeout=ev.CHUNK_TIMEOUT)
        self._server_time = now
        return now

    def spec_from(
        self, symbol: str, timeframe: str, strategy: str, params: dict | None = None, session_id: int | None = None,
        risk_profile_id: int | None = None, reverse_on_opposite: bool = True, window: dict | None = None, weekend_close: str | None = None,
    ) -> ev.EvidenceSpec:
        """Evidence settings from an editor form or a request. A Session supplies its Trading Window and
        Weekend Close; a Risk Profile its exit rules. Default: no breakeven, no trailing stop."""
        try:
            get_strategy(strategy)
        except KeyError:
            raise DomainError("unknown_strategy", f"unknown strategy {strategy!r}", 422) from None
        if session_id is not None:
            s = self.store.get_session(session_id)
            window, weekend_close = s.window, (s.weekend_close_time if s.weekend_close else None)
        rules = ExitRules(reverse_on_opposite=reverse_on_opposite)
        if risk_profile_id is not None:
            rules = ExitRules.from_profile(self.store.to_profile(self.store.risk_profile(risk_profile_id)), reverse_on_opposite)
        return ev.EvidenceSpec.of(symbol, timeframe, strategy, params, rules, window, weekend_close)

    def trial_summary(self) -> list[dict]:
        now = self._server_time or int(time.time())
        return [
            {"symbol": k[0], "timeframe": k[1], **v, "holdout_from": research.holdout_from(now, k[1])}
            for k, v in sorted(self.repo.trial_counts().items())
        ]

    async def record_trial(
        self, symbol: str, timeframe: str, hypothesis: str, data_from: int, data_to: int, result: dict,
        strategy: str | None = None, params: dict | None = None, source: str = "cli",
    ) -> dict:
        """Append a research trial to the ledger, whatever its result. Refused when its data reaches the
        sealed holdout: research runs on the snapshot, which ends before it."""
        tf = Timeframe(timeframe)
        now = await self._server_now()
        cutoff = research.holdout_from(now, tf.value)
        if data_to + tf.seconds > cutoff:
            raise DomainError("holdout", f"the trial's data reaches the sealed holdout (from {_day(cutoff)}): research only on the snapshot", 422)
        if data_from > data_to:
            raise DomainError("invalid", "data_from is after data_to", 422)
        try:
            ph = research.hypothesis_hash(strategy, params, hypothesis)
        except KeyError:
            raise DomainError("unknown_strategy", f"unknown strategy {strategy!r}", 422) from None
        row = self.repo.add_trial(
            symbol=symbol, timeframe=tf.value, hypothesis=hypothesis, strategy=strategy or "",
            params=Candidate.of(strategy, params).param_dict if strategy else (params or {}), params_hash=ph,
            data_from=data_from, data_to=data_to, result=result, source=source, ts=now,
        )
        return {**row.model_dump(), "arena_trials": self.repo.trial_count(symbol, tf.value)}

    async def snapshot(self, symbol: str, timeframe: str, tail: int | None = None, arena_timeframe: str | None = None) -> dict:
        """The research snapshot of an Arena: every closed bar before its sealed holdout, with the costs
        today's typical spread implies. Read-only. ``arena_timeframe``: these bars are context for another
        Arena (e.g. M15 bars for the GOLD M5 Arena) and end where *that* Arena's holdout starts."""
        tf = Timeframe(timeframe)
        now = await self._server_now()
        cutoff = research.holdout_from(now, Timeframe(arena_timeframe or tf).value)
        tick, info = await self.broker.run(lambda b: (b.tick(symbol), b.symbol_info(symbol)), timeout=ev.CHUNK_TIMEOUT)
        spread = self.spreads.spread_for(symbol, tick, now)
        if tail == 0:  # metadata only (the CLI's check of its cache): no history read
            bars, note, complete = pd.DataFrame(columns=["time", "open", "high", "low", "close", "volume"]), "", True
            total, first_ts = None, None
        else:
            bars, note, complete = await self._read_history(symbol, tf)
            bars = research.research_bars(bars, cutoff, tf.seconds)
            total = len(bars)
            first_ts = int(bars["time"].iloc[0]) if total else None
            if tail is not None:
                bars = bars.iloc[-tail:]
        # today's spread at today's price: older bars are priced by scaling from here (the Cost Model)
        costs = CostModel.from_symbol(info, spread=spread, ref_price=float(tick.bid)) if spread is not None and tick.bid > 0 else None
        return {
            "symbol": symbol, "timeframe": tf.value, "server_time": now, "holdout_from": cutoff,
            "bars_total": total, "first_ts": first_ts,
            "complete": complete, "note": note,
            "costs": costs.to_dict() if costs else None, "evidence_spread_multiplier": ev.SPREAD_MULTIPLIER,
            "trusted_from": research.trusted_from(symbol),
            "arena_trials": self.repo.trial_count(symbol, tf.value),
            "arena": self._research_arena(symbol, tf.value),
            "bars": {k: bars[k].tolist() for k in ("time", "open", "high", "low", "close", "volume", "spread") if k in bars.columns},
        }

    def _research_arena(self, symbol: str, timeframe: str) -> dict | None:
        """The Champion and the settings research must use for an Arena a Session trades (None: a research-only
        Arena, e.g. GOLD M5 scalping, judged against zero)."""
        found = self._arena_assignment(symbol, timeframe)
        if found is None:
            return None
        spec = self.spec_for(*found)
        return {
            "session": found[0].name, "champion": self.champion_of(found[1]).to_dict(),
            "rules": asdict(spec.rules), "window": spec.window, "weekend_close": spec.weekend_close,
        }

    async def evaluate_holdout(self, symbol: str, timeframe: str, strategy: str, params: dict | None, hypothesis: str) -> dict:
        """Score a finalist on the sealed holdout, once. The ledger row is written before the backtest, so
        a second request — or a retry after a crash — is refused and the first result stands."""
        tf = Timeframe(timeframe)
        try:
            cand = Candidate.of(strategy, params)
        except KeyError:
            raise DomainError("unknown_strategy", f"unknown strategy {strategy!r}", 422) from None
        earlier = self.repo.holdout_trial(symbol, tf.value, cand.key)
        if earlier is not None:
            raise DomainError("holdout_used", f"{cand.label()} was already scored on the {symbol} {tf.value} holdout on {_day(earlier.ts)}: "
                              f"{earlier.status} — the result is final", 409)
        now = await self._server_now()
        cutoff = research.holdout_from(now, tf.value)
        tick = await self.broker.run(lambda b: b.tick(symbol), timeout=ev.CHUNK_TIMEOUT)
        spread = self.spreads.spread_for(symbol, tick, now)
        if spread is None:
            raise DomainError("market_shut", "no typical spread is known yet and the market is shut: evaluate when it is open", 409)
        found = self._arena_assignment(symbol, tf.value)
        spec = self.spec_for(*found) if found else ev.EvidenceSpec.of(symbol, tf.value, strategy, params)
        champion = self.champion_of(found[1]) if found else None
        # reserve the one use before anything can fail — checked again with no await in between, so
        # two concurrent requests can never both pass
        if self.repo.holdout_trial(symbol, tf.value, cand.key) is not None:
            raise DomainError("holdout_used", f"{cand.label()} is already being scored on the {symbol} {tf.value} holdout", 409)
        row = self.repo.add_trial(
            symbol=symbol, timeframe=tf.value, hypothesis=hypothesis, strategy=strategy, params=cand.param_dict, params_hash=cand.key,
            data_from=cutoff, data_to=now, holdout_used=True, status="evaluating", source="holdout", ts=now,
        )
        try:
            warm = max(get_strategy(c.strategy).lookback(get_strategy(c.strategy).resolve(c.param_dict)) for c in (cand, champion) if c) + WARMUP_BARS
            bars, note, complete = await self._read_history(symbol, tf, warm_before=(cutoff, warm))
            before = int((bars["time"].to_numpy() < cutoff).sum()) if len(bars) else 0
            bars = bars.iloc[max(0, before - warm):].reset_index(drop=True)
            info = await self.broker.run(lambda b: b.symbol_info(symbol), timeout=ev.CHUNK_TIMEOUT)
            costs = CostModel.from_symbol(info, spread=ev.SPREAD_MULTIPLIER * spread, ref_price=float(bars["close"].iloc[-1]), floor_mult=ev.SPREAD_MULTIPLIER)
            weekend = WeekendClose(spec.weekend_close, tf.seconds) if spec.weekend_close else None
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(
                self._pool, research.holdout_job, bars, cand, champion, costs, spec.rules, EntryGate(spec.window, tf.seconds), weekend, cutoff
            )
            result.update(note=note, complete=complete, champion_key=champion.key if champion else None)
            status = "passed" if result["passed"] else "failed"
        except Exception as e:
            log.exception("holdout evaluation failed")
            self.repo.update_trial(row.id, status="error", result={"error": str(e)[:500]})
            raise
        row = self.repo.update_trial(row.id, status=status, result=result)
        c = result["candidate"]
        self._j("learning", f"Holdout {symbol} {tf.value} {cand.label()}: {status} ({c['trades']} trades, {c['mean_r']:+.3f}R)", symbol=symbol)
        return row.model_dump()

    def submit_challenger(self, symbol: str, timeframe: str, strategy: str, params: dict | None, note: str = "") -> dict:
        """A reviewer's Candidate for an Arena. It is judged like an Optimizer finalist (walk-forward,
        neighbours) and then Shadow Trades under the same Guardrails. It never promotes, never touches
        a Session and never retires another Challenger to make room."""
        tf = Timeframe(timeframe).value
        found = self._arena_assignment(symbol, tf)
        if found is None:
            raise DomainError("not_found", f"no session trades {symbol} {tf}", 404)
        try:
            cand = Candidate.of(strategy, params)
        except KeyError:
            raise DomainError("unknown_strategy", f"unknown strategy {strategy!r}", 422) from None
        champion = self.champion_of(found[1])
        if get_strategy(cand.strategy).family != get_strategy(champion.strategy).family:
            raise DomainError("cross_family", f"{cand.label()} is not in the Champion's family ({champion.label()}): new families arrive as code for review", 422)
        if cand.key == champion.key:
            raise DomainError("is_champion", "that is the Champion already", 409)
        # the review's loop: finalist, one score on the sealed holdout, and only a pass is submitted
        # (its walk-forward reads the newest bars, which overlap the holdout: a failure must stay final)
        held = self.repo.holdout_trial(symbol, tf, cand.key)
        if held is None:
            raise DomainError("holdout_required", f"score {cand.label()} on the {symbol} {tf} holdout first (POST /api/research/holdout)", 409)
        if held.status == "evaluating":
            raise DomainError("holdout_pending", "its holdout evaluation has not finished", 409)
        if held.status != "passed":
            raise DomainError("holdout_failed", f"{cand.label()} {held.status} the {symbol} {tf} holdout on {_day(held.ts)}: the result is final", 409)
        active = self.repo.challengers(symbol, tf)
        if any(c.candidate_key == cand.key for c in active) or cand.key in self._submitted.get((symbol, tf), {}):
            raise DomainError("duplicate", "that Candidate is already a Challenger (or queued)", 409)
        if len(active) + len(self._submitted.get((symbol, tf), {})) >= MAX_CHALLENGERS:
            raise DomainError("full", f"{symbol} {tf} already has {MAX_CHALLENGERS} Challengers: submit again when one retires", 409)
        run = self.repo.add_run(symbol=symbol, timeframe=tf, trigger="reviewer", champion_key=champion.key,
                                note=f"reviewer: {note}"[:500], result={"candidate": cand.to_dict()})
        self._submitted.setdefault((symbol, tf), {})[cand.key] = run.id
        if self._queue is not None:
            self._queue.put_nowait((OPTIMIZE, next(self._seq), ("challenger", (symbol, tf, run.id, cand, note))))
        return {"run_id": run.id, "status": "queued", "candidate": cand.to_dict()}

    async def _run_submission(self, symbol: str, timeframe: str, run_id: int, cand: Candidate, note: str) -> None:
        found = self._arena_assignment(symbol, timeframe)
        if found is None:
            self.repo.update_run(run_id, status="failed", note="no session trades this arena any more", finished_wall=time.time())
            return
        s, a = found
        champion = self.champion_of(a)
        self.repo.update_run(run_id, status="running", started_wall=time.time())
        tf = Timeframe(timeframe)
        n_bars = int(self.store.app_settings()["learning_bars"])
        bars, tick, info, now = await self.broker.run(
            lambda b: (b.closed_bars(symbol, tf, n_bars), b.tick(symbol), b.symbol_info(symbol), b.server_time())
        )
        spec = self.spec_for(s, a)
        costs = CostModel.from_symbol(info, spread=self._typical_spread(symbol, tick, now), ref_price=float(bars["close"].iloc[-1]) if len(bars) else None)
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            self._pool, evaluate_job, bars, champion, cand, costs, spec.rules, spec.window, tf.seconds, spec.weekend_close or None, run_id
        )
        self._server_time = now
        if result.insufficient:
            self.repo.update_run(run_id, status="insufficient", note=result.insufficient, result=result.to_dict(), finished_wall=time.time())
            return
        if len(self.repo.challengers(symbol, timeframe)) >= MAX_CHALLENGERS:
            self.repo.update_run(run_id, status="failed", note="the Arena filled up with Challengers meanwhile", result=result.to_dict(), finished_wall=time.time())
            return
        judged = result.finalists[0]
        self.repo.ensure_candidate(cand)
        self.repo.add_challenger(
            symbol=symbol, timeframe=timeframe, candidate_key=cand.key, started_ts=now, run_id=run_id,
            oos=judged.wf.oos.to_dict(), champion_oos=result.champion.wf.oos.to_dict(),
            trials=max(self.repo.trial_count(symbol, timeframe), TOP_K), robust=judged.robust,
            note=f"submitted by the Strategy Review: {note}"[:500], source="reviewer",
        )
        verdict = "qualifies" if result.picks else "does not qualify on walk-forward yet"
        self.repo.update_run(run_id, status="done", note=f"reviewer Challenger {cand.label()} installed; {verdict}", result=result.to_dict(), finished_wall=time.time())
        self._j("learning", f"Strategy Review Challenger for {symbol} {timeframe}: {cand.label()} ({verdict})", symbol=symbol)

    def add_review(self, title: str, summary: str, report: str = "", arenas: list | None = None, finalists: list | None = None,
                   actions: list | None = None, period_from: int | None = None, period_to: int | None = None) -> dict:
        """Store a review report and send its summary to Telegram (one Alert)."""
        counts = self.repo.trial_counts()
        ledger = {f"{k[0]} {k[1]}": v for k, v in counts.items()}
        row = self.repo.add_review(
            title=title, summary=summary, report=report, arenas=arenas or [], finalists=finalists or [], actions=actions or [],
            period_from=period_from, period_to=period_to, ledger=ledger, ts=self._server_time or int(time.time()),
        )
        self._j("alert", f"Strategy Review: {title} — {summary}", level="info", alert=True, data={"review_id": row.id})
        return row.model_dump()

    async def scorecard(self) -> list[dict]:
        """Per Arena: the Paper Account's record against the band the backtest says it should fall in,
        and what one minimum lot would risk on the real Account. Advisory; it never blocks."""
        arenas: dict[tuple[str, str], tuple[SessionRow, AssignmentRow]] = {}
        for s in self.store.list_sessions():
            for a in self.store.assignments(s.id):
                cur = arenas.get((a.symbol, a.timeframe))
                rank = (s.execution == "paper", s.status in ACTIVE)
                if cur is None or rank > (cur[0].execution == "paper", cur[0].status in ACTIVE):
                    arenas[(a.symbol, a.timeframe)] = (s, a)
        out = []
        for (symbol, timeframe), (s, a) in sorted(arenas.items()):
            spec = self.spec_for(s, a)
            badge = self.evidence_for(spec)
            rs = self.repo.evidence_row(badge["evidence"]["id"]).rs if badge["status"] == "match" else []
            # only this parameter set's Paper trades: a Promotion starts a new record (trades recorded before the
            # parameter set was kept cannot be attributed and are counted separately)
            paper_rs = [self._trade_r(t) for t in self.store.paper_closed_trades(symbol, timeframe, a.strategy, candidate_key=spec.candidate.key)]
            unattributed = len(self.store.paper_closed_trades(symbol, timeframe, a.strategy, candidate_key=""))
            n = len(paper_rs)
            mean = sum(paper_rs) / n if n else 0.0
            rng = ev.band(rs, n) if rs and n else None
            strat = get_strategy(a.strategy)
            p = strat.resolve(a.params)
            tf = Timeframe(timeframe)
            stop, risk = None, None
            try:
                acct, info, bars = await self.broker.run(
                    lambda b, k=strat.lookback(p) + WARMUP_BARS: (b.account(), b.symbol_info(symbol), b.closed_bars(symbol, tf, k)),
                    timeout=ev.CHUNK_TIMEOUT,
                )
                if len(bars):
                    stop = float(strat.signals(bars, p)["sl_dist"].iloc[-1])
                    vpp = float(info.trade_tick_value) / float(info.trade_tick_size) if info.trade_tick_size else 0.0
                    risk = ev.min_lot_risk_pct(float(info.volume_min), stop, vpp, float(acct.balance))
            except BrokerError:
                pass
            out.append({
                "symbol": symbol, "timeframe": timeframe, "strategy": a.strategy,
                "session": {"id": s.id, "name": s.name, "status": s.status, "execution": s.execution},
                "evidence": badge,
                "paper": {"trades": n, "mean_r": round(mean, 4), "total_r": round(sum(paper_rs), 3), "epoch": self.store.paper_epoch(),
                          "candidate": spec.candidate.label(), "unattributed": unattributed},
                "band": list(rng) if rng else None,
                "band_level": ev.BAND_LEVEL,
                "verdict": self._verdict(badge["status"] == "match", rs, n, mean, rng),
                "min_lot_risk_pct": risk,
                "stop": stop if stop == stop else None,
                "note": f"counts this epoch's Paper trades of {spec.candidate.label()} only"
                        + (f"; {unattributed} earlier trades have no parameter record and are not counted" if unattributed else ""),
            })
        return out

    def expected_hold(self, symbol: str, timeframe: str, strategy: str, params: dict | None, every_night: bool = False) -> dict | None:
        """How many swap nights a trade of this Candidate typically pays, and how many of its trades are
        long: from its Evidence (the same Candidate, else the same Strategy on the Arena), else its closed
        Shadow Trades (``every_night``: the symbol is charged 7 nights a week). None while neither exists.
        Read by the Cost Check to show swap beside the cost."""
        from .costs import rollover_nights

        cand = Candidate.of(strategy, params)
        rows = [r for r in self.repo.evidence_rows(symbol=symbol, timeframe=timeframe, status=("done", "incomplete"), limit=50)
                if r.avg_nights is not None and r.trades]
        same = [r for r in rows if r.candidate_key == cand.key]
        other = [r for r in rows if r.strategy == strategy]
        for pick, source in ((same, "Evidence"), (other, f"Evidence of another {get_strategy(strategy).title} parameter set")):
            if pick:
                return {"nights": pick[0].avg_nights, "long_share": pick[0].long_share, "trades": pick[0].trades, "source": source}
        shadows = self.repo.shadow_closed(symbol, timeframe, cand.key, limit=500)
        shadows = [t for t in shadows if t.close_ts]
        if shadows:
            nights = [rollover_nights(t.open_ts, t.close_ts, every_night=every_night) for t in shadows]
            return {"nights": round(sum(nights) / len(nights), 3), "long_share": round(sum(t.side == "long" for t in shadows) / len(shadows), 3),
                    "trades": len(shadows), "source": "Shadow Trades"}
        return None

    @staticmethod
    def _verdict(matched: bool, rs: list, n: int, mean: float, rng) -> str:
        if not matched:
            return "no evidence"
        if not rs:
            return "the backtest took no trades"
        if n < ev.MIN_SCORECARD_TRADES:
            return "too few trades"
        return ev.verdict(n, mean, rng)

    @staticmethod
    def _trade_r(t: TradeRow) -> float:
        """A closed trade's result in R: profit over the money it risked (swap included); the price
        move over the initial stop when the risk was not recorded (an adopted Orphan)."""
        if t.risk_amount and t.risk_amount > 0 and t.profit is not None:
            return float(t.profit) / float(t.risk_amount)
        if t.initial_risk and t.close_price is not None:
            move = t.close_price - t.open_price if t.side == "long" else t.open_price - t.close_price
            return move / t.initial_risk
        return 0.0

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
        costs = CostModel.from_symbol(info, spread=self._typical_spread(symbol, tick, now), ref_price=float(bars["close"].iloc[-1]) if len(bars) else None)
        exclude = {c.candidate_key for c in self.repo.challengers(symbol, timeframe)} | {champion.key}
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(self._pool, optimize_job, *self._job_args(bars, champion, costs, rules, n_cands, run_id, window, tf, exclude,
                                                                                                  s.weekend_close_time if s.weekend_close else None))
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
        pend = self.repo.pending(s.id, a.symbol, a.timeframe)
        return {
            "symbol": a.symbol,
            "timeframe": a.timeframe,
            "session": {"id": s.id, "name": s.name, "status": s.status},
            "champion": {**champion.to_dict(), "shadow": champ_stats.to_dict()},
            "challengers": challengers,
            "filter": {k: v for k, v in fstate.to_dict().items() if k not in ("mean", "std")},
            "last_run": runs[0].model_dump(exclude={"result"}) if runs else None,
            "pending": pend.model_dump() if pend else None,
            "auto_promote": self.repo.slot(s.id, a.symbol, a.timeframe).auto_promote,
            "auto_promote_allowed": not is_live,
            "versions": len(self.repo.versions(s.id, a.symbol, a.timeframe)),
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


def _day(ts: int) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(ts))
