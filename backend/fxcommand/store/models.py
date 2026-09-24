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


# ------------------------------------------------------------------ learning
# All learning records are keyed by Arena (symbol, timeframe) or by (session_id, symbol) — never by
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
    candidate_key: str
    kind: str  # promotion | auto_promotion | rollback | auto_rollback
    reason: str = ""
    challenger_id: Optional[int] = None
    status: str = Field(default="pending", index=True)  # pending | applied | cancelled
    created_wall: float = 0
    applied_ts: Optional[int] = None
