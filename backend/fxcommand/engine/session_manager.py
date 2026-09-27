"""SessionManager: the engine core.

Small interface — create/update/delete, start/pause/resume/stop, kill_all,
boot, tick_once, snapshot — over everything that makes Sessions trade:

- per-Assignment bar-close evaluation (Strategy -> Risk Gate -> Broker)
- position management (breakeven / trailing) on every pass, even when paused
- ownership by Magic Number, re-adoption on start, reconciliation of closes
- daily-loss Auto-stop (per Session and global), Kill Switch
- Interrupted-on-boot
- going live (ADR 0006/0007): Order Outcomes and Orphan adoption, close-until-confirmed,
  Pinned Login, hedging-only, Live Caps, Equity Floor, Weekend Close, Paper Execution Mode,
  Pre-flight Check

All state-changing work is serialised by one asyncio lock, so a command can
never interleave with an engine pass. All Broker calls go through
``BrokerThread``.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import time
from dataclasses import asdict, dataclass, field

from ..broker import BrokerError, BrokerThread, BrokerTimeout, OrderResult, Position, Timeframe
from ..broker.paper import DEFERRABLE_REASONS
from ..broker.types import AccountInfo
from ..journal import EventBus, Journal
from ..tasks import cancel_and_wait
from ..risk import Exposure, GateInput, LiveCaps, Rejected, RiskLimits, TradingWindow, check, check_margin, manage
from ..learning.costs import CostModel
from ..risk.costcheck import CostEstimate, estimate_cost
from ..risk.gate import MAX_QUOTE_AGE
from ..risk.spreads import SpreadBook
from ..risk.window import fill_limit, in_weekend_close, weekday  # noqa: F401 (re-exported)
from ..store import AssignmentRow, SessionRow, Store, TradeRow
from ..store.models import PendingEntryRow
from ..strategies import Signal, get_strategy
from ..strategies.base import WARMUP_BARS
from ..strategies.indicators import atr as atr_series
from . import preflight as pf
from .schemas import DomainError, SessionIn

log = logging.getLogger("fxcommand.engine")

ACTIVE = ("running", "paused")
DAY = 86400
MISSING_CLOSE_GIVE_UP = 60  # engine passes before an unexplained vanished position is closed as "unknown"
RETRY_ONCE = {10004, 10018, 10019, 10020, 10021}  # requote, market closed, no money, price changed, price off
DISCONNECT_NOTIFY_SECONDS = 60
MARKET_CLOSED = 10018
# Risk Gate rejects that clear by themselves: the entry becomes a Pending Entry (spec D5/D13/D20)
TRANSIENT_REJECTS = {"outside_window", "stale_quote", "spread"}
PENDING_MAX_WAIT = 4 * DAY  # a Pending Entry that never saw the market open (a long closure) expires anyway
DEFERRED = "deferred: market closed"  # _close_confirmed's failure note for a close the market refused
DEFAULT_FLOOR_PCT = 80.0


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
    disconnected_since: float | None = None  # wall time the Broker went away
    disconnect_notified: bool = False
    extra: dict = field(default_factory=dict)


def _order_in_flight(fn):
    """Count the engine's order paths while they run: an Evidence Run reads history only between them
    (spec D24: a long history read must never delay an entry or a close into an uncertain outcome)."""

    @functools.wraps(fn)
    async def wrapper(self, *args, **kwargs):
        self._orders_in_flight += 1
        try:
            return await fn(self, *args, **kwargs)
        finally:
            self._orders_in_flight -= 1

    return wrapper


class SessionManager:
    def __init__(self, broker: BrokerThread, store: Store, journal: Journal, bus: EventBus, learning=None):
        self.broker = broker
        self._orders_in_flight = 0
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
        self._modify_failed: dict[int, str] = {}  # ticket -> last failed SL move, journaled once
        self.spreads = SpreadBook(store)  # typical spreads for the Cost Check (never one quote from the break)
        self._task: asyncio.Task | None = None
        self._kill_seq = 0  # bumped by the Kill Switch; an entry prepared before it is refused
        self.close_attempts = 5
        self.close_retry_delay = 1.0  # seconds between close attempts
        self.on_activity = None  # callable(bool any_session_active) — keeps the PC awake in the app
        self.notifier_configured = lambda: False  # set by the app (Pre-flight Check)

    # ================================================================ helpers
    @property
    def learning(self):
        return self._learning

    @learning.setter
    def learning(self, service) -> None:
        self._learning = service
        if service is not None:  # its Evidence Runs read history only while no order is in flight
            service.order_busy = lambda: self._orders_in_flight > 0

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

    @property
    def _login(self) -> int | None:
        return self._account.login if self._account else None

    def _mine(self, login: int | None) -> bool:
        """Does a trade recorded under ``login`` belong to the connected Account? (None = legacy row)"""
        return login is None or self._login is None or login == self._login

    def _ds_key(self) -> str:
        # the trading day's start equity is per Account: switching accounts must not mix them
        return f"day_start:{self._login}" if self._login is not None else "day_start"

    def _day_start(self) -> tuple[int, float]:
        ds = self.store.get_setting(self._ds_key()) or {}
        equity = float(ds.get("equity") or (self._account.equity if self._account else 0.0))
        return int(ds.get("day", self.now // DAY)) * DAY, equity

    # ---------------------------------------------------------- Paper Account (D8/D19)
    def _paper_account(self) -> AccountInfo:
        """The notional pool every Paper Session sizes from. Built from the database and the open Paper
        Positions; used only for the Risk Gate's sizing and limits — never stored in ``self._account``,
        so the real Equity Floor, day start and equity snapshots never see it."""
        cfg = self.store.paper_account()
        balance = float(cfg["start_balance"]) + self.store.realized_paper(epoch=int(cfg["epoch"]))
        equity = balance + sum(p.profit for p in self._positions if p.ticket < 0)
        real = self._account
        return AccountInfo(
            login=real.login if real else 0, name="Paper Account", server=real.server if real else "", company=real.company if real else "",
            currency=real.currency if real else "USD", balance=round(balance, 2), equity=round(equity, 2), margin=0.0, margin_free=round(equity, 2),
            leverage=real.leverage if real else 100, is_demo=True, trade_allowed=True, margin_mode="hedging",
        )

    def _paper_day_start(self) -> tuple[int, float]:
        ds = self.store.get_setting("day_start:paper") or {}
        equity = float(ds.get("equity") or self._paper_account().equity)
        return int(ds.get("day", self.now // DAY)) * DAY, equity

    def _paper_global_pnl(self, day_ts: int) -> float:
        return self.store.realized_paper(since=day_ts) + sum(p.profit for p in self._positions if p.ticket < 0)

    def paper_account_view(self) -> dict:
        cfg = self.store.paper_account()
        acct = self._paper_account()
        _, day_equity = self._paper_day_start()
        return {
            "start_balance": float(cfg["start_balance"]), "epoch": int(cfg["epoch"]), "currency": acct.currency,
            "balance": acct.balance, "equity": acct.equity, "open_pnl": round(acct.equity - acct.balance, 2),
            "realized": round(acct.balance - float(cfg["start_balance"]), 2),
            "day_start_equity": round(day_equity, 2), "day_pnl": round(acct.equity - day_equity, 2),
            "open_positions": sum(1 for p in self._positions if p.ticket < 0),
            "active_sessions": [s.name for s in self.store.list_sessions() if s.execution == "paper" and s.status in ACTIVE],
        }

    def _paper_idle(self) -> None:
        active = [s.name for s in self.store.list_sessions() if s.execution == "paper" and s.status in ACTIVE]
        if active:
            raise DomainError("paper_active", f"stop the Paper Sessions first: {', '.join(active)}")
        if self.store.paper_open():
            raise DomainError("paper_open", "close the open Paper Positions first")

    async def set_paper_start_balance(self, start_balance: float) -> dict:
        """Change the notional start balance within the epoch. Same guards as a reset, and the Paper day
        start moves with it (otherwise today's loss limit would see a phantom loss or gain)."""
        async with self._lock:
            if not start_balance > 0:
                raise DomainError("invalid", "the start balance must be positive", 422)
            self._paper_idle()
            cfg = self.store.set_paper_account(start_balance=float(start_balance))
            equity = self._paper_account().equity
            self.store.set_setting("day_start:paper", {"day": self.now // DAY, "equity": equity})
            self._j("state", f"Paper Account start balance set to {start_balance:.2f} (equity {equity:.2f})", level="warn")
            return cfg

    async def reset_paper_account(self, confirm: str, start_balance: float | None = None) -> dict:
        """Start a new Paper Account epoch. Typed confirmation; refused while a Paper Session is active
        or a Paper Position is open. Old rows stay (archived by epoch); tickets are never reused."""
        async with self._lock:
            if confirm != "RESET PAPER":
                raise DomainError("confirm_required", "type RESET PAPER to reset the Paper Account", 422)
            self._paper_idle()
            cfg = self.store.paper_account()
            start = float(start_balance) if start_balance is not None else float(cfg["start_balance"])
            if not start > 0:
                raise DomainError("invalid", "the start balance must be positive", 422)
            cfg = self.store.set_paper_account(start_balance=start, epoch=int(cfg["epoch"]) + 1)
            self.store.set_setting("day_start:paper", {"day": self.now // DAY, "equity": start})
            self._j("alert", f"Paper Account reset: epoch {cfg['epoch']}, balance {start:.2f} (earlier Paper trades archived)", level="warn", alert=True)
            return cfg

    def _owned(self, magics: dict[int, int] | None = None) -> list[Position]:
        """Owned positions on the Account (Paper Positions excluded: they never touch the Account)."""
        magics = magics if magics is not None else self.store.all_magics()
        return [p for p in self._positions if p.magic in magics and p.ticket > 0]

    def _session_pnl(self, s: SessionRow, day_ts: int) -> float:
        magics = self.store.session_magics(s.id)
        floating = sum(p.profit for p in self._positions if p.magic in magics)
        return self.store.realized_since(day_ts, s.id, self._login) + floating

    def _session_positions(self, s: SessionRow, positions: list[Position] | None = None) -> list[Position]:
        """Every position of the Session, whichever of its Assignment magics it carries (ADR 0009)."""
        magics = self.store.session_magics(s.id)
        return [p for p in (self._positions if positions is None else positions) if p.magic in magics]

    def _owner(self, s: SessionRow, p: Position, assigns: list[AssignmentRow], idents: dict | None = None) -> AssignmentRow | None:
        """The Assignment of ``s`` that manages position ``p`` (ADR 0009):
        1. the Assignment whose magic ``p`` carries, if the Symbol matches;
        2. else the Assignment in the Arena of the identity ``p``'s magic belongs to (the strategy
           changed by an edit or a Promotion, the position stayed);
        3. else, for a position carrying the Session's own magic with another Symbol (opened before
           magics were per Assignment), the Session's first Assignment on that Symbol."""
        same = [a for a in assigns if a.symbol == p.symbol]
        if not same:
            return None
        for a in same:
            if a.magic == p.magic:
                return a
        ident = (self.store.magic_identities() if idents is None else idents).get(p.magic)
        if ident is not None and ident.session_id == s.id and ident.symbol == p.symbol:
            return next((a for a in same if a.timeframe == ident.timeframe), None)
        if p.magic == s.magic:
            return same[0]
        return None

    def _position_of(self, s: SessionRow, a: AssignmentRow, positions: list[Position] | None = None) -> Position | None:
        assigns = self.store.assignments(s.id)
        idents = self.store.magic_identities()
        for p in self._session_positions(s, positions):
            o = self._owner(s, p, assigns, idents)
            if o is not None and o.id == a.id:
                return p
        return None

    def _global_pnl(self, day_ts: int, magics: dict[int, int]) -> float:
        return self.store.realized_since(day_ts, login=self._login) + sum(p.profit for p in self._owned(magics))

    def _sync_paper(self) -> None:
        """Tell the PaperRouter which Magic Numbers are Paper Sessions (ADR 0007)."""
        router = self.broker.broker
        if hasattr(router, "paper_magics"):
            router.paper_magics = set().union(*(self.store.session_magics(s.id) for s in self.store.list_sessions() if s.execution == "paper"))

    def _has_paper(self) -> bool:
        return hasattr(self.broker.broker, "paper_magics")

    def _is_live(self) -> bool:
        return bool(self._account and not self._account.is_demo)

    # ============================================================= config API
    def _validate_spec(self, spec: SessionIn, session_id: int | None) -> list[AssignmentRow]:
        other = self.store.session_by_name(spec.name)
        if other is not None and other.id != session_id:
            raise DomainError("name_taken", f"a session named {spec.name!r} already exists")
        seen: set[tuple[str, str]] = set()
        profiles = {p.id: p for p in self.store.risk_profiles()}
        default_profile = next(iter(profiles))
        rows = []
        for a in spec.assignments:
            arena = (a.symbol, a.timeframe.value)
            if arena in seen:
                raise DomainError(
                    "duplicate_symbol", f"{a.symbol} {a.timeframe.value} appears twice; a Symbol may appear once per Timeframe in a Session", 422
                )
            seen.add(arena)
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

    def _check_execution(self, spec: SessionIn) -> None:
        if spec.execution == "paper" and not self._has_paper():
            raise DomainError("paper_unavailable", "Paper execution is not available in this process", 422)

    async def create_session(self, spec: SessionIn) -> SessionRow:
        async with self._lock:
            rows = self._validate_spec(spec, None)
            self._check_execution(spec)
            row = SessionRow(
                name=spec.name,
                notes=spec.notes,
                auto_resume=spec.auto_resume,
                max_positions=spec.max_positions,
                daily_loss_pct=spec.daily_loss_pct,
                window=self._window(spec),
                magic=self.store.allocate_magic(),
                created_at=time.time(),
                execution=spec.execution,
                weekend_close=spec.weekend_close,
                weekend_close_time=spec.weekend_close_time,
            )
            self._check_cost_overrides(spec)
            row = self.store.save_session(row, rows)
            self._sync_paper()
            self._sync_cost_overrides(row, spec.cost_overrides)
            mode = " — PAPER" if row.execution == "paper" else ""
            magics = ", ".join(str(a.magic) for a in self.store.assignments(row.id))
            self._j("state", f"Session '{row.name}' created (magics {magics}, {len(rows)} assignments){mode}", row)
            return row

    async def update_session(self, session_id: int, spec: SessionIn) -> SessionRow:
        async with self._lock:
            s = self.store.get_session(session_id)
            if s.status in ACTIVE:
                raise DomainError("session_active", "stop the session before editing it")
            rows = self._validate_spec(spec, session_id)
            self._check_execution(spec)
            self._check_cost_overrides(spec)
            if spec.execution != s.execution:
                if self._session_positions(s):
                    raise DomainError("has_positions", "close the session's positions before changing its execution mode")
                if spec.execution == "broker" and self._is_live() and spec.confirm_login != self._account.login:
                    raise DomainError(
                        "confirm_required", f"type the account number {self._account.login} to switch this session to Broker execution on a LIVE account", 422
                    )
            s.name, s.notes, s.auto_resume = spec.name, spec.notes, spec.auto_resume
            s.max_positions, s.daily_loss_pct, s.window = spec.max_positions, spec.daily_loss_pct, self._window(spec)
            s.execution, s.weekend_close, s.weekend_close_time = spec.execution, spec.weekend_close, spec.weekend_close_time
            s = self.store.save_session(s, rows)
            self._sync_paper()
            self._sync_cost_overrides(s, spec.cost_overrides)
            for a in self.store.assignments(session_id):
                self._states.pop(a.id, None)
            self._j("state", f"Session '{s.name}' updated", s)
            return s

    async def delete_session(self, session_id: int) -> None:
        async with self._lock:
            s = self.store.get_session(session_id)
            if s.status in ACTIVE:
                raise DomainError("session_active", "stop the session before deleting it")
            if self._session_positions(s):
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
        arenas = {(a.symbol, a.timeframe) for a in assigns}
        for other in self.store.list_sessions():
            if other.id == s.id or other.status not in ACTIVE:
                continue
            clash = arenas & {(a.symbol, a.timeframe) for a in self.store.assignments(other.id) if a.enabled}
            if clash:
                raise DomainError(
                    "symbol_conflict",
                    f"{', '.join(f'{sym} {tf}' for sym, tf in sorted(clash))} already traded by running session '{other.name}'",
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
        paper = s.execution == "paper"
        if paper and not self._has_paper():
            raise DomainError("paper_unavailable", "Paper execution is not available in this process", 422)
        if acct and acct.margin_mode != "hedging":
            raise DomainError("netting_account", f"account {acct.login} uses {acct.margin_mode} margin mode; FXCommand trades hedging accounts only (ADR 0006)", 422)
        if acct and not acct.is_demo and not paper:
            if not self.store.live_enabled(acct.login):
                raise DomainError("live_blocked", f"account {acct.login} is LIVE and not live-enabled (Account page)")
            floor = self.store.equity_floor(acct.login)
            if floor is None:
                self._ensure_floor(acct)
            elif floor.get("breached_at"):
                raise DomainError("equity_floor", f"the Equity Floor ({floor['floor']:.2f}) was breached; reset it on the Risk page first")
            report = await self._preflight(s)
            if not report["ok"]:
                raise DomainError("preflight_failed", "Pre-flight Check failed: " + "; ".join(report["failed"]), 422)
        costs = await self.cost_check(s, assigns)  # enforced here, so neither the API nor MCP can skip it
        blocked = [c for c in costs if not c["allowed"]]
        if blocked:
            raise DomainError("cost_check", "Cost Check failed: " + "; ".join(f"{c['symbol']} {c['timeframe']} {c['reason']}" for c in blocked), 422)
        for c in costs:
            if c["blocked"] and c["override"]:
                self._j("state", f"Cost Check override used for {c['symbol']} {c['timeframe']}: {c['reason']}", s, c["symbol"], level="warn")
        self._sync_paper()
        await self._adopt_orphans(announce=False)
        self._learn(lambda L: L.on_session_started(s, assigns, self.now))
        for a in assigns:
            self._states[a.id] = AssignmentState(status="waiting for first bar")
        s = self.store.update_session_fields(
            s.id, status="running", started_at=self.now, stopped_at=None, stop_reason="", login=acct.login if acct else None
        )
        mode = "PAPER" if paper else ("LIVE" if acct and not acct.is_demo else "demo")
        self._j("state", f"Session '{s.name}' started ({mode}, account {s.login}) — {len(assigns)} assignments: {', '.join(sorted(symbols))}", s)
        self._publish()
        return s

    # ------------------------------------------------------------- Cost Check (D4/D14/D26)
    async def cost_check(self, s: SessionRow, assigns: list[AssignmentRow] | None = None, strategy: str | None = None, params: dict | None = None) -> list[dict]:
        """Spread + slippage as R of each Assignment's typical stop (its Strategy's stop on the latest
        bar), priced at the typical spread. ``strategy``/``params`` judge a Candidate instead (a
        Promotion). Each result says whether it blocks, and whether an override lets it start."""
        threshold = float(self.store.app_settings()["cost_check_max_r"])
        out = []
        for a in assigns if assigns is not None else [x for x in self.store.assignments(s.id) if x.enabled]:
            key = strategy or a.strategy
            strat = get_strategy(key)
            p = strat.resolve(params if params is not None else a.params)
            tf = Timeframe(a.timeframe)
            bars, tick, info = await self.broker.run(
                lambda b, a=a, n=strat.lookback(p) + WARMUP_BARS: (b.closed_bars(a.symbol, tf, n), b.tick(a.symbol), b.symbol_info(a.symbol))
            )
            spread = self.spreads.spread_for(a.symbol, tick, self.now)
            stop = float(strat.signals(bars, p)["sl_dist"].iloc[-1]) if len(bars) else 0.0
            # swap is shown beside the cost, never blocks (D38): nights per trade from Evidence or Shadow Trades
            hold = self._learn(lambda L, a=a, key=key, p=p: L.expected_hold(a.symbol, a.timeframe, key, p))
            swap = 0.0
            if hold:
                cm = CostModel.from_symbol(info, spread=spread or 0.0)
                long = hold["long_share"] if hold["long_share"] is not None else 1.0
                per_night = long * cm.swap_per_night("long", tick.bid) + (1 - long) * cm.swap_per_night("short", tick.bid)
                swap = per_night * hold["nights"]
            if spread is None:
                est = CostEstimate(float("inf"), 0.0, threshold, True, "no typical spread known yet and the market is shut: start when it is open")
            else:
                est = estimate_cost(spread, info.point, stop if stop == stop else 0.0, threshold, swap=swap)
            override = self.store.cost_override(s.id, a.symbol, a.timeframe) if s is not None else None
            out.append({
                "symbol": a.symbol, "timeframe": a.timeframe, "strategy": key, **est.to_dict(), "spread": spread, "stop": stop,
                "override": override is not None, "allowed": not est.blocked or override is not None,
                "swap_nights": hold["nights"] if hold else None, "swap_source": hold["source"] if hold else None,
            })
        return out

    async def cost_check_preview(self, session_id: int | None, assignments: list) -> list[dict]:
        """The Cost Check for an editor's unsaved Assignments (``AssignmentIn``), with the saved Session's
        overrides. Read-only; one result per Assignment, an ``error`` where it cannot be priced."""
        s = self.store.get_session(session_id) if session_id is not None else None
        out = []
        for a in assignments:
            tf = Timeframe(a.timeframe).value
            row = AssignmentRow(session_id=session_id or 0, symbol=a.symbol, timeframe=tf, strategy=a.strategy, params=a.params, risk_profile_id=0)
            try:
                out.extend(await self.cost_check(s, [row]))
            except (KeyError, BrokerError, ValueError) as e:
                out.append({"symbol": a.symbol, "timeframe": tf, "strategy": a.strategy, "error": str(e.args[0] if e.args else e), "allowed": False, "override": False})
        return out

    @staticmethod
    def _check_cost_overrides(spec: SessionIn) -> None:
        if spec.cost_overrides is None:
            return
        arenas = {(a.symbol, Timeframe(a.timeframe).value) for a in spec.assignments}
        for o in spec.cost_overrides:
            if (o.symbol, o.timeframe.value) not in arenas:
                raise DomainError("invalid_override", f"a Cost Check override for {o.symbol} {o.timeframe.value} needs that Assignment", 422)

    def _sync_cost_overrides(self, s: SessionRow, wanted: list | None) -> None:
        """Keep overrides only for the Session's Arenas; with ``wanted`` (the editor's full set) add and
        remove to match it. Every change is journaled."""
        arenas = {(a.symbol, a.timeframe) for a in self.store.assignments(s.id)}
        current = {(o.symbol, o.timeframe): o for o in self.store.cost_overrides(s.id)}
        if wanted is None:
            target = {k: o.reason for k, o in current.items() if k in arenas}
        else:
            target = {(o.symbol, o.timeframe.value): o.reason for o in wanted}
        for (symbol, tf), o in current.items():
            if (symbol, tf) not in target:
                self.store.set_cost_override(s.id, symbol, tf, False, ts=self.now)
                why = "" if (symbol, tf) in arenas else " (Assignment removed)"
                self._j("state", f"Cost Check override removed for {symbol} {tf}{why}", s, symbol)
        for (symbol, tf), reason in target.items():
            if (symbol, tf) not in current or current[(symbol, tf)].reason != reason:
                self.store.set_cost_override(s.id, symbol, tf, True, reason, self.now)
                self._j("state", f"Cost Check overridden for {symbol} {tf}" + (f": {reason}" if reason else ""), s, symbol, level="warn")

    async def set_cost_override(self, session_id: int, symbol: str, timeframe: str, enabled: bool, reason: str = "") -> None:
        s = self.store.get_session(session_id)
        if not any(a.symbol == symbol and a.timeframe == timeframe for a in self.store.assignments(session_id)):
            raise DomainError("not_found", f"session {s.name!r} has no {symbol} {timeframe} Assignment", 404)
        self.store.set_cost_override(session_id, symbol, timeframe, enabled, reason, self.now)
        verb = "overridden" if enabled else "override removed"
        self._j("state", f"Cost Check {verb} for {symbol} {timeframe}" + (f": {reason}" if reason else ""), s, symbol, level="warn" if enabled else "info")

    async def _adopt_orphans(self, announce: bool = True) -> int:
        """Record every owned position the database does not know (an Orphan: e.g. after an uncertain
        Order Outcome or a crash between send and commit). With ``announce`` it is an Alert."""
        magics = self.store.all_magics()
        known = {r.ticket for r in self.store.open_trades()}
        sessions = None
        adopted = 0
        for p in self._positions:
            sid = magics.get(p.magic)
            if sid is None or p.ticket in known or self.store.trade_by_ticket(p.ticket) is not None:
                continue
            sessions = sessions or {x.id: x for x in self.store.list_sessions()}
            s = sessions.get(sid)
            a = self._owner(s, p, self.store.assignments(sid)) if s else None
            self.store.add_trade(
                TradeRow(
                    ticket=p.ticket,
                    session_id=sid,
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
                    paper=p.ticket < 0,
                    paper_epoch=self.store.paper_epoch() if p.ticket < 0 else 0,
                    login=self._login,
                )
            )
            adopted += 1
            if announce:
                self._j("alert", f"Orphan adopted: {p.side} {p.volume} {p.symbol} #{p.ticket} was on the Account without a record", s, p.symbol,
                        level="warn", alert=True, data={"ticket": p.ticket})
            else:
                self._j("info", f"Re-adopted {p.side} {p.volume} {p.symbol} #{p.ticket}", s, p.symbol)
        return adopted

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
            if s.execution == "paper":
                close_positions = True  # Paper Positions have no server-side stops: never leave them unattended
            if close_positions and not await self._ensure_connected():
                raise DomainError("broker_offline", "cannot close positions: broker not connected", 503)
            if close_positions:
                await self._refresh()
            s = await self._stop(s, close_positions, "stopped by operator")
            self._publish()
            return s

    async def _stop(self, s: SessionRow, close_positions: bool, reason: str, alert: bool = False) -> SessionRow:
        s = self.store.update_session_fields(s.id, status="stopped", stopped_at=self.now, stop_reason=reason)
        self._cancel_pending(s, None, None, f"session stopped: {reason}")
        self._learn(lambda L: L.on_session_stopped(s))
        for a in self.store.assignments(s.id):
            self._states.pop(a.id, None)
        closed = 0
        if close_positions:
            closed = await self._close_all(self.store.session_magics(s.id), reason)
        left = len(self._session_positions(s))
        tail = f"closed {closed} positions" if close_positions else f"left {left} positions open (server SL/TP protect them)"
        self._j("alert" if alert else "state", f"Session '{s.name}' stopped: {reason}; {tail}", s, level="warn" if alert else "info", alert=alert)
        return s

    def _halt_sessions(self, reason: str) -> list[str]:
        """Stop every active/interrupted Session in the database and refuse entries already in flight."""
        self._kill_seq += 1
        stopped = []
        for s in self.store.list_sessions():
            if s.status in (*ACTIVE, "interrupted"):
                self.store.update_session_fields(s.id, status="stopped", stopped_at=self.now, stop_reason=reason)
                self._cancel_pending(s, None, None, reason)
                stopped.append(s.name)
        return stopped

    async def kill_all(self) -> dict:
        # Sessions are stopped BEFORE waiting for the engine lock: an engine pass stuck on a hung
        # broker call must not delay the stop, and its pending entry is refused (_kill_seq).
        stopped = self._halt_sessions("kill switch")
        async with self._lock:
            res = await self._kill_locked("kill switch", stopped)
            self._publish()
            return res

    async def _kill_locked(self, reason: str, stopped: list[str] | None = None) -> dict:
        stopped = (stopped or []) + self._halt_sessions(reason)
        self._states.clear()
        magics = set(self.store.all_magics())
        closed = 0
        if await self._ensure_connected():
            await self._refresh()
            closed = await self._close_all(magics, reason)
        left = sum(1 for p in self._positions if p.magic in magics)
        self.status.kill_switch_at = self.now
        tail = f"; {left} positions could NOT be closed — act in the terminal" if left else ""
        self._j(
            "alert",
            f"KILL SWITCH ({reason}): stopped {len(stopped)} sessions, closed {closed} positions{tail}",
            level="error",
            alert=True,
            data={"stopped": stopped, "closed": closed, "left": left},
        )
        return {"stopped_sessions": stopped, "closed_positions": closed, "left_open": left}

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
            closed, failed = await self._close_confirmed([ticket], "manual")
            if failed:
                raise DomainError("close_failed", failed[ticket], 502)
            self._j("close", f"Manual close requested for #{ticket} {p.symbol}", s, p.symbol)
            await self._reconcile_closed()
            self._publish()
            return {"ok": True, "ticket": ticket}

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
                row = self.store.get_session(sid)
                if row.login and self._account and row.login != self._account.login:
                    self._j("alert", f"Auto-resume skipped: terminal is logged into {self._account.login}, the session was pinned to {row.login}",
                            row, level="warn", alert=True)
                    continue
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
        await cancel_and_wait(self._task)

    async def tick_once(self) -> None:
        async with self._lock:
            self._sync_paper()
            if not await self._ensure_connected():
                self._track_disconnect()
                self.status.last_pass_wall = time.time()
                self._publish()
                return
            try:
                await self._refresh()
                self._check_pins()
                self._roll_day()
                await self._adopt_orphans()
                await self._reconcile_closed()
                await self._enforce_daily_loss()
                await self._enforce_equity_floor()
                await self._enforce_weekend_close()
                for s in self.store.list_sessions():
                    if s.status in ACTIVE:
                        await self._process_session(s)
                self._snapshot_equity()
            except BrokerError as e:
                self.status.connected = False
                if str(e) != self.status.last_error:
                    self._j("alert", f"Broker error: {e}", level="error", alert=True, data={"notify": False})
                self.status.last_error = str(e)
                self.status.disconnected_since = self.status.disconnected_since or time.time()
            self.status.passes += 1
            self.status.last_pass_wall = time.time()
            if self.on_activity is not None:
                try:
                    self.on_activity(any(s.status in ACTIVE for s in self.store.list_sessions()))
                except Exception:  # noqa: BLE001
                    log.exception("activity hook failed")
            self._publish()

    def _track_disconnect(self) -> None:
        st = self.status
        st.disconnected_since = st.disconnected_since or time.time()
        down = time.time() - st.disconnected_since
        if down >= DISCONNECT_NOTIFY_SECONDS and not st.disconnect_notified:
            st.disconnect_notified = True
            active = [s.name for s in self.store.list_sessions() if s.status in ACTIVE]
            self._j("alert", f"Broker disconnected for {down:.0f}s: {st.last_error or 'no connection'}"
                    + (f" — active sessions: {', '.join(active)} (server SL/TP still protect open positions)" if active else ""),
                    level="error", alert=True)

    async def _ensure_connected(self) -> bool:
        st = self.status
        try:
            if st.connected:
                ok = await self.broker.run(lambda b: b.is_connected())
                if ok:
                    return True
                st.connected = False
                st.disconnected_since = st.disconnected_since or time.time()
                self._j("alert", "Broker connection lost", level="error", alert=True, data={"notify": False})
            await self.broker.run(lambda b: b.connect())
        except BrokerError as e:
            st.connected = False
            if str(e) != st.last_error:
                st.last_error = str(e)
                self._j("alert", f"Cannot connect to broker: {e}", level="error", alert=True, data={"notify": False})
            return False
        st.connected = True
        st.last_error = ""
        await self._refresh()
        a = self._account
        self._j("info", f"Connected to {self.broker.mode.upper()} — account {a.login} @ {a.server} ({'demo' if a.is_demo else 'LIVE'})")
        if st.disconnected_since is not None:
            down = time.time() - st.disconnected_since
            if st.disconnect_notified:
                self._j("alert", f"Broker reconnected after {down:.0f}s", level="info", alert=True)
            st.disconnected_since, st.disconnect_notified = None, False
        return True

    def _check_pins(self) -> None:
        """Pinned Login (ADR 0006): a login change interrupts every active Session."""
        acct = self._account
        if acct is None:
            return
        for s in self.store.list_sessions():
            if s.status in ACTIVE and s.login and s.login != acct.login:
                self.store.update_session_fields(s.id, status="interrupted", stop_reason=f"account switched from {s.login} to {acct.login}")
                self._cancel_pending(s, None, None, "pinned login changed")
                for a in self.store.assignments(s.id):
                    self._states.pop(a.id, None)
                self._j("alert", f"Session '{s.name}' interrupted: the terminal switched from account {s.login} to {acct.login}. "
                        "Its positions stay on the old account; restart it manually.", s, level="error", alert=True)

    async def _enforce_equity_floor(self) -> None:
        acct = self._account
        if acct is None or acct.is_demo or not self.store.live_enabled(acct.login):
            return
        floor = self.store.equity_floor(acct.login)
        if floor is None:
            self._ensure_floor(acct)
            return
        if floor.get("breached_at") or acct.equity >= floor["floor"]:
            return
        floor = {**floor, "breached_at": self.now}
        self.store.set_equity_floor(acct.login, floor)
        self._j("alert", f"EQUITY FLOOR breached: equity {acct.equity:.2f} < floor {floor['floor']:.2f} — firing the Kill Switch", level="error", alert=True)
        await self._kill_locked(f"equity floor {floor['floor']:.2f} breached")

    def _ensure_floor(self, acct: AccountInfo) -> dict:
        floor = self.store.equity_floor(acct.login)
        if floor is None:
            floor = {"floor": round(acct.equity * DEFAULT_FLOOR_PCT / 100, 2), "pct": DEFAULT_FLOOR_PCT, "set_at": self.now, "breached_at": None}
            self.store.set_equity_floor(acct.login, floor)
            self._j("state", f"Equity Floor set at {floor['floor']:.2f} ({DEFAULT_FLOOR_PCT:.0f}% of equity {acct.equity:.2f})")
        return floor

    async def reset_equity_floor(self, confirm_login: int, pct: float = DEFAULT_FLOOR_PCT) -> dict:
        async with self._lock:
            acct = self._account
            if acct is None:
                raise DomainError("broker_offline", "no account connected", 503)
            if confirm_login != acct.login:
                raise DomainError("confirm_required", f"type the account number {acct.login} to reset the Equity Floor", 422)
            if not 0 < pct < 100:
                raise DomainError("invalid", "floor percent must be between 0 and 100", 422)
            floor = {"floor": round(acct.equity * pct / 100, 2), "pct": pct, "set_at": self.now, "breached_at": None}
            self.store.set_equity_floor(acct.login, floor)
            self._j("alert", f"Equity Floor reset to {floor['floor']:.2f} ({pct:g}% of equity {acct.equity:.2f})", level="warn", alert=True)
            self._publish()
            return floor

    async def _enforce_weekend_close(self) -> None:
        for s in self.store.list_sessions():
            if s.status not in ACTIVE or not s.weekend_close or not in_weekend_close(self.now, s.weekend_close_time):
                continue
            mine = [p.ticket for p in self._session_positions(s)]
            if mine:
                self._j("state", f"Weekend Close: closing {len(mine)} positions of '{s.name}' (Friday {s.weekend_close_time} server time)", s)
                await self._close_confirmed(mine, "weekend close")
                await self._reconcile_closed()

    async def _refresh(self) -> None:
        self._server_time, self._account, self._positions = await self.broker.run(
            lambda b: (b.server_time(), b.account(), b.positions())
        )

    def _roll_day(self) -> None:
        day = self.now // DAY
        pds = self.store.get_setting("day_start:paper") or {}
        if pds.get("day") != day:
            self.store.set_setting("day_start:paper", {"day": day, "equity": self._paper_account().equity})
        ds = self.store.get_setting(self._ds_key()) or {}
        if ds.get("day") != day and self._account:
            self.store.set_setting(self._ds_key(), {"day": day, "equity": self._account.equity, "balance": self._account.balance})
            if ds:
                self._j("info", f"New trading day (server time) — start equity {self._account.equity:.2f}")
                self._learn(lambda L: L.on_new_day(self.now))
                try:
                    self.bus.publish("notify", {"text": self.daily_summary(int(ds["day"]) * DAY, day * DAY, float(ds.get("equity") or 0))})
                except Exception:  # noqa: BLE001
                    log.exception("daily summary failed")

    def daily_summary(self, start: int, end: int, start_equity: float) -> str:
        acct = self._account
        lines = [f"📊 FXCommand daily summary — {time.strftime('%a %Y-%m-%d', time.gmtime(start))} (server time)"]
        closed = [
            t for t in self.store.trades(status="closed", limit=100_000) if t.close_time is not None and start <= t.close_time < end and self._mine(t.login)
        ]
        names = {x.id: x for x in self.store.list_sessions()}
        for sid in sorted({t.session_id for t in closed if t.session_id is not None}):
            mine = [t for t in closed if t.session_id == sid]
            pnl = sum(t.profit or 0 for t in mine)
            rs = [t.profit / t.risk_amount for t in mine if t.risk_amount and t.profit is not None]
            wins = sum(1 for t in mine if (t.profit or 0) > 0)
            s = names.get(sid)
            tag = " (paper)" if s and s.execution == "paper" else ""
            lines.append(f"• {s.name if s else sid}{tag}: {len(mine)} trades, P&L {pnl:+.2f}, {sum(rs):+.1f}R, win {wins / len(mine):.0%}")
        if not closed:
            lines.append("• no trades closed")
        lines.append(f"Open positions: {len(self._owned())} on the account, {sum(1 for p in self._positions if p.ticket < 0)} paper")
        if acct is not None and start_equity:
            lines.append(f"Equity {acct.equity:.2f} {acct.currency} ({acct.equity - start_equity:+.2f} vs day start)")
        rej = self.store.journal(kinds=["risk_reject"], limit=100_000)
        errs = self.store.journal(level="error", limit=100_000)
        lines.append(f"Risk Gate rejections: {sum(1 for j in rej if start <= j.ts < end)}; errors: {sum(1 for j in errs if start <= j.ts < end)}")
        return "\n".join(lines)

    async def _reconcile_closed(self) -> None:
        # only this Account's trades: another Account's open positions are simply not visible now
        open_rows = [r for r in self.store.open_trades() if self._mine(r.login)]
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
                if s.execution == "paper":
                    continue  # the Account's loss is not a Paper Session's
                await self._stop(s, close, f"AUTO-STOP: global daily loss {g:.2f} hit limit {g_cap:.2f}", alert=True)
            await self._refresh()
            active = [s for s in active if s.execution == "paper"]
        paper = [s for s in active if s.execution == "paper"]
        if paper:  # the Paper pool has its own day start and limits; they stop only Paper Sessions (D19)
            p_ts, p_equity = self._paper_day_start()
            pg = self._paper_global_pnl(p_ts)
            pg_cap = -limits.daily_loss_pct_global / 100 * p_equity
            if p_equity > 0 and pg <= pg_cap:
                for s in paper:
                    await self._stop(s, True, f"AUTO-STOP: Paper Account daily loss {pg:.2f} hit limit {pg_cap:.2f}", alert=True)
                await self._refresh()
                active = [s for s in active if s.execution != "paper"]
        for s in active:
            if s.execution == "paper":
                p_ts, p_equity = self._paper_day_start()
                pnl = self.store.realized_paper(since=p_ts, session_id=s.id) + sum(p.profit for p in self._session_positions(s))
                cap = -s.daily_loss_pct / 100 * p_equity
            else:
                pnl = self._session_pnl(s, day_ts)
                cap = -s.daily_loss_pct / 100 * day_equity
            if pnl <= cap:
                await self._stop(s, close or s.execution == "paper", f"AUTO-STOP: session daily loss {pnl:.2f} hit limit {cap:.2f}", alert=True)
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
        await self._process_pending(s, assigns)

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
        self.spreads.sample(a.symbol, tick, self.now)
        a_series = atr_series(bars, params["atr_period"]) if len(bars) > params["atr_period"] else None
        st.atr = float(a_series.iloc[-1]) if a_series is not None else 0.0
        pos = self._position_of(s, a)
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
        change = self._learn(lambda L: L.pending(s.id, a.symbol, a.timeframe))
        if change is None:
            return a
        if change.kind == "auto_promotion":
            acct = await self.broker.run(lambda b: b.account())
            if not acct.is_demo:  # re-checked at the moment of applying: never promote automatically on live
                self._learn(lambda L: L.cancel(change, "live account"))
                self._j("learning", f"Automatic promotion for {a.symbol} cancelled: account is live", s, a.symbol, level="warn")
                return a
        # an auto_rollback is allowed on live: it only restores a Champion the operator already approved
        now_open = await self.broker.run(lambda b: b.positions())
        if self._position_of(s, a, now_open) is not None:
            return a  # not flat yet: try again at the next bar close
        target = self._learn(lambda L: L.repo.candidate(change.candidate_key))
        if target is not None:
            [cost] = await self.cost_check(s, [a], target.strategy, target.param_dict)
            if not cost["allowed"]:  # the Cost Check holds at a Promotion too (D26)
                self._learn(lambda L: L.cancel(change, f"Cost Check: {cost['reason']}"))
                self._j("learning", f"{change.kind.replace('_', ' ').capitalize()} for {a.symbol} {a.timeframe} cancelled — Cost Check: {cost['reason']}", s, a.symbol, level="warn")
                return a
        cand = self._learn(lambda L: L.apply(change, s, a, self.now))
        if cand is None:
            return a
        try:
            params = get_strategy(cand.strategy).resolve(cand.param_dict)
        except KeyError:
            return a
        a = self.store.update_assignment(a.id, strategy=cand.strategy, params=params)
        self._sync_paper()  # a new strategy is a new identity with its own magic (ADR 0009)
        self._cancel_pending(s, a, "entry", "Champion changed")
        return a

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
            self._cancel_pending(s, a, "entry", "exit signal")
            if pos:
                await self._close(s, pos, f"{DEFERRABLE_REASONS[0]}: {sig.reason}", a)
                await self._apply_pending(s, a)  # the old Champion just went flat
            return
        if self._pending_for(s, a, "exit"):
            self._j("info", f"{sig.action.upper()} signal skipped: an exit is waiting for the market to open", s, a.symbol)
            return  # never open while a close is still outstanding
        if pos is not None:
            if pos.side == sig.action:
                self._j("info", f"Already {pos.side} #{pos.ticket}; signal ignored", s, a.symbol)
                return
            if not await self._close(s, pos, DEFERRABLE_REASONS[1], a):
                return  # never open the opposite side while the old position may still be there
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
        if s.weekend_close and in_weekend_close(self.now, s.weekend_close_time):
            self._j("info", f"Weekend Close (from Friday {s.weekend_close_time}) — entry skipped", s, a.symbol)
            return
        # the Signal Filter gates entries only (a reverse has already closed the old position above)
        screen = self._learn(lambda L: L.screen(s, a, sig, bars, tick)) if bars is not None and tick is not None else None
        if screen is not None and not screen.allowed:
            self._j("filter", f"{sig.action.upper()} blocked by Signal Filter: {screen.note}", s, a.symbol, level="warn", data={"p_win": screen.p_win})
            return
        record_id = screen.record_id if screen else None
        outcome, why = await self._enter(s, a, sig, record_id)
        if outcome == "transient":
            self._defer_entry(s, a, sig, record_id, why)

    @_order_in_flight
    async def _enter(self, s: SessionRow, a: AssignmentRow, sig: Signal, record_id: int | None = None, deferred: str = "") -> tuple[str, str]:
        """Gate and send one entry. Returns (outcome, reason): ``filled``, ``transient`` (a reject that
        clears by itself — nothing journaled, the caller keeps a Pending Entry), ``rejected``,
        ``failed`` or ``uncertain`` (journaled here, and the Learning Signal record gets its outcome)."""
        side = sig.action
        sym = a.symbol
        paper = s.execution == "paper"
        if self.store.get_session(s.id).status != "running":
            return "rejected", "session no longer running"  # stopped meanwhile (Kill Switch / Auto-stop)
        # Everything that comes from our own database is read up front...
        magics = self.store.all_magics()
        mine_magics = self.store.session_magics(s.id)
        paper_acct = self._paper_account() if paper else None
        if paper:  # the Paper pool (D19): its own day start and realised P&L, never the real Account's
            day_ts, day_equity = self._paper_day_start()
            epoch = self.store.paper_epoch()
            realized_session = self.store.realized_paper(since=day_ts, session_id=s.id, epoch=epoch)
            realized_global = self.store.realized_paper(since=day_ts, epoch=epoch)
        else:
            day_ts, day_equity = self._day_start()
            realized_session = self.store.realized_since(day_ts, s.id, self._login)
            realized_global = self.store.realized_since(day_ts, login=self._login)
        profile = self.store.to_profile(self.store.risk_profile(a.risk_profile_id))
        global_limits = self.store.global_limits()
        live_flags = self.store.get_setting("live_enabled", {}) or {}
        caps = LiveCaps(**self.store.live_caps())
        session_limits = RiskLimits(max_positions_session=s.max_positions, daily_loss_pct_session=s.daily_loss_pct)
        window = TradingWindow.from_dict(s.window)
        comment = f"fxc s{s.id} {a.strategy}"
        kill_seq = self._kill_seq

        def gate_and_send(b):
            # ...and the market-dependent part runs as ONE broker-thread call, so the tick the
            # Risk Gate priced the stops against is the tick the order is sent at. A not-executed
            # answer gets ONE retry, re-gated on a fresh tick; an uncertain one never (ADR 0007).
            notes: list[str] = []
            for attempt in (1, 2):
                info, tick, acct, positions = b.symbol_info(sym), b.tick(sym), b.account(), b.positions()
                if self._kill_seq != kill_seq:
                    return Rejected("kill_switch", "the Kill Switch fired while this entry was being prepared"), None, acct, positions, notes
                sizing = paper_acct if paper else acct  # the real ``acct`` is what goes back to self._account
                mine = [p for p in positions if p.magic in mine_magics]
                owned = [p for p in positions if p.magic in magics and (p.ticket < 0) == paper]
                exposure = Exposure(
                    session_positions=len(mine),
                    global_positions=len(owned),
                    session_day_pnl=realized_session + sum(p.profit for p in mine),
                    global_day_pnl=realized_global + sum(p.profit for p in owned),
                    day_start_equity=day_equity or sizing.equity,
                )
                decision = check(
                    GateInput(
                        symbol=info,
                        tick=tick,
                        side=side,  # type: ignore[arg-type]
                        sl_dist=sig.sl_dist,
                        tp_dist=sig.tp_dist,
                        account=sizing,
                        live_enabled=bool(live_flags.get(str(acct.login), False)),
                        profile=profile,
                        session_limits=session_limits,
                        global_limits=global_limits,
                        exposure=exposure,
                        window=window,
                        now=self.now,
                        paper=paper,
                        caps=caps if not acct.is_demo and not paper else None,
                    )
                )
                if isinstance(decision, Rejected):
                    return decision, None, acct, positions, notes
                if paper and a.magic not in getattr(b, "paper_magics", ()):
                    return Rejected("paper_routing", "Paper Session is not routed to the Paper book; order refused"), None, acct, positions, notes
                if not paper:
                    margin_rej = check_margin(b.margin_required(sym, side, decision.volume), acct)
                    if margin_rej is not None:
                        return margin_rej, None, acct, positions, notes
                res = b.market_order(sym, side, decision.volume, decision.sl, decision.tp, a.magic, comment)
                if res.ok or res.uncertain or attempt == 2 or res.retcode not in RETRY_ONCE:
                    break
                notes.append(f"retried once after: {res.message} ({res.retcode})")
            return decision, res, acct, b.positions(), notes

        before = {p.ticket for p in self._positions}
        decision, res, acct, self._positions, notes = await self.broker.run(gate_and_send)
        self._account = acct
        if isinstance(decision, Rejected):
            if decision.code in TRANSIENT_REJECTS:
                return "transient", decision.reason
            self._j("risk_reject", f"{side.upper()} rejected by Risk Gate: {decision.reason}", s, sym, level="warn", data=decision.to_dict())
            self._learn(lambda L: L.signal_outcome(record_id, rejected=True))
            return "rejected", decision.reason
        if res.uncertain:
            appeared = next((p for p in self._positions if p.magic == a.magic and p.symbol == sym and p.ticket not in before), None)
            self._j(
                "order_fail",
                f"{side.upper()} {decision.volume} {sym}: outcome UNCERTAIN ({res.message}, {res.retcode}) — not retried; "
                + (f"position #{appeared.ticket} found and adopted" if appeared else "no position found on the Account yet"),
                s, sym, level="error", alert=True, data={**res.to_dict(), "notes": notes},
            )
            await self._adopt_orphans(announce=False)
            if appeared is not None:
                self._learn(lambda L: L.signal_outcome(record_id, ticket=appeared.ticket))
            else:
                self._learn(lambda L: L.signal_outcome(record_id, rejected=True))
            return "uncertain", res.message
        if not res.ok:
            if res.retcode == MARKET_CLOSED:
                return "transient", f"market closed ({res.retcode})"
            self._learn(lambda L: L.signal_outcome(record_id, rejected=True))
            tail = f" ({'; '.join(notes)})" if notes else ""
            self._j("order_fail", f"{side.upper()} {decision.volume} {sym} failed: {res.message} ({res.retcode}){tail}", s, sym, level="error", alert=True,
                    data={**res.to_dict(), "notes": notes})
            return "failed", res.message
        # filled: trust the position, not the reply (some servers report price 0 on market execution)
        pos = next((p for p in self._positions if p.ticket == res.ticket), None)
        if pos is None:
            pos = next((p for p in self._positions if p.magic == a.magic and p.symbol == sym and p.ticket not in before), None)
        ticket = pos.ticket if pos else res.ticket
        price = pos.price_open if pos else (res.price or decision.entry)
        volume = pos.volume if pos else (res.volume or decision.volume)
        risk_amount = round(decision.risk_amount * volume / decision.volume, 2) if decision.volume else decision.risk_amount
        if volume < decision.volume - 1e-9:
            notes.append(f"partial fill {volume} of {decision.volume} lots")
        if decision.note:
            notes.insert(0, decision.note)
        if deferred:
            notes.append(deferred)
        self.store.add_trade(
            TradeRow(
                ticket=ticket,
                session_id=s.id,
                assignment_id=a.id,
                magic=a.magic,
                symbol=sym,
                side=side,
                volume=volume,
                strategy=a.strategy,
                timeframe=a.timeframe,
                open_time=pos.time if pos else self.now,
                open_price=price,
                sl=decision.sl,
                tp=decision.tp,
                initial_risk=abs(price - decision.sl),
                risk_amount=risk_amount,
                signal_reason=sig.reason,
                paper=paper,
                login=acct.login,
                paper_epoch=self.store.paper_epoch() if paper else 0,
            )
        )
        self._learn(lambda L: L.signal_outcome(record_id, ticket=ticket))
        tag = "PAPER " if paper else ""
        tail = f" — {'; '.join(notes)}" if notes else ""
        self._j(
            "order",
            f"{tag}Opened {side} {volume} {sym} #{ticket} @ {price} SL {decision.sl} TP {decision.tp or '—'} (risk {risk_amount:.2f} {acct.currency}){tail}",
            s,
            sym,
            data={**decision.to_dict(), "ticket": ticket, "price": price, "volume": volume, "paper": paper, "notes": notes},
        )
        return "filled", ""

    @_order_in_flight
    async def _close_confirmed(self, tickets: list[int], reason: str, defer_market_closed: bool = False) -> tuple[int, dict[int, str]]:
        """Close positions and keep trying until the Account confirms they are gone (closes are
        idempotent, ADR 0007). Returns (closed, {ticket: last failure message}). With
        ``defer_market_closed`` a close the market refuses (shut) is not retried or alerted: its
        failure note is ``DEFERRED`` and the caller keeps it as a pending exit (spec D12)."""
        remaining = list(dict.fromkeys(tickets))
        last: dict[int, str] = {}
        deferred: set[int] = set()
        for attempt in range(1, self.close_attempts + 1):
            def close_and_check(b, todo=tuple(remaining)):
                results = {t: b.close(t, reason) for t in todo}
                return results, b.positions()

            results, self._positions = await self.broker.run(close_and_check)
            open_now = {p.ticket for p in self._positions}
            for t, r in results.items():
                if t in open_now:
                    last[t] = f"{r.message} ({r.retcode})" if not r.ok else "position still open after a filled close"
                    if defer_market_closed and not r.ok and r.retcode == MARKET_CLOSED:
                        deferred.add(t)
            remaining = [t for t in remaining if t in open_now and t not in deferred]
            if not remaining:
                break
            if attempt < self.close_attempts:
                await asyncio.sleep(self.close_retry_delay)
        owners = self.store.all_magics()
        sessions = {x.id: x for x in self.store.list_sessions()}
        failed = {t: last.get(t, "unknown") for t in remaining}
        for t, msg in failed.items():
            p = next((x for x in self._positions if x.ticket == t), None)
            self._j("order_fail", f"Close #{t} failed after {self.close_attempts} attempts: {msg} — close it in the terminal",
                    sessions.get(owners.get(p.magic)) if p else None, p.symbol if p else None, level="error", alert=True)
        failed.update({t: DEFERRED for t in deferred})
        return len(tickets) - len(failed), failed

    async def _close(self, s: SessionRow, pos: Position, reason: str, a: AssignmentRow | None = None) -> bool:
        closed, failed = await self._close_confirmed([pos.ticket], reason, defer_market_closed=a is not None)
        if failed:
            if failed.get(pos.ticket) == DEFERRED and a is not None:
                self._defer_exit(s, a, pos, reason)
            return False
        self._j("info", f"Closing #{pos.ticket} {pos.symbol}: {reason}", s, pos.symbol)
        await self._reconcile_closed()
        return True

    async def _close_all(self, magics: set[int], reason: str) -> int:
        tickets = [p.ticket for p in self._positions if p.magic in magics]
        closed = 0
        if tickets:
            closed, _ = await self._close_confirmed(tickets, reason)
        await self._refresh()
        await self._reconcile_closed()
        return closed

    # ---------------------------------------------------- Pending Entries (D5/D12/D13/D20)
    def _pending_for(self, s: SessionRow, a: AssignmentRow, kind: str) -> list[PendingEntryRow]:
        return [r for r in self.store.pending_entries(s.id) if r.symbol == a.symbol and r.timeframe == a.timeframe and r.kind == kind]

    def _cancel_pending(self, s: SessionRow, a: AssignmentRow | None, kind: str | None, why: str, status: str = "cancelled") -> None:
        for r in self.store.pending_entries(s.id):
            if (a is None or (r.symbol, r.timeframe) == (a.symbol, a.timeframe)) and (kind is None or r.kind == kind):
                self._end_pending(s, r, status, why)

    def _end_pending(self, s: SessionRow, r: PendingEntryRow, status: str, why: str, journal: bool = True) -> None:
        self.store.update_pending_entry(r.id, status=status, note=why, done_ts=self.now)
        if r.kind == "entry" and status != "filled":
            self._learn(lambda L: L.signal_outcome(r.record_id, rejected=True))
        if journal:
            what = f"{r.side.upper()} entry" if r.kind == "entry" else f"exit of #{r.ticket}"
            self._j("info", f"Pending {what} ({r.timeframe}) {status}: {why}", s, r.symbol, data={"pending_id": r.id, "status": status})

    def _defer_entry(self, s: SessionRow, a: AssignmentRow, sig: Signal, record_id: int | None, why: str) -> None:
        """The Signal could not be sent now for a reason that clears by itself: keep it as a Pending
        Entry, sent through ``_enter`` on every engine pass until it fills or its fill window ends.
        Journaled once, here."""
        self._cancel_pending(s, a, "entry", "a newer signal replaced it", status="superseded")
        cand = self._learn(lambda L: L.champion_of(a))
        row = self.store.add_pending_entry(
            PendingEntryRow(
                session_id=s.id, symbol=a.symbol, timeframe=a.timeframe, signal_bar_ts=int(sig.bar_time), candidate_key=cand.key if cand else a.strategy,
                kind="entry", side=sig.action, sl_dist=sig.sl_dist, tp_dist=sig.tp_dist, reason=sig.reason, record_id=record_id, created_ts=self.now,
            )
        )
        limit = fill_limit(Timeframe(a.timeframe).seconds, get_strategy(a.strategy).fill_window_s)
        self._j(
            "risk_reject",
            f"{sig.action.upper()} deferred: {why} — pending until the market and the Trading Window are open (fills within {limit // 60} min of that)",
            s, a.symbol, level="warn", data={"pending_id": row.id, "reason": why},
        )

    def _defer_exit(self, s: SessionRow, a: AssignmentRow, pos: Position, reason: str) -> None:
        if any(r.ticket == pos.ticket for r in self._pending_for(s, a, "exit")):
            return
        self.store.add_pending_entry(
            PendingEntryRow(
                session_id=s.id, symbol=a.symbol, timeframe=a.timeframe, signal_bar_ts=self.now, candidate_key=a.strategy, kind="exit",
                side=pos.side, reason=reason, ticket=pos.ticket, created_ts=self.now,
            )
        )
        self._j("info", f"Exit of #{pos.ticket} {pos.symbol} deferred: market closed — it closes as soon as the market opens ({reason})", s, a.symbol)

    async def _first_tradable(self, s: SessionRow, r: PendingEntryRow) -> int | None:
        """The Next Tradable Time after the Signal's bar closed, as observed: the first M1 bar the
        market printed with the Trading Window open, or now if the quote is fresh and the window open."""
        window = TradingWindow.from_dict(s.window)
        close = r.signal_bar_ts + Timeframe(r.timeframe).seconds
        tick = await self.broker.run(lambda b: b.tick(r.symbol))
        if self.now - tick.time > MAX_QUOTE_AGE:
            return None  # still shut: no need to read bars on every pass (a weekend is 3,000 passes)
        n = int(min(5000, max(3, (self.now - close) // 60 + 3)))
        bars = await self.broker.run(lambda b: b.closed_bars(r.symbol, Timeframe.M1, n))
        for t in bars["time"].to_numpy() if len(bars) else ():
            if int(t) >= close and window.is_open(int(t)):
                return int(t)
        if self.now - tick.time <= MAX_QUOTE_AGE and window.is_open(self.now) and self.now >= close:
            return self.now
        return None

    async def _process_pending(self, s: SessionRow, assigns: list[AssignmentRow]) -> None:
        rows = self.store.pending_entries(s.id)
        if not rows:
            return
        s = self.store.get_session(s.id)
        by_arena = {(a.symbol, a.timeframe): a for a in assigns}
        for r in rows:
            a = by_arena.get((r.symbol, r.timeframe))
            if a is None:
                self._end_pending(s, r, "cancelled", "the Assignment was removed")
                continue
            if r.kind == "exit":
                await self._retry_exit(s, a, r)
            else:
                await self._retry_entry(s, a, r)

    @_order_in_flight
    async def _retry_exit(self, s: SessionRow, a: AssignmentRow, r: PendingEntryRow) -> None:
        """One close request per pass at most, and none while the quote is stale: a shut market
        must not see a request a second (brokers throttle that). Any other failure alerts once per
        distinct message and is retried quietly."""
        pos = next((p for p in self._positions if p.ticket == r.ticket), None)
        if pos is None:
            self._end_pending(s, r, "closed", "the position is gone (stop-loss / take-profit or closed elsewhere)")
            await self._reconcile_closed()
            return

        def close_once(b, ticket=pos.ticket, symbol=pos.symbol):
            if b.server_time() - b.tick(symbol).time > MAX_QUOTE_AGE:
                return None, None
            res = b.close(ticket, r.reason)
            return res, b.positions()

        res, positions = await self.broker.run(close_once)
        if res is None:
            return  # still shut
        self._positions = positions
        if all(p.ticket != pos.ticket for p in positions):
            self._end_pending(s, r, "closed", f"closed after the market opened ({(self.now - r.created_ts) // 60} min late)")
            await self._reconcile_closed()
            return
        msg = f"{res.message} ({res.retcode})" if not res.ok else "position still open after a filled close"
        if res.retcode != MARKET_CLOSED and msg != r.note:
            self.store.update_pending_entry(r.id, note=msg)
            self._j("order_fail", f"Deferred exit of #{pos.ticket} {pos.symbol} failed: {msg} — retrying every pass; close it in the terminal if it persists",
                    s, pos.symbol, level="error", alert=True)

    async def _retry_entry(self, s: SessionRow, a: AssignmentRow, r: PendingEntryRow) -> None:
        cand = self._learn(lambda L: L.champion_of(a))
        if cand is not None and cand.key != r.candidate_key:
            self._end_pending(s, r, "cancelled", "Champion changed")
            return
        if s.weekend_close and in_weekend_close(self.now, s.weekend_close_time):
            self._end_pending(s, r, "cancelled", f"Weekend Close (from Friday {s.weekend_close_time})")
            return
        if self._position_of(s, a) is not None:
            self._end_pending(s, r, "superseded", "the Assignment already holds a position")
            return
        if r.first_tradable_ts is None:
            first = await self._first_tradable(s, r)
            if first is not None:
                limit = fill_limit(Timeframe(r.timeframe).seconds, get_strategy(a.strategy).fill_window_s)
                r = self.store.update_pending_entry(r.id, first_tradable_ts=first, expires_ts=first + limit)
        if (r.expires_ts is not None and self.now >= r.expires_ts) or self.now - r.created_ts > PENDING_MAX_WAIT:
            self._end_pending(s, r, "expired", "its fill window passed" if r.expires_ts else "the market never opened")
            return
        if s.status == "paused" or r.first_tradable_ts is None:
            return  # a paused Session never sends; nothing to try while the market is shut
        sig = Signal(r.side, r.reason, r.sl_dist, r.tp_dist, bar_time=r.signal_bar_ts)
        delay = self.now - (r.signal_bar_ts + Timeframe(r.timeframe).seconds)
        outcome, why = await self._enter(s, a, sig, r.record_id, deferred=f"Pending Entry filled {delay // 60} min after the signal")
        if outcome == "transient":
            return
        if outcome == "filled":
            ticket = next((t.ticket for t in self.store.open_trades() if t.assignment_id == a.id), None)
            self.store.update_pending_entry(r.id, status="filled", ticket=ticket, done_ts=self.now, note=f"filled {delay}s after the signal")
        else:  # rejected / failed / uncertain: journaled by _enter; never retried
            self.store.update_pending_entry(r.id, status="rejected", done_ts=self.now, note=why)

    async def _manage_positions(self, s: SessionRow, assigns: list[AssignmentRow]) -> None:
        idents = self.store.magic_identities()
        for p in self._session_positions(s):
            a = self._owner(s, p, assigns, idents)
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
                tick = b.tick(p.symbol)
                if b.server_time() - tick.time > MAX_QUOTE_AGE:
                    return None, None  # shut (e.g. GOLD's break): no request at all, try when it reopens
                new_sl = manage(p, tick, b.symbol_info(p.symbol), atr, initial, profile)
                return new_sl, (b.modify(p.ticket, new_sl, p.tp) if new_sl is not None else None)

            new_sl, res = await self.broker.run(manage_and_modify)
            if res is None:
                continue
            if res.ok:
                self._modify_failed.pop(p.ticket, None)
                if tr:
                    self.store.update_trade(p.ticket, sl=new_sl)
                self._j("modify", f"Moved SL of #{p.ticket} {p.symbol} {p.sl} → {new_sl}", s, p.symbol, data={"ticket": p.ticket, "old": p.sl, "new": new_sl})
            elif self._modify_failed.get(p.ticket) != res.message:
                # retried every pass (e.g. through GOLD's daily break), journaled once per reason
                self._modify_failed[p.ticket] = res.message
                self._j("order_fail", f"SL move #{p.ticket} failed: {res.message} (retrying every pass)", s, p.symbol, level="warn")

    # ============================================================== read side
    def snapshot_account(self) -> AccountInfo | None:
        return self._account

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
                    "paper": p.ticket < 0,
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
                    "magics": sorted(self.store.session_magics(s.id)),
                    "open_positions": len(self._session_positions(s)),
                    "day_pnl": round(self._session_pnl(s, day_ts), 2),
                    "stop_reason": s.stop_reason,
                    "execution": s.execution,
                    "login": s.login,
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
            "heartbeat_age": round(time.time() - self.status.last_pass_wall, 1) if self.status.last_pass_wall else None,
            "broker_busy_for": round(time.time() - self.broker.busy_since, 1) if self.broker.busy_since else 0.0,
            "disconnected_for": round(time.time() - self.status.disconnected_since, 1) if self.status.disconnected_since else 0.0,
            "equity_floor": self.store.equity_floor(acct.login) if acct else None,
            "live_caps": self.store.live_caps(),
        }

    # ============================================================ pre-flight
    async def preflight(self, session_id: int | None = None) -> dict:
        s = self.store.get_session(session_id) if session_id is not None else None
        await self._ensure_connected()
        return await self._preflight(s)

    async def _preflight(self, s: SessionRow | None) -> dict:
        assigns = [a for a in self.store.assignments(s.id) if a.enabled] if s else []
        profiles = {a.id: self.store.to_profile(self.store.risk_profile(a.risk_profile_id)) for a in assigns}
        typical: dict[int, tuple[int, float]] = {}
        for a in assigns:
            try:
                params = get_strategy(a.strategy).resolve(a.params)
                typical[a.id] = (int(params.get("atr_period", 14)), float(params.get("sl_atr", 2.0)))
            except KeyError:
                typical[a.id] = (14, 2.0)
        connected = self.status.connected

        def gather(b):
            term = acct = None
            now = int(time.time())
            try:
                term, acct, now = b.terminal(), b.account(), b.server_time()
            except BrokerError:
                pass
            facts = []
            for a in assigns:
                sf = pf.SymbolFacts(name=a.symbol, profile=profiles[a.id])
                try:
                    sf.info, sf.tick = b.symbol_info(a.symbol), b.tick(a.symbol)
                    period, mult = typical[a.id]
                    bars = b.closed_bars(a.symbol, Timeframe(a.timeframe), period * 3 + 5)
                    if len(bars) > period:
                        sf.typical_sl = float(atr_series(bars, period).iloc[-1]) * mult
                    sf.margin_min_lot = b.margin_required(a.symbol, "long", sf.info.volume_min)
                except BrokerError as e:
                    sf.error = str(e)
                facts.append(sf)
            return term, acct, now, facts

        if connected:
            term, acct, now, facts = await self.broker.run(gather)
        else:
            term, acct, now, facts = None, None, int(time.time()), [pf.SymbolFacts(name=a.symbol, error="broker offline") for a in assigns]
        db_ok = True
        try:
            self.store.set_setting("preflight_probe", time.time())
        except Exception:  # noqa: BLE001
            db_ok = False
        report = pf.evaluate(
            pf.Facts(
                connected=connected,
                terminal=term,
                account=acct,
                now=now,
                live_enabled=bool(acct and self.store.live_enabled(acct.login)),
                floor=self.store.equity_floor(acct.login) if acct else None,
                notifier_configured=bool(self.notifier_configured()),
                db_ok=db_ok,
                heartbeat_age=(time.time() - self.status.last_pass_wall) if self.status.last_pass_wall else None,
                paper=bool(s and s.execution == "paper"),
                symbols=facts,
            )
        )
        report["session_id"] = s.id if s else None
        return report

    def _publish(self) -> None:
        try:
            self.bus.publish("snapshot", self.snapshot())
        except Exception:  # pragma: no cover
            log.exception("snapshot publish failed")
