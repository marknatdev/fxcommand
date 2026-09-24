"""SessionManager: the engine core.

Small interface — create/update/delete, start/pause/resume/stop, kill_all,
boot, tick_once, snapshot — over everything that makes Sessions trade:

- per-Assignment bar-close evaluation (Strategy -> Risk Gate -> Broker)
- position management (breakeven / trailing) on every pass, even when paused
- ownership by Magic Number, re-adoption on start, reconciliation of closes
- daily-loss Auto-stop (per Session and global), Kill Switch
- Interrupted-on-boot

All state-changing work is serialised by one asyncio lock, so a command can
never interleave with an engine pass. All Broker calls go through
``BrokerThread``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import asdict, dataclass, field

from ..broker import BrokerError, BrokerThread, Position, Timeframe
from ..broker.types import AccountInfo
from ..journal import EventBus, Journal
from ..risk import Exposure, GateInput, Rejected, RiskLimits, TradingWindow, check, manage
from ..store import AssignmentRow, SessionRow, Store, TradeRow
from ..strategies import Signal, get_strategy
from ..strategies.base import WARMUP_BARS
from ..strategies.indicators import atr as atr_series
from .schemas import DomainError, SessionIn

log = logging.getLogger("fxcommand.engine")

ACTIVE = ("running", "paused")
DAY = 86400
MISSING_CLOSE_GIVE_UP = 60  # engine passes before an unexplained vanished position is closed as "unknown"


@dataclass
class AssignmentState:
    last_bar_time: int | None = None
    last_eval: int | None = None
    last_signal: dict | None = None
    atr: float = 0.0
    status: str = "idle"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class EngineStatus:
    connected: bool = False
    last_error: str = ""
    last_pass_wall: float = 0.0
    passes: int = 0
    kill_switch_at: int | None = None
    extra: dict = field(default_factory=dict)


class SessionManager:
    def __init__(self, broker: BrokerThread, store: Store, journal: Journal, bus: EventBus, learning=None):
        self.broker = broker
        self.learning = learning  # LearningService | None (ADR 0004); every call goes through _learn()
        self._learn_errors: set[str] = set()
        self.store = store
        self.journal = journal
        self.bus = bus
        self.status = EngineStatus()
        self._lock = asyncio.Lock()
        self._states: dict[int, AssignmentState] = {}
        self._server_time = 0
        self._account: AccountInfo | None = None
        self._positions: list[Position] = []
        self._missing: dict[int, int] = {}
        self._task: asyncio.Task | None = None

    # ================================================================ helpers
    def _learn(self, fn, default=None):
        """Run a learning hook. Learning must never stop trading: any failure is logged, journaled once
        per distinct message, and swallowed (it is also never mistaken for a Broker error)."""
        if self.learning is None:
            return default
        try:
            return fn(self.learning)
        except Exception as e:  # noqa: BLE001
            log.exception("learning hook failed")
            msg = f"{type(e).__name__}: {e}"
            if msg not in self._learn_errors:
                self._learn_errors.add(msg)
                self._j("learning", f"Learning error (trading continues): {msg}", level="error")
            return default

    @property
    def now(self) -> int:
        return self._server_time or int(time.time())

    def _j(self, kind: str, msg: str, s: SessionRow | None = None, symbol: str | None = None, **kw) -> dict:
        return self.journal.record(kind, msg, ts=self.now, session_id=s.id if s else None, symbol=symbol, **kw)

    def _day_start(self) -> tuple[int, float]:
        ds = self.store.get_setting("day_start") or {}
        equity = float(ds.get("equity") or (self._account.equity if self._account else 0.0))
        return int(ds.get("day", self.now // DAY)) * DAY, equity

    def _owned(self, magics: dict[int, int] | None = None) -> list[Position]:
        magics = magics if magics is not None else self.store.all_magics()
        return [p for p in self._positions if p.magic in magics]

    def _session_pnl(self, s: SessionRow, day_ts: int) -> float:
        floating = sum(p.profit for p in self._positions if p.magic == s.magic)
        return self.store.realized_since(day_ts, s.id) + floating

    def _global_pnl(self, day_ts: int, magics: dict[int, int]) -> float:
        return self.store.realized_since(day_ts) + sum(p.profit for p in self._owned(magics))

    # ============================================================= config API
    def _validate_spec(self, spec: SessionIn, session_id: int | None) -> list[AssignmentRow]:
        other = self.store.session_by_name(spec.name)
        if other is not None and other.id != session_id:
            raise DomainError("name_taken", f"a session named {spec.name!r} already exists")
        seen: set[str] = set()
        profiles = {p.id: p for p in self.store.risk_profiles()}
        default_profile = next(iter(profiles))
        rows = []
        for a in spec.assignments:
            if a.symbol in seen:
                raise DomainError("duplicate_symbol", f"{a.symbol} appears twice; a Symbol may appear only once per Session", 422)
            seen.add(a.symbol)
            try:
                strat = get_strategy(a.strategy)
            except KeyError as e:
                raise DomainError("unknown_strategy", str(e.args[0]), 422) from None
            pid = a.risk_profile_id or default_profile
            if pid not in profiles:
                raise DomainError("unknown_risk_profile", f"risk profile {pid} does not exist", 422)
            rows.append(
                AssignmentRow(
                    session_id=session_id or 0,
                    symbol=a.symbol,
                    timeframe=a.timeframe.value,
                    strategy=a.strategy,
                    params=strat.resolve(a.params),
                    risk_profile_id=pid,
                    reverse_on_opposite=a.reverse_on_opposite,
                    enabled=a.enabled,
                )
            )
        return rows

    def _window(self, spec: SessionIn) -> dict:
        raw = spec.window if spec.window is not None else self.store.app_settings()["default_window"]
        try:
            return TradingWindow.from_dict(raw).to_dict()
        except (ValueError, KeyError, TypeError) as e:
            raise DomainError("invalid_window", f"invalid trading window: {e}", 422) from None

    async def create_session(self, spec: SessionIn) -> SessionRow:
        async with self._lock:
            rows = self._validate_spec(spec, None)
            row = SessionRow(
                name=spec.name,
                notes=spec.notes,
                auto_resume=spec.auto_resume,
                max_positions=spec.max_positions,
                daily_loss_pct=spec.daily_loss_pct,
                window=self._window(spec),
                magic=self.store.allocate_magic(),
                created_at=time.time(),
            )
            row = self.store.save_session(row, rows)
            self._j("state", f"Session '{row.name}' created (magic {row.magic}, {len(rows)} assignments)", row)
            return row

    async def update_session(self, session_id: int, spec: SessionIn) -> SessionRow:
        async with self._lock:
            s = self.store.get_session(session_id)
            if s.status in ACTIVE:
                raise DomainError("session_active", "stop the session before editing it")
            rows = self._validate_spec(spec, session_id)
            s.name, s.notes, s.auto_resume = spec.name, spec.notes, spec.auto_resume
            s.max_positions, s.daily_loss_pct, s.window = spec.max_positions, spec.daily_loss_pct, self._window(spec)
            s = self.store.save_session(s, rows)
            for a in self.store.assignments(session_id):
                self._states.pop(a.id, None)
            self._j("state", f"Session '{s.name}' updated", s)
            return s

    async def delete_session(self, session_id: int) -> None:
        async with self._lock:
            s = self.store.get_session(session_id)
            if s.status in ACTIVE:
                raise DomainError("session_active", "stop the session before deleting it")
            if any(p.magic == s.magic for p in self._positions):
                raise DomainError("has_positions", "the session still has open positions; close them first")
            self.store.delete_session(session_id)
            self._j("state", f"Session '{s.name}' deleted")

    # ============================================================ lifecycle API
    async def start(self, session_id: int) -> SessionRow:
        async with self._lock:
            return await self._start(session_id)

    async def _start(self, session_id: int) -> SessionRow:
        s = self.store.get_session(session_id)
        if s.status in ACTIVE:
            raise DomainError("already_active", f"session is already {s.status}")
        assigns = [a for a in self.store.assignments(s.id) if a.enabled]
        if not assigns:
            raise DomainError("no_assignments", "session has no enabled assignments", 422)
        symbols = {a.symbol for a in assigns}
        for other in self.store.list_sessions():
            if other.id == s.id or other.status not in ACTIVE:
                continue
            clash = symbols & {a.symbol for a in self.store.assignments(other.id) if a.enabled}
            if clash:
                raise DomainError(
                    "symbol_conflict", f"{', '.join(sorted(clash))} already traded by running session '{other.name}'"
                )
        if not await self._ensure_connected():
            raise DomainError("broker_offline", f"broker not connected: {self.status.last_error}", 503)
        try:
            for sym in symbols:
                await self.broker.run(lambda b, sym=sym: b.symbol_info(sym))
            await self._refresh()
        except BrokerError as e:
            raise DomainError("symbol_unavailable", str(e), 422) from None
        acct = self._account
        if acct and not acct.is_demo and not self.store.live_enabled(acct.login):
            raise DomainError("live_blocked", f"account {acct.login} is LIVE and not live-enabled (Account page)")
        self._adopt(s, assigns)
        self._learn(lambda L: L.on_session_started(s, assigns, self.now))
        for a in assigns:
            self._states[a.id] = AssignmentState(status="waiting for first bar")
        s = self.store.update_session_fields(s.id, status="running", started_at=self.now, stopped_at=None, stop_reason="")
        self._j("state", f"Session '{s.name}' started — {len(assigns)} assignments: {', '.join(sorted(symbols))}", s)
        self._publish()
        return s

    def _adopt(self, s: SessionRow, assigns: list[AssignmentRow]) -> None:
        by_symbol = {a.symbol: a for a in assigns}
        for p in self._positions:
            if p.magic != s.magic or self.store.trade_by_ticket(p.ticket) is not None:
                continue
            a = by_symbol.get(p.symbol)
            self.store.add_trade(
                TradeRow(
                    ticket=p.ticket,
                    session_id=s.id,
                    assignment_id=a.id if a else None,
                    magic=p.magic,
                    symbol=p.symbol,
                    side=p.side,
                    volume=p.volume,
                    strategy=a.strategy if a else "",
                    timeframe=a.timeframe if a else "",
                    open_time=p.time,
                    open_price=p.price_open,
                    sl=p.sl,
                    tp=p.tp,
                    initial_risk=abs(p.price_open - p.sl) if p.sl else 0.0,
                    adopted=True,
                )
            )
            self._j("info", f"Re-adopted {p.side} {p.volume} {p.symbol} #{p.ticket}", s, p.symbol)

    async def pause(self, session_id: int) -> SessionRow:
        async with self._lock:
            s = self.store.get_session(session_id)
            if s.status != "running":
                raise DomainError("not_running", f"session is {s.status}, not running")
            s = self.store.update_session_fields(s.id, status="paused")
            self._j("state", f"Session '{s.name}' paused — no new entries, positions still managed", s)
            self._publish()
            return s

    async def resume(self, session_id: int) -> SessionRow:
        async with self._lock:
            s = self.store.get_session(session_id)
            if s.status != "paused":
                raise DomainError("not_paused", f"session is {s.status}, not paused")
            s = self.store.update_session_fields(s.id, status="running")
            self._j("state", f"Session '{s.name}' resumed", s)
            self._publish()
            return s

    async def stop(self, session_id: int, close_positions: bool = False) -> SessionRow:
        async with self._lock:
            s = self.store.get_session(session_id)
            if s.status not in (*ACTIVE, "interrupted"):
                raise DomainError("not_active", f"session is already {s.status}")
            if close_positions and not await self._ensure_connected():
                raise DomainError("broker_offline", "cannot close positions: broker not connected", 503)
            if close_positions:
                await self._refresh()
            s = await self._stop(s, close_positions, "stopped by operator")
            self._publish()
            return s

    async def _stop(self, s: SessionRow, close_positions: bool, reason: str, alert: bool = False) -> SessionRow:
        s = self.store.update_session_fields(s.id, status="stopped", stopped_at=self.now, stop_reason=reason)
        self._learn(lambda L: L.on_session_stopped(s))
        for a in self.store.assignments(s.id):
            self._states.pop(a.id, None)
        closed = 0
        if close_positions:
            closed = await self._close_all({s.magic}, reason)
        left = sum(1 for p in self._positions if p.magic == s.magic)
        tail = f"closed {closed} positions" if close_positions else f"left {left} positions open (server SL/TP protect them)"
        self._j("alert" if alert else "state", f"Session '{s.name}' stopped: {reason}; {tail}", s, level="warn" if alert else "info", alert=alert)
        return s

    async def kill_all(self) -> dict:
        async with self._lock:
            stopped = []
            for s in self.store.list_sessions():
                if s.status in (*ACTIVE, "interrupted"):
                    self.store.update_session_fields(s.id, status="stopped", stopped_at=self.now, stop_reason="kill switch")
                    stopped.append(s.name)
            self._states.clear()
            magics = set(self.store.all_magics())
            closed = 0
            if await self._ensure_connected():
                await self._refresh()
                closed = await self._close_all(magics, "kill switch")
            self.status.kill_switch_at = self.now
            self._j(
                "alert",
                f"KILL SWITCH: stopped {len(stopped)} sessions, closed {closed} positions",
                level="error",
                alert=True,
                data={"sessions": stopped, "closed": closed},
            )
            self._publish()
            return {"stopped_sessions": stopped, "closed_positions": closed}

    async def close_position(self, ticket: int) -> dict:
        async with self._lock:
            await self._refresh()
            magics = self.store.all_magics()
            p = next((p for p in self._positions if p.ticket == ticket), None)
            if p is None:
                raise DomainError("not_found", f"position {ticket} not found", 404)
            if p.magic not in magics:
                raise DomainError("foreign_position", "this position was not opened by FXCommand; it is never touched", 403)
            s = self.store.get_session(magics[p.magic])
            res = await self.broker.run(lambda b: b.close(ticket, "manual"))
            if not res.ok:
                self._j("order_fail", f"Manual close #{ticket} failed: {res.message} ({res.retcode})", s, p.symbol, level="error", alert=True)
                raise DomainError("close_failed", res.message, 502)
            self._j("close", f"Manual close requested for #{ticket} {p.symbol}", s, p.symbol)
            await self._refresh()
            await self._reconcile_closed()
            self._publish()
            return res.to_dict()

    async def boot(self) -> None:
        """App start: Sessions that were active become Interrupted; auto-resume ones restart."""
        async with self._lock:
            await self._ensure_connected()
            if self.status.connected:
                await self._refresh()
            resume = []
            for s in self.store.list_sessions():
                if s.status in ACTIVE:
                    self.store.update_session_fields(s.id, status="interrupted", stop_reason="application restarted")
                    self._j("alert", f"Session '{s.name}' was {s.status} when the app stopped — now Interrupted", s, level="warn", alert=True)
                    if s.auto_resume:
                        resume.append(s.id)
            for sid in resume:
                try:
                    await self._start(sid)
                except DomainError as e:
                    self._j("alert", f"Auto-resume failed: {e.message}", self.store.get_session(sid), level="error", alert=True)

    # ============================================================ engine pass
    async def run_forever(self, interval: callable) -> None:
        while True:
            try:
                await self.tick_once()
            except Exception:  # never let the loop die
                log.exception("engine pass failed")
            await asyncio.sleep(max(0.1, float(interval())))

    def start_loop(self, interval: callable) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self.run_forever(interval), name="engine")

    async def stop_loop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    async def tick_once(self) -> None:
        async with self._lock:
            if not await self._ensure_connected():
                self._publish()
                return
            try:
                await self._refresh()
                self._roll_day()
                await self._reconcile_closed()
                await self._enforce_daily_loss()
                for s in self.store.list_sessions():
                    if s.status in ACTIVE:
                        await self._process_session(s)
                self._snapshot_equity()
            except BrokerError as e:
                self.status.connected = False
                self.status.last_error = str(e)
                self._j("alert", f"Broker error: {e}", level="error", alert=True)
            self.status.passes += 1
            self.status.last_pass_wall = time.time()
            self._publish()

    async def _ensure_connected(self) -> bool:
        if self.status.connected:
            ok = await self.broker.run(lambda b: b.is_connected())
            if ok:
                return True
            self.status.connected = False
            self._j("alert", "Broker connection lost", level="error", alert=True)
        try:
            await self.broker.run(lambda b: b.connect())
        except BrokerError as e:
            if str(e) != self.status.last_error:
                self.status.last_error = str(e)
                self._j("alert", f"Cannot connect to broker: {e}", level="error", alert=True)
            return False
        self.status.connected = True
        self.status.last_error = ""
        await self._refresh()
        a = self._account
        self._j("info", f"Connected to {self.broker.mode.upper()} — account {a.login} @ {a.server} ({'demo' if a.is_demo else 'LIVE'})")
        return True

    async def _refresh(self) -> None:
        self._server_time, self._account, self._positions = await self.broker.run(
            lambda b: (b.server_time(), b.account(), b.positions())
        )

    def _roll_day(self) -> None:
        day = self.now // DAY
        ds = self.store.get_setting("day_start") or {}
        if ds.get("day") != day and self._account:
            self.store.set_setting("day_start", {"day": day, "equity": self._account.equity, "balance": self._account.balance})
            if ds:
                self._j("info", f"New trading day (server time) — start equity {self._account.equity:.2f}")
                self._learn(lambda L: L.on_new_day(self.now))

    async def _reconcile_closed(self) -> None:
        open_rows = self.store.open_trades()
        live = {p.ticket for p in self._positions}
        missing = [r for r in open_rows if r.ticket not in live]
        if not missing:
            return
        since = min(r.open_time for r in missing) - 60
        hist = {c.ticket: c for c in await self.broker.run(lambda b: b.history(since))}
        sessions = {s.id: s for s in self.store.list_sessions()}
        for r in missing:
            c = hist.get(r.ticket)
            if c is None:
                self._missing[r.ticket] = self._missing.get(r.ticket, 0) + 1
                if self._missing[r.ticket] < MISSING_CLOSE_GIVE_UP:
                    continue
                self.store.update_trade(r.ticket, status="closed", close_time=self.now, close_reason="unknown", profit=0.0)
                self._j("alert", f"#{r.ticket} vanished without a closing deal; marked closed", sessions.get(r.session_id), r.symbol, level="warn", alert=True)
                continue
            self._missing.pop(r.ticket, None)
            closed_row = self.store.update_trade(
                r.ticket, status="closed", close_time=c.time_close, close_price=c.price_close, profit=c.profit, close_reason=c.reason
            )
            self._learn(lambda L, row=closed_row: L.on_live_close(row, sessions.get(row.session_id)))
            r_mult = ""
            if r.initial_risk and r.volume:
                risk_money = r.risk_amount or 0
                if risk_money:
                    r_mult = f", {c.profit / risk_money:+.2f}R"
            self._j(
                "close",
                f"Closed {r.side} {r.volume} {r.symbol} #{r.ticket} @ {c.price_close} ({c.reason}) P&L {c.profit:+.2f}{r_mult}",
                sessions.get(r.session_id),
                r.symbol,
                level="info" if c.profit >= 0 else "warn",
                data={"ticket": r.ticket, "profit": c.profit, "reason": c.reason},
            )

    async def _enforce_daily_loss(self) -> None:
        active = [s for s in self.store.list_sessions() if s.status in ACTIVE]
        if not active:
            return
        day_ts, day_equity = self._day_start()
        if day_equity <= 0:
            return
        app = self.store.app_settings()
        close = bool(app["close_on_auto_stop"])
        magics = self.store.all_magics()
        limits = self.store.global_limits()
        g = self._global_pnl(day_ts, magics)
        g_cap = -limits.daily_loss_pct_global / 100 * day_equity
        if g <= g_cap:
            for s in active:
                await self._stop(s, close, f"AUTO-STOP: global daily loss {g:.2f} hit limit {g_cap:.2f}", alert=True)
            await self._refresh()
            return
        for s in active:
            pnl = self._session_pnl(s, day_ts)
            cap = -s.daily_loss_pct / 100 * day_equity
            if pnl <= cap:
                await self._stop(s, close, f"AUTO-STOP: session daily loss {pnl:.2f} hit limit {cap:.2f}", alert=True)
                await self._refresh()

    def _snapshot_equity(self) -> None:
        if not self._account:
            return
        every = int(self.store.app_settings()["equity_snapshot_seconds"])
        if self.now - self.store.last_equity_ts() >= every:
            self.store.add_equity(self.now, self._account.balance, self._account.equity)

    # ----------------------------------------------------------- per session
    async def _process_session(self, s: SessionRow) -> None:
        assigns = [a for a in self.store.assignments(s.id) if a.enabled]
        for a in assigns:
            try:
                await self._process_assignment(s, a)
            except BrokerError:
                raise
            except Exception as e:  # one bad assignment must not stop the others
                log.exception("assignment %s failed", a.id)
                self._states.setdefault(a.id, AssignmentState()).status = f"error: {e}"
        await self._manage_positions(s, assigns)

    async def _process_assignment(self, s: SessionRow, a: AssignmentRow) -> None:
        st = self._states.setdefault(a.id, AssignmentState())
        tf = Timeframe(a.timeframe)
        last = await self.broker.run(lambda b: b.closed_bars(a.symbol, tf, 1))
        if last.empty:
            st.status = "no bar data"
            return
        t = int(last["time"].iloc[-1])
        if st.last_bar_time == t:
            return
        first = st.last_bar_time is None
        st.last_bar_time = t
        a = await self._apply_pending(s, a)  # Promotions / Rollbacks take effect at a bar close, when flat
        strat = get_strategy(a.strategy)
        params = strat.resolve(a.params)
        need = max(strat.lookback(params) + WARMUP_BARS, self._learn(lambda L: L.bars_needed(a), 0))
        bars, tick, info = await self.broker.run(lambda b: (b.closed_bars(a.symbol, tf, need), b.tick(a.symbol), b.symbol_info(a.symbol)))
        a_series = atr_series(bars, params["atr_period"]) if len(bars) > params["atr_period"] else None
        st.atr = float(a_series.iloc[-1]) if a_series is not None else 0.0
        pos = next((p for p in self._positions if p.magic == s.magic and p.symbol == a.symbol), None)
        sig = strat.run(bars, params, pos.side if pos else None)
        st.last_eval = self.now
        st.last_signal = sig.to_dict()
        is_demo = bool(self._account and self._account.is_demo)
        self._learn(lambda L: L.on_bar(s, a, bars, tick, info, self.now, is_demo))  # Shadow Trades, even when paused
        if first:
            # never act on a bar that closed before the Session started
            st.status = "watching — acts from the next bar close"
            return
        st.status = f"evaluated {tf.value} bar {time.strftime('%H:%M', time.gmtime(t))}: {sig.action}"
        await self._act(s, a, sig, pos, bars, tick)

    async def _apply_pending(self, s: SessionRow, a: AssignmentRow) -> AssignmentRow:
        change = self._learn(lambda L: L.pending(s.id, a.symbol))
        if change is None:
            return a
        if change.kind == "auto_promotion":
            acct = await self.broker.run(lambda b: b.account())
            if not acct.is_demo:  # re-checked at the moment of applying: never promote automatically on live
                self._learn(lambda L: L.cancel(change, "live account"))
                self._j("learning", f"Automatic promotion for {a.symbol} cancelled: account is live", s, a.symbol, level="warn")
                return a
        # an auto_rollback is allowed on live: it only restores a Champion the operator already approved
        mine = await self.broker.run(lambda b: b.positions(s.magic))
        if any(p.symbol == a.symbol for p in mine):
            return a  # not flat yet: try again at the next bar close
        cand = self._learn(lambda L: L.apply(change, s, a, self.now))
        if cand is None:
            return a
        try:
            params = get_strategy(cand.strategy).resolve(cand.param_dict)
        except KeyError:
            return a
        return self.store.update_assignment(a.id, strategy=cand.strategy, params=params)

    async def _act(self, s: SessionRow, a: AssignmentRow, sig: Signal, pos: Position | None, bars=None, tick=None) -> None:
        if sig.action == "none":
            return
        self._j(
            "signal",
            f"{sig.action.upper()} signal ({a.strategy} {a.timeframe}): {sig.reason}",
            s,
            a.symbol,
            data={**sig.to_dict(), "strategy": a.strategy, "timeframe": a.timeframe},
        )
        if sig.action == "exit":
            if pos:
                await self._close(s, pos, f"strategy exit: {sig.reason}")
                await self._apply_pending(s, a)  # the old Champion just went flat
            return
        if pos is not None:
            if pos.side == sig.action:
                self._j("info", f"Already {pos.side} #{pos.ticket}; signal ignored", s, a.symbol)
                return
            await self._close(s, pos, "opposite signal")
            # an always-in-market Champion is only flat between closing and reversing: a pending
            # Promotion/Rollback takes over right here, and the new Champion decides from the next bar
            changed = await self._apply_pending(s, a)
            if (changed.strategy, changed.params) != (a.strategy, a.params):
                return
            if not a.reverse_on_opposite:
                return
        if s.status == "paused":
            self._j("info", "Session paused — entry skipped", s, a.symbol)
            return
        # the Signal Filter gates entries only (a reverse has already closed the old position above)
        screen = self._learn(lambda L: L.screen(s, a, sig, bars, tick)) if bars is not None and tick is not None else None
        if screen is not None and not screen.allowed:
            self._j("filter", f"{sig.action.upper()} blocked by Signal Filter: {screen.note}", s, a.symbol, level="warn", data={"p_win": screen.p_win})
            return
        await self._enter(s, a, sig, screen.record_id if screen else None)

    async def _enter(self, s: SessionRow, a: AssignmentRow, sig: Signal, record_id: int | None = None) -> None:
        side = sig.action
        sym = a.symbol
        # Everything that comes from our own database is read up front...
        magics = self.store.all_magics()
        day_ts, day_equity = self._day_start()
        realized_session = self.store.realized_since(day_ts, s.id)
        realized_global = self.store.realized_since(day_ts)
        profile = self.store.to_profile(self.store.risk_profile(a.risk_profile_id))
        global_limits = self.store.global_limits()
        live_flags = self.store.get_setting("live_enabled", {}) or {}
        session_limits = RiskLimits(max_positions_session=s.max_positions, daily_loss_pct_session=s.daily_loss_pct)
        window = TradingWindow.from_dict(s.window)
        comment = f"fxc s{s.id} {a.strategy}"

        def gate_and_send(b):
            # ...and the market-dependent part runs as ONE broker-thread call, so the tick the
            # Risk Gate priced the stops against is the tick the order is sent at.
            info, tick, acct, positions = b.symbol_info(sym), b.tick(sym), b.account(), b.positions()
            owned = [p for p in positions if p.magic in magics]
            mine = [p for p in owned if p.magic == s.magic]
            exposure = Exposure(
                session_positions=len(mine),
                global_positions=len(owned),
                session_day_pnl=realized_session + sum(p.profit for p in mine),
                global_day_pnl=realized_global + sum(p.profit for p in owned),
                day_start_equity=day_equity or acct.equity,
            )
            decision = check(
                GateInput(
                    symbol=info,
                    tick=tick,
                    side=side,  # type: ignore[arg-type]
                    sl_dist=sig.sl_dist,
                    tp_dist=sig.tp_dist,
                    account=acct,
                    live_enabled=bool(live_flags.get(str(acct.login), False)),
                    profile=profile,
                    session_limits=session_limits,
                    global_limits=global_limits,
                    exposure=exposure,
                    window=window,
                    now=self.now,
                )
            )
            if isinstance(decision, Rejected):
                return decision, None, acct, positions
            res = b.market_order(sym, side, decision.volume, decision.sl, decision.tp, s.magic, comment)
            return decision, res, acct, (b.positions() if res.ok else positions)

        decision, res, acct, self._positions = await self.broker.run(gate_and_send)
        self._account = acct
        if isinstance(decision, Rejected):
            self._j("risk_reject", f"{side.upper()} rejected by Risk Gate: {decision.reason}", s, sym, level="warn", data=decision.to_dict())
            self._learn(lambda L: L.signal_outcome(record_id, rejected=True))
            return
        if not res.ok:
            self._learn(lambda L: L.signal_outcome(record_id, rejected=True))
            self._j("order_fail", f"{side.upper()} {decision.volume} {sym} failed: {res.message} ({res.retcode})", s, sym, level="error", alert=True, data=res.to_dict())
            return
        self.store.add_trade(
            TradeRow(
                ticket=res.ticket,
                session_id=s.id,
                assignment_id=a.id,
                magic=s.magic,
                symbol=sym,
                side=side,
                volume=res.volume or decision.volume,
                strategy=a.strategy,
                timeframe=a.timeframe,
                open_time=self.now,
                open_price=res.price,
                sl=decision.sl,
                tp=decision.tp,
                initial_risk=abs(res.price - decision.sl),
                risk_amount=decision.risk_amount,
                signal_reason=sig.reason,
            )
        )
        self._learn(lambda L: L.signal_outcome(record_id, ticket=res.ticket))
        self._j(
            "order",
            f"Opened {side} {res.volume} {sym} #{res.ticket} @ {res.price} SL {decision.sl} TP {decision.tp or '—'} (risk {decision.risk_amount:.2f} {acct.currency})",
            s,
            sym,
            data={**decision.to_dict(), "ticket": res.ticket, "price": res.price},
        )

    async def _close(self, s: SessionRow, pos: Position, reason: str) -> bool:
        res = await self.broker.run(lambda b: b.close(pos.ticket, reason))
        if not res.ok:
            self._j("order_fail", f"Close #{pos.ticket} failed: {res.message} ({res.retcode})", s, pos.symbol, level="error", alert=True)
            return False
        self._j("info", f"Closing #{pos.ticket} {pos.symbol}: {reason}", s, pos.symbol)
        await self._refresh()
        await self._reconcile_closed()
        return True

    async def _close_all(self, magics: set[int], reason: str) -> int:
        closed = 0
        sessions = {s.magic: s for s in self.store.list_sessions()}
        for p in [p for p in self._positions if p.magic in magics]:
            res = await self.broker.run(lambda b, t=p.ticket: b.close(t, reason))
            if res.ok:
                closed += 1
            else:
                self._j("order_fail", f"Close #{p.ticket} failed: {res.message}", sessions.get(p.magic), p.symbol, level="error", alert=True)
        await self._refresh()
        await self._reconcile_closed()
        return closed

    async def _manage_positions(self, s: SessionRow, assigns: list[AssignmentRow]) -> None:
        by_symbol = {a.symbol: a for a in assigns}
        for p in [p for p in self._positions if p.magic == s.magic]:
            a = by_symbol.get(p.symbol)
            if a is None:
                continue
            profile = self.store.to_profile(self.store.risk_profile(a.risk_profile_id))
            if not (profile.breakeven or profile.trailing):
                continue
            st = self._states.get(a.id)
            tr = self.store.trade_by_ticket(p.ticket)
            initial = tr.initial_risk if tr and tr.initial_risk else abs(p.price_open - p.sl)
            atr = st.atr if st else 0.0

            def manage_and_modify(b, p=p, initial=initial, profile=profile, atr=atr):
                new_sl = manage(p, b.tick(p.symbol), b.symbol_info(p.symbol), atr, initial, profile)
                return new_sl, (b.modify(p.ticket, new_sl, p.tp) if new_sl is not None else None)

            new_sl, res = await self.broker.run(manage_and_modify)
            if res is None:
                continue
            if res.ok:
                if tr:
                    self.store.update_trade(p.ticket, sl=new_sl)
                self._j("modify", f"Moved SL of #{p.ticket} {p.symbol} {p.sl} → {new_sl}", s, p.symbol, data={"ticket": p.ticket, "old": p.sl, "new": new_sl})
            else:
                self._j("order_fail", f"SL move #{p.ticket} failed: {res.message}", s, p.symbol, level="warn")

    # ============================================================== read side
    def assignment_states(self, session_id: int) -> dict[int, dict]:
        return {a.id: (self._states.get(a.id) or AssignmentState()).to_dict() for a in self.store.assignments(session_id)}

    def positions_view(self) -> list[dict]:
        magics = self.store.all_magics()
        names = {s.id: s.name for s in self.store.list_sessions()}
        out = []
        for p in self._positions:
            sid = magics.get(p.magic)
            tr = self.store.trade_by_ticket(p.ticket) if sid else None
            out.append(
                {
                    **p.to_dict(),
                    "owned": sid is not None,
                    "session_id": sid,
                    "session_name": names.get(sid),
                    "strategy": tr.strategy if tr else None,
                    "timeframe": tr.timeframe if tr else None,
                    "risk_amount": tr.risk_amount if tr else None,
                }
            )
        return out

    def snapshot(self) -> dict:
        day_ts, day_equity = self._day_start()
        magics = self.store.all_magics()
        sessions = []
        for s in self.store.list_sessions():
            sessions.append(
                {
                    "id": s.id,
                    "name": s.name,
                    "status": s.status,
                    "magic": s.magic,
                    "open_positions": sum(1 for p in self._positions if p.magic == s.magic),
                    "day_pnl": round(self._session_pnl(s, day_ts), 2),
                    "stop_reason": s.stop_reason,
                }
            )
        acct = self._account
        return {
            "mode": self.broker.mode,
            "connected": self.status.connected,
            "last_error": self.status.last_error,
            "server_time": self.now,
            "account": acct.to_dict() if acct else None,
            "live_enabled": self.store.live_enabled(acct.login) if acct else False,
            "day_start": day_ts,
            "day_start_equity": day_equity,
            "day_pnl": round(self._global_pnl(day_ts, magics), 2),
            "sessions": sessions,
            "positions": self.positions_view(),
            "kill_switch_at": self.status.kill_switch_at,
            "passes": self.status.passes,
        }

    def _publish(self) -> None:
        try:
            self.bus.publish("snapshot", self.snapshot())
        except Exception:  # pragma: no cover
            log.exception("snapshot publish failed")
