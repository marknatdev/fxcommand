"""SQLite tables (SQLModel). Timestamps: ``*_at`` / ``ts`` are broker server time; ``wall`` is real UTC time."""

from __future__ import annotations

from typing import Any, Optional

from sqlalchemy import JSON, Column
from sqlmodel import Field, SQLModel


class SessionRow(SQLModel, table=True):
    __tablename__ = "sessions"

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True, unique=True)
    status: str = "stopped"  # stopped | running | paused | interrupted
    magic: int = Field(unique=True)
    auto_resume: bool = False
    window: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    max_positions: int = 5
    daily_loss_pct: float = 3.0
    notes: str = ""
    created_at: float = 0
    started_at: Optional[int] = None
    stopped_at: Optional[int] = None
    stop_reason: str = ""
    execution: str = "broker"  # broker | paper (Execution Mode, ADR 0007)
    login: Optional[int] = None  # Pinned Login while active (ADR 0006)
    weekend_close: bool = False
    weekend_close_time: str = "22:30"  # Friday, server time


class AssignmentRow(SQLModel, table=True):
    __tablename__ = "assignments"

    id: Optional[int] = Field(default=None, primary_key=True)
    session_id: int = Field(foreign_key="sessions.id", index=True)
    symbol: str
    timeframe: str = "M15"
    strategy: str = "ema_cross"
    params: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    risk_profile_id: int = Field(foreign_key="risk_profiles.id")
    reverse_on_opposite: bool = True
    enabled: bool = True
    magic: int = 0  # from magic_identities; set whenever the row is written (ADR 0009)


class MagicIdentityRow(SQLModel, table=True):
    """One Magic Number per (Session, Symbol, Strategy, Timeframe), never reused (ADR 0009). Rows
    outlive Assignment edits and Session deletion, so an identity always gets its magic back."""

    __tablename__ = "magic_identities"

    id: Optional[int] = Field(default=None, primary_key=True)
    session_id: int = Field(index=True)
    symbol: str
    strategy: str
    timeframe: str
    magic: int = Field(unique=True)
    created_wall: float = 0


class PendingEntryRow(SQLModel, table=True):
    """A Signal the engine could not act on yet (spec D5/D12/D20): an entry waiting for the market
    or the Trading Window to open, or a strategy exit waiting for the market. Keyed by Arena and
    bar (never by assignment id); sent only through ``SessionManager._enter`` / the close path."""

    __tablename__ = "pending_entries"

    id: Optional[int] = Field(default=None, primary_key=True)
    session_id: int = Field(index=True)
    symbol: str
    timeframe: str
    signal_bar_ts: int  # open time of the Signal's bar
    candidate_key: str
    kind: str = "entry"  # entry | exit
    side: str = ""  # long | short (entry)
    sl_dist: float = 0.0
    tp_dist: float = 0.0
    reason: str = ""  # the Signal's reason
    record_id: Optional[int] = None  # Learning Signal record (the Signal Filter screens only when the Signal arrives)
    ticket: Optional[int] = None  # exit: the position to close; entry: the fill
    created_ts: int = 0
    first_tradable_ts: Optional[int] = None  # Next Tradable Time, as observed
    expires_ts: Optional[int] = None
    status: str = Field(default="pending", index=True)  # pending | filled | expired | cancelled | rejected | superseded | closed
    note: str = ""
    done_ts: Optional[int] = None


class CostOverrideRow(SQLModel, table=True):
    """The operator let an Assignment start although its Cost Check fails (spec D4). Keyed by
    (Session, Symbol, Timeframe): since ADR 0009 a Symbol can be in a Session twice, and an override
    for GOLD H1 must not cover a GOLD M1 scalper."""

    __tablename__ = "cost_overrides"

    id: Optional[int] = Field(default=None, primary_key=True)
    session_id: int = Field(index=True)
    symbol: str
    timeframe: str
    reason: str = ""
    set_ts: int = 0


class RiskProfileRow(SQLModel, table=True):
    __tablename__ = "risk_profiles"

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(unique=True)
    risk_pct: float = 1.0
    max_spread_points: float = 30.0
    breakeven: bool = False
    breakeven_at_r: float = 1.0
    trailing: bool = False
    trailing_atr: float = 2.0
    trailing_start_r: float = 1.0
    allow_min_lot: bool = False  # trade the minimum lot when risk % buys less, if that risk is small enough
    min_lot_max_risk_pct: float = 2.0


