"""Persistence for configuration, trades, Journal and equity. SQLite via SQLModel.

Every method opens its own short DB session and returns detached rows, so
callers never hold a connection across an ``await``.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import event, func
from sqlmodel import Session as DB
from sqlmodel import SQLModel, create_engine, select

from ..risk import RiskLimits, RiskProfile, TradingWindow
from .models import AssignmentRow, EquityRow, JournalRow, PaperPositionRow, RiskProfileRow, SessionRow, SettingRow, TradeRow

FIRST_MAGIC = 770_001

DEFAULT_APP_SETTINGS: dict[str, Any] = {
    "poll_interval": 1.0,  # seconds between engine passes
    "sim_speed": 60.0,  # simulated minutes per real minute (0 = clock stopped, advance manually)
    "close_on_auto_stop": True,  # close a Session's positions when a daily-loss limit stops it
    "equity_snapshot_seconds": 300,  # server-time spacing of equity curve points
    "default_window": TradingWindow().to_dict(),
    "learning_enabled": True,  # Shadow Trading, Signal Filter and Optimizer Runs
    "learning_candidates": 200,  # Candidates generated per Optimizer Run
    "learning_bars": 5000,  # history fetched per Optimizer Run
}

DEFAULT_LIVE_CAPS: dict[str, float] = {"max_risk_pct": 1.0, "max_volume": 0.10}
DEFAULT_FLOOR_PCT = 80.0

DEFAULT_PROFILES = (
    RiskProfileRow(name="Default", risk_pct=1.0, max_spread_points=30),
    RiskProfileRow(name="Conservative", risk_pct=0.5, max_spread_points=20, breakeven=True, breakeven_at_r=1.0),
    RiskProfileRow(name="Aggressive", risk_pct=2.0, max_spread_points=40, trailing=True, trailing_atr=2.0, trailing_start_r=1.0),
    RiskProfileRow(name="Gold", risk_pct=1.0, max_spread_points=60, breakeven=True, trailing=True, trailing_atr=2.5),
)


class NotFound(LookupError):
    pass


class Store:
    def __init__(self, url: str):
        if url.startswith("sqlite:///") and ":memory:" not in url:
            Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
        self.engine = create_engine(url, connect_args={"check_same_thread": False})

        @event.listens_for(self.engine, "connect")
        def _pragmas(dbapi_conn, _):  # pragma: no cover - trivial
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

        SQLModel.metadata.create_all(self.engine)
        self._add_missing_columns()
        self._seed()

    def _add_missing_columns(self) -> None:
        """``create_all`` never alters an existing table. Add columns that newer models declare, with
        their model default, so an existing database keeps working after an upgrade."""
        from sqlalchemy import inspect, text

        insp = inspect(self.engine)
        by_table = {m.__tablename__: m for m in SQLModel.__subclasses__() if getattr(m, "__tablename__", None)}
        with self.engine.begin() as conn:
            for table in SQLModel.metadata.sorted_tables:
                if not insp.has_table(table.name):
                    continue
                have = {c["name"] for c in insp.get_columns(table.name)}
                model = by_table.get(table.name)
                for col in table.columns:
                    if col.name in have:
                        continue
                    default = None
                    if model is not None and col.name in model.model_fields:
                        d = model.model_fields[col.name].default
                        default = None if d is ... or callable(d) else d
                    lit = "NULL" if default is None else (str(int(default)) if isinstance(default, bool) else repr(default) if isinstance(default, str) else str(default))
                    conn.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {col.type.compile(self.engine.dialect)} DEFAULT {lit}'))

    def _db(self) -> DB:
        return DB(self.engine, expire_on_commit=False)

    def _seed(self) -> None:
        with self._db() as db:
            if db.exec(select(RiskProfileRow)).first() is None:
                for p in DEFAULT_PROFILES:
                    db.add(RiskProfileRow.model_validate(p.model_dump(exclude={"id"})))
                db.commit()

    # --------------------------------------------------------------- settings
    def get_setting(self, key: str, default: Any = None) -> Any:
        with self._db() as db:
            row = db.get(SettingRow, key)
            return default if row is None else row.value

    def set_setting(self, key: str, value: Any) -> None:
        with self._db() as db:
            row = db.get(SettingRow, key)
            if row is None:
                db.add(SettingRow(key=key, value=value))
            else:
                row.value = value
                db.add(row)
            db.commit()

    def app_settings(self) -> dict:
        return {**DEFAULT_APP_SETTINGS, **(self.get_setting("app", {}) or {})}

    def update_app_settings(self, patch: dict) -> dict:
        merged = {**self.app_settings(), **{k: v for k, v in patch.items() if k in DEFAULT_APP_SETTINGS}}
        TradingWindow.from_dict(merged["default_window"])  # validate
        self.set_setting("app", merged)
        return merged

    def global_limits(self) -> RiskLimits:
        return RiskLimits(**{**RiskLimits().to_dict(), **(self.get_setting("global_limits", {}) or {})})

    def set_global_limits(self, limits: RiskLimits) -> None:
        self.set_setting("global_limits", limits.to_dict())

    def live_enabled(self, login: int) -> bool:
        return bool((self.get_setting("live_enabled", {}) or {}).get(str(login), False))

    def set_live_enabled(self, login: int, enabled: bool) -> None:
        cur = dict(self.get_setting("live_enabled", {}) or {})
        cur[str(login)] = bool(enabled)
        self.set_setting("live_enabled", cur)

    # Live Caps and Equity Floor (live Accounts only)
    def live_caps(self) -> dict:
        return {**DEFAULT_LIVE_CAPS, **(self.get_setting("live_caps", {}) or {})}

    def set_live_caps(self, caps: dict) -> dict:
        merged = {**self.live_caps(), **{k: float(v) for k, v in caps.items() if k in DEFAULT_LIVE_CAPS}}
        if merged["max_risk_pct"] <= 0 or merged["max_volume"] <= 0:
            raise ValueError("live caps must be positive")
        self.set_setting("live_caps", merged)
        return merged

    def equity_floor(self, login: int) -> dict | None:
        return (self.get_setting("equity_floor", {}) or {}).get(str(login))

    def set_equity_floor(self, login: int, value: dict | None) -> None:
        cur = dict(self.get_setting("equity_floor", {}) or {})
        if value is None:
            cur.pop(str(login), None)
        else:
            cur[str(login)] = value
        self.set_setting("equity_floor", cur)

    def notify_settings(self) -> dict:
        return {"telegram_token": "", "telegram_chat_id": "", "enabled": True, **(self.get_setting("notify", {}) or {})}

    def allocate_magic(self) -> int:
        magic = int(self.get_setting("next_magic", FIRST_MAGIC))
        with self._db() as db:
            used = db.exec(select(func.max(SessionRow.magic))).one()
        magic = max(magic, (used or 0) + 1)
        self.set_setting("next_magic", magic + 1)
        return magic

    # ---------------------------------------------------------------- sessions
    def list_sessions(self) -> list[SessionRow]:
        with self._db() as db:
            return list(db.exec(select(SessionRow).order_by(SessionRow.id)))

    def get_session(self, session_id: int) -> SessionRow:
        with self._db() as db:
            row = db.get(SessionRow, session_id)
            if row is None:
                raise NotFound(f"session {session_id} not found")
            return row

    def session_by_name(self, name: str) -> SessionRow | None:
        with self._db() as db:
            return db.exec(select(SessionRow).where(SessionRow.name == name)).first()

    def all_magics(self) -> dict[int, int]:
        """magic -> session id, for every Session ever created (ownership test)."""
        with self._db() as db:
            return {m: i for i, m in db.exec(select(SessionRow.id, SessionRow.magic))}

    def save_session(self, row: SessionRow, assignments: list[AssignmentRow] | None = None) -> SessionRow:
        with self._db() as db:
            db.add(row)
            db.flush()
            if assignments is not None:
                for old in db.exec(select(AssignmentRow).where(AssignmentRow.session_id == row.id)):
                    db.delete(old)
                db.flush()
                for a in assignments:
                    a.id = None
                    a.session_id = row.id
                    db.add(a)
            db.commit()
            db.refresh(row)
            return row

    def update_session_fields(self, session_id: int, **fields: Any) -> SessionRow:
        with self._db() as db:
            row = db.get(SessionRow, session_id)
            if row is None:
                raise NotFound(f"session {session_id} not found")
            for k, v in fields.items():
                setattr(row, k, v)
            db.add(row)
            db.commit()
            db.refresh(row)
            return row

    def delete_session(self, session_id: int) -> None:
        with self._db() as db:
            row = db.get(SessionRow, session_id)
            if row is None:
                raise NotFound(f"session {session_id} not found")
            for a in db.exec(select(AssignmentRow).where(AssignmentRow.session_id == session_id)):
                db.delete(a)
            db.delete(row)
            db.commit()

    def update_assignment(self, assignment_id: int, **fields: Any) -> AssignmentRow:
        with self._db() as db:
            row = db.get(AssignmentRow, assignment_id)
            if row is None:
                raise NotFound(f"assignment {assignment_id} not found")
            for k, v in fields.items():
                setattr(row, k, v)
            db.add(row)
            db.commit()
            db.refresh(row)
            return row

    def assignments(self, session_id: int) -> list[AssignmentRow]:
        with self._db() as db:
            return list(db.exec(select(AssignmentRow).where(AssignmentRow.session_id == session_id).order_by(AssignmentRow.id)))

    def all_assignments(self) -> list[AssignmentRow]:
        with self._db() as db:
            return list(db.exec(select(AssignmentRow)))

    # ----------------------------------------------------------- risk profiles
    def risk_profiles(self) -> list[RiskProfileRow]:
        with self._db() as db:
            return list(db.exec(select(RiskProfileRow).order_by(RiskProfileRow.id)))

    def risk_profile(self, profile_id: int) -> RiskProfileRow:
        with self._db() as db:
            row = db.get(RiskProfileRow, profile_id)
            if row is None:
                raise NotFound(f"risk profile {profile_id} not found")
            return row

    def save_risk_profile(self, row: RiskProfileRow) -> RiskProfileRow:
        with self._db() as db:
            if row.id is not None:
                existing = db.get(RiskProfileRow, row.id)
                if existing is None:
                    raise NotFound(f"risk profile {row.id} not found")
                for k, v in row.model_dump(exclude={"id"}).items():
                    setattr(existing, k, v)
                row = existing
            db.add(row)
            db.commit()
            db.refresh(row)
            return row

    def delete_risk_profile(self, profile_id: int) -> None:
        with self._db() as db:
            if db.exec(select(AssignmentRow).where(AssignmentRow.risk_profile_id == profile_id)).first():
                raise ValueError("risk profile is used by an assignment")
            row = db.get(RiskProfileRow, profile_id)
            if row is None:
                raise NotFound(f"risk profile {profile_id} not found")
            db.delete(row)
            db.commit()

    @staticmethod
    def to_profile(row: RiskProfileRow) -> RiskProfile:
        return RiskProfile(**row.model_dump(exclude={"id"}))

    # ------------------------------------------------------------------ trades
    def add_trade(self, row: TradeRow) -> TradeRow:
        with self._db() as db:
            db.add(row)
            db.commit()
            db.refresh(row)
            return row

    def open_trades(self, session_id: int | None = None) -> list[TradeRow]:
        with self._db() as db:
            q = select(TradeRow).where(TradeRow.status == "open")
            if session_id is not None:
                q = q.where(TradeRow.session_id == session_id)
            return list(db.exec(q))

    def trade_by_ticket(self, ticket: int) -> TradeRow | None:
        with self._db() as db:
            return db.exec(select(TradeRow).where(TradeRow.ticket == ticket)).first()

    def update_trade(self, ticket: int, **fields: Any) -> TradeRow:
        with self._db() as db:
            row = db.exec(select(TradeRow).where(TradeRow.ticket == ticket)).one()
            for k, v in fields.items():
                setattr(row, k, v)
            db.add(row)
            db.commit()
            db.refresh(row)
            return row

    def trades(
        self,
        session_id: int | None = None,
        symbol: str | None = None,
        status: str | None = None,
        since: int | None = None,
        until: int | None = None,
        limit: int = 1000,
    ) -> list[TradeRow]:
        with self._db() as db:
            q = select(TradeRow)
            if session_id is not None:
                q = q.where(TradeRow.session_id == session_id)
            if symbol:
                q = q.where(TradeRow.symbol == symbol)
            if status:
                q = q.where(TradeRow.status == status)
            if since is not None:
                q = q.where(TradeRow.open_time >= since)
            if until is not None:
                q = q.where(TradeRow.open_time <= until)
            return list(db.exec(q.order_by(TradeRow.open_time.desc()).limit(limit)))

    def realized_since(self, since: int, session_id: int | None = None) -> float:
        """Realised P&L of trades closed since ``since``: one Session's, or the Account's (Paper
        trades excluded — they never touched the Account)."""
        with self._db() as db:
            q = select(func.coalesce(func.sum(TradeRow.profit), 0.0)).where(
                TradeRow.status == "closed", TradeRow.close_time >= since
            )
            if session_id is not None:
                q = q.where(TradeRow.session_id == session_id)
            else:
                q = q.where(TradeRow.paper == False)  # noqa: E712
            return float(db.exec(q).one())

    # ----------------------------------------------------------------- journal
    def add_journal(self, row: JournalRow) -> JournalRow:
        with self._db() as db:
            db.add(row)
            db.commit()
            db.refresh(row)
            return row

    def journal(
        self,
        session_id: int | None = None,
        kinds: Iterable[str] | None = None,
        level: str | None = None,
        alerts_only: bool = False,
        symbol: str | None = None,
        before_id: int | None = None,
        limit: int = 200,
    ) -> list[JournalRow]:
        with self._db() as db:
            q = select(JournalRow)
            if session_id is not None:
                q = q.where(JournalRow.session_id == session_id)
            kinds = [k for k in (kinds or []) if k]
            if kinds:
                q = q.where(JournalRow.kind.in_(kinds))
            if level:
                q = q.where(JournalRow.level == level)
            if alerts_only:
                q = q.where(JournalRow.alert == True)  # noqa: E712
            if symbol:
                q = q.where(JournalRow.symbol == symbol)
            if before_id:
                q = q.where(JournalRow.id < before_id)
            return list(db.exec(q.order_by(JournalRow.id.desc()).limit(limit)))

    def last_server_ts(self) -> int:
        """Latest broker server time this database has recorded anything at (0 if empty)."""
        with self._db() as db:
            j = db.exec(select(func.coalesce(func.max(JournalRow.ts), 0))).one()
            t = db.exec(select(func.coalesce(func.max(TradeRow.close_time), 0))).one()
            o = db.exec(select(func.coalesce(func.max(TradeRow.open_time), 0))).one()
            e = db.exec(select(func.coalesce(func.max(EquityRow.ts), 0))).one()
            return int(max(j or 0, t or 0, o or 0, e or 0))

    # ------------------------------------------------------------ paper book
    def paper_open(self) -> list[PaperPositionRow]:
        with self._db() as db:
            return list(db.exec(select(PaperPositionRow).where(PaperPositionRow.status == "open").order_by(PaperPositionRow.ticket.desc())))

    def paper_add(self, row: PaperPositionRow) -> None:
        with self._db() as db:
            db.add(row)
            db.commit()

    def paper_update(self, ticket: int, **fields: Any) -> None:
        with self._db() as db:
            row = db.get(PaperPositionRow, ticket)
            if row is None:
                raise NotFound(f"paper position {ticket} not found")
            for k, v in fields.items():
                setattr(row, k, v)
            db.add(row)
            db.commit()

    def paper_closed_since(self, since: int) -> list[PaperPositionRow]:
        with self._db() as db:
            q = select(PaperPositionRow).where(PaperPositionRow.status == "closed", PaperPositionRow.time_close >= since)
            return list(db.exec(q))

    def paper_next_ticket(self) -> int:
        with self._db() as db:
            low = db.exec(select(func.min(PaperPositionRow.ticket))).one()
            return min(int(low or 0), 0) - 1

    # ------------------------------------------------------------------ equity
    def add_equity(self, ts: int, balance: float, equity: float) -> None:
        with self._db() as db:
            db.add(EquityRow(ts=ts, balance=balance, equity=equity))
            db.commit()

    def last_equity_ts(self) -> int:
        with self._db() as db:
            return int(db.exec(select(func.coalesce(func.max(EquityRow.ts), 0))).one())

    def equity_curve(self, since: int = 0, limit: int = 2000) -> list[EquityRow]:
        with self._db() as db:
            rows = list(db.exec(select(EquityRow).where(EquityRow.ts >= since).order_by(EquityRow.ts.desc()).limit(limit)))
            return rows[::-1]


def now_wall() -> float:
    return time.time()
