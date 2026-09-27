"""Persistence for configuration, trades, Journal and equity. SQLite via SQLModel.

Every method opens its own short DB session and returns detached rows, so
callers never hold a connection across an ``await``.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import event, func, or_
from sqlmodel import Session as DB
from sqlmodel import SQLModel, create_engine, select

from ..risk import RiskLimits, RiskProfile, TradingWindow
from .models import (
    AssignmentRow,
    CostOverrideRow,
    EquityRow,
    JournalRow,
    LearningSlotRow,
    MagicIdentityRow,
    PaperPositionRow,
    PendingChangeRow,
    PendingEntryRow,
    RiskProfileRow,
    SessionRow,
    SettingRow,
    TradeRow,
)

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
    "cost_check_max_r": 0.15,  # Cost Check: spread + slippage above this share of the stop blocks a start (D14)
}

DEFAULT_LIVE_CAPS: dict[str, float] = {"max_risk_pct": 1.0, "max_volume": 0.10}
DEFAULT_PAPER_ACCOUNT: dict[str, float] = {"start_balance": 5000.0, "epoch": 0}  # the notional pool Paper Sessions share (D8)
DEFAULT_FLOOR_PCT = 80.0

DEFAULT_PROFILES = (
    RiskProfileRow(name="Default", risk_pct=1.0, max_spread_points=30),
    RiskProfileRow(name="Conservative", risk_pct=0.5, max_spread_points=20, breakeven=True, breakeven_at_r=1.0),
    RiskProfileRow(name="Aggressive", risk_pct=2.0, max_spread_points=40, trailing=True, trailing_atr=2.0, trailing_start_r=1.0),
    RiskProfileRow(name="Gold", risk_pct=1.0, max_spread_points=60, breakeven=True, trailing=True, trailing_atr=2.5),
    # GOLD Reopen Drift enters at the 01:00 reopen, where XM quotes ~70 points; its exit is by time
    RiskProfileRow(name="Gold Reopen", risk_pct=1.0, max_spread_points=100),
)
FIRST_PROFILES = ("Default", "Conservative", "Aggressive", "Gold")  # seeded before profiles were tracked


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
        self._migrate_magics()

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
        """Add each default Risk Profile once. A default added in a later version reaches existing
        databases too, but one the operator deleted or renamed is never brought back."""
        with self._db() as db:
            empty = db.exec(select(RiskProfileRow)).first() is None
            seeded = [] if empty else list(self.get_setting("seeded_profiles") or FIRST_PROFILES)
            names = set(db.exec(select(RiskProfileRow.name)).all())
            added = False
            for p in DEFAULT_PROFILES:
                if p.name in seeded:
                    continue
                if p.name not in names:
                    db.add(RiskProfileRow.model_validate(p.model_dump(exclude={"id"})))
                    added = True
                seeded.append(p.name)
            if added:
                db.commit()
        if self.get_setting("seeded_profiles") != seeded:
            self.set_setting("seeded_profiles", seeded)

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
            used = max(db.exec(select(func.max(SessionRow.magic))).one() or 0, db.exec(select(func.max(MagicIdentityRow.magic))).one() or 0)
        magic = max(magic, used + 1)
        self.set_setting("next_magic", magic + 1)
        return magic

    # ------------------------------------------------ magic identities (ADR 0009)
    def magic_for(self, session_id: int, symbol: str, strategy: str, timeframe: str, prefer: int | None = None) -> int:
        """The Magic Number of an Assignment identity, created on first use. ``prefer`` (a Session's
        own magic) is taken when no identity holds it yet: a new Session's first Assignment and the
        migration of Sessions from before ADR 0009."""
        with self._db() as db:
            row = db.exec(
                select(MagicIdentityRow).where(
                    MagicIdentityRow.session_id == session_id,
                    MagicIdentityRow.symbol == symbol,
                    MagicIdentityRow.strategy == strategy,
                    MagicIdentityRow.timeframe == timeframe,
                )
            ).first()
            if row is not None:
                return row.magic
            taken = prefer is not None and db.exec(select(MagicIdentityRow).where(MagicIdentityRow.magic == prefer)).first() is not None
        magic = prefer if prefer is not None and not taken else self.allocate_magic()
        with self._db() as db:
            db.add(MagicIdentityRow(session_id=session_id, symbol=symbol, strategy=strategy, timeframe=timeframe, magic=magic, created_wall=time.time()))
            db.commit()
        return magic

    def magic_identities(self) -> dict[int, MagicIdentityRow]:
        """magic -> identity, for every identity ever created."""
        with self._db() as db:
            return {r.magic: r for r in db.exec(select(MagicIdentityRow))}

    def session_magics(self, session_id: int) -> set[int]:
        """Every magic the Session's positions may carry: its own and each identity it ever had."""
        with self._db() as db:
            own = db.get(SessionRow, session_id)
            ids = set(db.exec(select(MagicIdentityRow.magic).where(MagicIdentityRow.session_id == session_id)))
        return ids | ({own.magic} if own else set())

    def _bind_magics(self, session: SessionRow) -> None:
        """Stamp each Assignment with its identity's magic. The first Assignment gets the Session's own
        magic, unless an identity already holds it."""
        for i, a in enumerate(self.assignments(session.id)):
            m = self.magic_for(session.id, a.symbol, a.strategy, a.timeframe, prefer=session.magic if i == 0 else None)
            if a.magic != m:
                self.update_assignment(a.id, magic=m)

    def _migrate_magics(self) -> None:
        """Sessions from before ADR 0009 had one magic: it moves to their first Assignment and the
        others get new ones. Learning slots and pending changes gain the Assignment's timeframe
        (a Symbol appeared once per Session then, so it is unambiguous). Idempotent."""
        with self._db() as db:
            unbound = {a.session_id for a in db.exec(select(AssignmentRow).where(AssignmentRow.magic == 0))}
            sessions = [db.get(SessionRow, sid) for sid in sorted(unbound)]
            tf = {(a.session_id, a.symbol): a.timeframe for a in db.exec(select(AssignmentRow))}
            changed = False
            for model in (LearningSlotRow, PendingChangeRow):
                for row in db.exec(select(model).where(model.timeframe == "")):
                    if (row.session_id, row.symbol) in tf:
                        row.timeframe = tf[(row.session_id, row.symbol)]
                        db.add(row)
                        changed = True
            if changed:
                db.commit()
        for sess in sessions:
            if sess is not None:
                self._bind_magics(sess)

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
        """magic -> session id for every existing Session: its own magic and every Assignment magic
        it ever had (ownership test: the Kill Switch, global counts, Orphans)."""
        with self._db() as db:
            out = {m: i for i, m in db.exec(select(SessionRow.id, SessionRow.magic))}
            ids = set(out.values())
            for r in db.exec(select(MagicIdentityRow)):
                if r.session_id in ids:
                    out[r.magic] = r.session_id
            return out

    def allocate_session_id(self) -> int:
        """A Session id never used before. SQLite hands out max(id) + 1, so deleting the newest
        Session would give its id, and with it its trades, Journal, Learning records and that day's
        realized P&L, to the next Session. Ids are therefore allocated above every session_id any
        table has ever recorded (deleted Sessions' magic identities hold the negated id)."""
        top = int(self.get_setting("next_session_id", 1)) - 1
        with self._db() as db:
            for table in SQLModel.metadata.sorted_tables:
                col = table.c.get("id") if table.name == SessionRow.__tablename__ else table.c.get("session_id")
                if col is not None:
                    top = max(top, int(db.exec(select(func.max(func.abs(col)))).one() or 0))
        self.set_setting("next_session_id", top + 2)
        return top + 1

    def save_session(self, row: SessionRow, assignments: list[AssignmentRow] | None = None) -> SessionRow:
        if row.id is None:
            row.id = self.allocate_session_id()
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
        if assignments is not None:
            self._bind_magics(row)
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
            # SQLite may give a later Session this id again: its identities must not match that one.
            # They stay (their magics are never reused) under the negated id.
            for ident in db.exec(select(MagicIdentityRow).where(MagicIdentityRow.session_id == session_id)):
                ident.session_id = -session_id
                db.add(ident)
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
        if {"symbol", "strategy", "timeframe"} & fields.keys():  # a new identity (e.g. a Promotion)
            m = self.magic_for(row.session_id, row.symbol, row.strategy, row.timeframe)
            if m != row.magic:
                return self.update_assignment(assignment_id, magic=m)
        return row

    def assignments(self, session_id: int) -> list[AssignmentRow]:
        with self._db() as db:
            return list(db.exec(select(AssignmentRow).where(AssignmentRow.session_id == session_id).order_by(AssignmentRow.id)))

    def all_assignments(self) -> list[AssignmentRow]:
        with self._db() as db:
            return list(db.exec(select(AssignmentRow)))

    # ------------------------------------------------------------ cost overrides
    def cost_override(self, session_id: int, symbol: str, timeframe: str) -> CostOverrideRow | None:
        with self._db() as db:
            q = select(CostOverrideRow).where(
                CostOverrideRow.session_id == session_id, CostOverrideRow.symbol == symbol, CostOverrideRow.timeframe == timeframe
            )
            return db.exec(q).first()

    def set_cost_override(self, session_id: int, symbol: str, timeframe: str, enabled: bool, reason: str = "", ts: int = 0) -> None:
        with self._db() as db:
            for row in db.exec(select(CostOverrideRow).where(
                CostOverrideRow.session_id == session_id, CostOverrideRow.symbol == symbol, CostOverrideRow.timeframe == timeframe
            )):
                db.delete(row)
            if enabled:
                db.add(CostOverrideRow(session_id=session_id, symbol=symbol, timeframe=timeframe, reason=reason, set_ts=ts))
            db.commit()

    # ---------------------------------------------------------- pending entries
    def add_pending_entry(self, row: PendingEntryRow) -> PendingEntryRow:
        with self._db() as db:
            db.add(row)
            db.commit()
            db.refresh(row)
            return row

    def pending_entries(self, session_id: int | None = None, status: str | None = "pending") -> list[PendingEntryRow]:
        with self._db() as db:
            q = select(PendingEntryRow)
            if session_id is not None:
                q = q.where(PendingEntryRow.session_id == session_id)
            if status is not None:
                q = q.where(PendingEntryRow.status == status)
            return list(db.exec(q.order_by(PendingEntryRow.id)))

    def update_pending_entry(self, pending_id: int, **fields: Any) -> PendingEntryRow:
        with self._db() as db:
            row = db.get(PendingEntryRow, pending_id)
            if row is None:
                raise NotFound(f"pending entry {pending_id} not found")
            for k, v in fields.items():
                setattr(row, k, v)
            db.add(row)
            db.commit()
            db.refresh(row)
            return row

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

    def realized_since(self, since: int, session_id: int | None = None, login: int | None = None) -> float:
        """Realised P&L of trades closed since ``since``: one Session's, or the Account's (Paper
        trades excluded — they never touched the Account). With ``login``, only that Account's trades
        (and legacy rows without a login)."""
        with self._db() as db:
            q = select(func.coalesce(func.sum(TradeRow.profit), 0.0)).where(
                TradeRow.status == "closed", TradeRow.close_time >= since
            )
            if login is not None:
                q = q.where(or_(TradeRow.login == login, TradeRow.login.is_(None)))
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
        """Paper tickets only ever go down, so none is reused, even after a reset (``TradeRow.ticket``
        is unique across real and Paper trades)."""
        with self._db() as db:
            low = min(
                int(db.exec(select(func.min(PaperPositionRow.ticket))).one() or 0),
                int(db.exec(select(func.min(TradeRow.ticket))).one() or 0),
                int(self.get_setting("paper_last_ticket", 0) or 0),
                0,
            )
        self.set_setting("paper_last_ticket", low - 1)
        return low - 1

    def paper_epoch(self) -> int:
        return int(self.paper_account()["epoch"])

    def paper_account(self) -> dict:
        return {**DEFAULT_PAPER_ACCOUNT, **(self.get_setting("paper_account", {}) or {})}

    def set_paper_account(self, **fields: Any) -> dict:
        cur = {**self.paper_account(), **fields}
        self.set_setting("paper_account", cur)
        return cur

    def realized_paper(self, since: int = 0, session_id: int | None = None, epoch: int | None = None) -> float:
        """Realised P&L of the Paper Account's closed trades (this epoch unless given), since ``since``."""
        epoch = self.paper_epoch() if epoch is None else epoch
        with self._db() as db:
            q = select(func.coalesce(func.sum(TradeRow.profit), 0.0)).where(
                TradeRow.status == "closed", TradeRow.paper == True, TradeRow.paper_epoch == epoch  # noqa: E712
            )
            if since:
                q = q.where(TradeRow.close_time >= since)
            if session_id is not None:
                q = q.where(TradeRow.session_id == session_id)
            return float(db.exec(q).one())

    def paper_closed_trades(self, symbol: str, timeframe: str, strategy: str | None = None, epoch: int | None = None) -> list[TradeRow]:
        """The Paper Account's closed trades on an Arena (this epoch unless given), oldest first."""
        epoch = self.paper_epoch() if epoch is None else epoch
        with self._db() as db:
            q = select(TradeRow).where(
                TradeRow.status == "closed", TradeRow.paper == True, TradeRow.paper_epoch == epoch,  # noqa: E712
                TradeRow.symbol == symbol, TradeRow.timeframe == timeframe,
            )
            if strategy:
                q = q.where(TradeRow.strategy == strategy)
            return list(db.exec(q.order_by(TradeRow.close_time)))

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