class TradeRow(SQLModel, table=True):
    __tablename__ = "trades"

    id: Optional[int] = Field(default=None, primary_key=True)
    ticket: int = Field(index=True, unique=True)
    session_id: Optional[int] = Field(default=None, index=True)
    assignment_id: Optional[int] = None
    magic: int = Field(index=True)
    symbol: str = Field(index=True)
    side: str
    volume: float
    strategy: str = ""
    timeframe: str = ""
    open_time: int
    open_price: float
    sl: float = 0.0
    tp: float = 0.0
    initial_risk: float = 0.0  # price distance entry -> initial SL (1R)
    risk_amount: float = 0.0
    signal_reason: str = ""
    status: str = Field(default="open", index=True)  # open | closed
    close_time: Optional[int] = Field(default=None, index=True)
    close_price: Optional[float] = None
    profit: Optional[float] = None
    close_reason: str = ""
    adopted: bool = False
    paper: bool = False
    login: Optional[int] = Field(default=None, index=True)  # Account the trade lives on (None: recorded before logins were kept)
    paper_epoch: int = 0  # Paper Account epoch (a reset starts a new one; older rows are archived, never deleted)


class JournalRow(SQLModel, table=True):
    __tablename__ = "journal"

    id: Optional[int] = Field(default=None, primary_key=True)
    ts: int = Field(index=True)
    wall: float
    session_id: Optional[int] = Field(default=None, index=True)
    symbol: Optional[str] = None
    kind: str = Field(index=True)  # signal | risk_reject | order | order_fail | close | modify | state | alert | info
    level: str = "info"  # info | warn | error
    alert: bool = Field(default=False, index=True)
    message: str
    data: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))


class SettingRow(SQLModel, table=True):
    __tablename__ = "settings"

    key: str = Field(primary_key=True)
    value: Any = Field(default=None, sa_column=Column(JSON))


class EquityRow(SQLModel, table=True):
    __tablename__ = "equity"

    id: Optional[int] = Field(default=None, primary_key=True)
    ts: int = Field(index=True)
    balance: float
    equity: float


class PaperPositionRow(SQLModel, table=True):
    """The Paper book (ADR 0007). Tickets are negative so they can never collide with the Broker's."""

    __tablename__ = "paper_positions"

    ticket: int = Field(primary_key=True)
    symbol: str = Field(index=True)
    side: str
    volume: float
    price_open: float
    sl: float
    tp: float = 0.0
    magic: int = Field(index=True)
    time: int
    comment: str = ""
    checked_to: int = 0  # server time up to which bars have been checked against SL/TP
    status: str = Field(default="open", index=True)  # open | closed
    price_close: Optional[float] = None
    time_close: Optional[int] = Field(default=None, index=True)
    profit: Optional[float] = None  # includes swap
    reason: str = ""
    swap: float = 0.0  # charged at the rollovers held through (spec D28)
    epoch: int = 0  # Paper Account epoch


# ------------------------------------------------------------------ learning
# All learning records are keyed by Arena (symbol, timeframe) or by (session_id, symbol, timeframe) — never by
# assignment id, which changes whenever a Session is edited.


class CandidateRow(SQLModel, table=True):
    __tablename__ = "learning_candidates"

    key: str = Field(primary_key=True)
    strategy: str
    params: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created: float = 0


class ChampionVersionRow(SQLModel, table=True):
    __tablename__ = "learning_champion_versions"

    id: Optional[int] = Field(default=None, primary_key=True)
    session_id: int = Field(index=True)
    symbol: str = Field(index=True)
    timeframe: str
    version: int
    candidate_key: str
    previous_key: Optional[str] = None
    kind: str  # initial | manual | promotion | auto_promotion | rollback | auto_rollback
    reason: str = ""
    server_ts: int = 0
    wall: float = 0


class LearningSlotRow(SQLModel, table=True):
    __tablename__ = "learning_slots"

    id: Optional[int] = Field(default=None, primary_key=True)
    session_id: int = Field(index=True)
    symbol: str
    timeframe: str = ""  # "" = written before a Symbol could appear twice in a Session
    auto_promote: bool = False


class ChallengerRow(SQLModel, table=True):
    __tablename__ = "learning_challengers"

    id: Optional[int] = Field(default=None, primary_key=True)
    symbol: str = Field(index=True)
    timeframe: str
    candidate_key: str
    status: str = Field(default="active", index=True)  # active | retired | promoted
    started_ts: int = 0  # server time the Challenger started Shadow Trading
    ended_ts: Optional[int] = None
    run_id: Optional[int] = None
    oos: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))  # challenger Walk-forward OOS RStats
    champion_oos: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    trials: int = 10
    robust: Optional[bool] = None
    note: str = ""


class ShadowTradeRow(SQLModel, table=True):
    __tablename__ = "learning_shadow_trades"

    id: Optional[int] = Field(default=None, primary_key=True)
    symbol: str = Field(index=True)
    timeframe: str
    candidate_key: str = Field(index=True)
    side: str
    signal_ts: int
    open_ts: int = Field(index=True)
    close_ts: Optional[int] = Field(default=None, index=True)
    entry: float
    exit: Optional[float] = None
    sl: float = 0
    tp: float = 0
    risk: float = 0
    r: Optional[float] = None
    reason: str = ""
    status: str = Field(default="open", index=True)  # open | closed | void
    features: Optional[list] = Field(default=None, sa_column=Column(JSON))
    p_win: Optional[float] = None


class OptimizerRunRow(SQLModel, table=True):
    __tablename__ = "learning_optimizer_runs"

    id: Optional[int] = Field(default=None, primary_key=True)
    symbol: str = Field(index=True)
    timeframe: str
    trigger: str = "manual"
    status: str = "queued"  # queued | running | done | insufficient | failed
    champion_key: str = ""
    queued_wall: float = 0
    started_wall: Optional[float] = None
    finished_wall: Optional[float] = None
    result: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    note: str = ""


class FilterStateRow(SQLModel, table=True):
    __tablename__ = "learning_filters"

    key: str = Field(primary_key=True)  # "SYMBOL|TF|strategy"
    state: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    trained_on: int = 0


class SignalRecordRow(SQLModel, table=True):
    __tablename__ = "learning_signals"

    id: Optional[int] = Field(default=None, primary_key=True)
    session_id: int = Field(index=True)
    symbol: str = Field(index=True)
    timeframe: str
    strategy: str
    candidate_key: str
    ts: int = Field(index=True)
    side: str
    features: Optional[list] = Field(default=None, sa_column=Column(JSON))
    p_win: Optional[float] = None
    filter_mode: str = "observe"
    decision: str = "taken"  # taken | blocked | rejected
    ticket: Optional[int] = Field(default=None, index=True)
    r: Optional[float] = None


class PendingChangeRow(SQLModel, table=True):
    __tablename__ = "learning_pending"

    id: Optional[int] = Field(default=None, primary_key=True)
    session_id: int = Field(index=True)
    symbol: str
    timeframe: str = ""  # "" = written before a Symbol could appear twice in a Session
    candidate_key: str
    kind: str  # promotion | auto_promotion | rollback | auto_rollback
    reason: str = ""
    challenger_id: Optional[int] = None
    status: str = Field(default="pending", index=True)  # pending | applied | cancelled
    created_wall: float = 0
    applied_ts: Optional[int] = None


class EvidenceRow(SQLModel, table=True):
    """One Evidence Run: a read-only Backtest of a Candidate on an Arena's full history at 2× the
    typical spread (spec D11, D23). Local DB only: never committed, never exposed beyond read-only MCP."""

    __tablename__ = "evidence_runs"

    id: Optional[int] = Field(default=None, primary_key=True)
    key: str = Field(index=True)  # learning.evidence.evidence_key: every setting the result depends on
    symbol: str = Field(index=True)
    timeframe: str
    strategy: str
    candidate_key: str
    params: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    exit_rules: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    exit_rules_hash: str = ""
    window: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    window_hash: str = ""
    weekend_close: str = ""  # "" = off, else Friday close time "HH:MM"
    cost_version: int = 0
    costs: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))  # the CostModel used
    status: str = Field(default="queued", index=True)  # queued | running | done | incomplete | failed
    trigger: str = "manual"
    requested_wall: float = 0
    finished_wall: Optional[float] = None
    run_ts: Optional[int] = None  # server time the history was read
    bars: int = 0
    first_ts: Optional[int] = None
    last_ts: Optional[int] = None
    trades: int = 0
    mean_r: float = 0.0
    sqn: float = 0.0
    win_rate: float = 0.0
    total_r: float = 0.0
    max_dd_r: float = 0.0
    periods: list[Any] = Field(default_factory=list, sa_column=Column(JSON))  # [{label, from_ts, to_ts, n, mean}]
    rs: list[Any] = Field(default_factory=list, sa_column=Column(JSON))  # every trade's R (rounded), for the scorecard band
    note: str = ""
