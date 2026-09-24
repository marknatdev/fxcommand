"""Persistence for learning records (see store.models, learning section)."""

from __future__ import annotations

import time
from typing import Any

from sqlmodel import Session as DB
from sqlmodel import select

from ..store import Store
from ..store.models import (
    CandidateRow,
    ChallengerRow,
    ChampionVersionRow,
    FilterStateRow,
    LearningSlotRow,
    OptimizerRunRow,
    PendingChangeRow,
    ShadowTradeRow,
    SignalRecordRow,
)
from .candidate import Candidate


class LearningRepo:
    def __init__(self, store: Store):
        self.engine = store.engine

    def _db(self) -> DB:
        return DB(self.engine, expire_on_commit=False)

    def _save(self, row):
        with self._db() as db:
            db.add(row)
            db.commit()
            db.refresh(row)
            return row

    def _update(self, model, pk, **fields):
        with self._db() as db:
            row = db.get(model, pk)
            if row is None:
                return None
            for k, v in fields.items():
                setattr(row, k, v)
            db.add(row)
            db.commit()
            db.refresh(row)
            return row

    # ---------------------------------------------------------- candidates
    def ensure_candidate(self, c: Candidate) -> None:
        with self._db() as db:
            if db.get(CandidateRow, c.key) is None:
                db.add(CandidateRow(key=c.key, strategy=c.strategy, params=c.param_dict, created=time.time()))
                db.commit()

    def candidate(self, key: str) -> Candidate | None:
        with self._db() as db:
            row = db.get(CandidateRow, key)
            return Candidate.of(row.strategy, row.params) if row else None

    def candidates(self, keys: list[str]) -> dict[str, Candidate]:
        with self._db() as db:
            rows = db.exec(select(CandidateRow).where(CandidateRow.key.in_(keys))).all() if keys else []
            return {r.key: Candidate.of(r.strategy, r.params) for r in rows}

    # ------------------------------------------------------ champion versions
    def versions(self, session_id: int, symbol: str) -> list[ChampionVersionRow]:
        with self._db() as db:
            q = select(ChampionVersionRow).where(ChampionVersionRow.session_id == session_id, ChampionVersionRow.symbol == symbol)
            return list(db.exec(q.order_by(ChampionVersionRow.version)))

    def arena_versions(self, symbol: str, timeframe: str, limit: int = 50) -> list[ChampionVersionRow]:
        with self._db() as db:
            q = select(ChampionVersionRow).where(ChampionVersionRow.symbol == symbol, ChampionVersionRow.timeframe == timeframe)
            return list(db.exec(q.order_by(ChampionVersionRow.id.desc()).limit(limit)))

    def add_version(self, **fields: Any) -> ChampionVersionRow:
        prior = self.versions(fields["session_id"], fields["symbol"])
        fields.setdefault("version", (prior[-1].version + 1) if prior else 1)
        fields.setdefault("previous_key", prior[-1].candidate_key if prior else None)
        return self._save(ChampionVersionRow(wall=time.time(), **fields))

    # ----------------------------------------------------------------- slots
    def slot(self, session_id: int, symbol: str) -> LearningSlotRow:
        with self._db() as db:
            row = db.exec(select(LearningSlotRow).where(LearningSlotRow.session_id == session_id, LearningSlotRow.symbol == symbol)).first()
            if row is None:
                row = LearningSlotRow(session_id=session_id, symbol=symbol)
                db.add(row)
                db.commit()
                db.refresh(row)
            return row

    def set_auto_promote(self, session_id: int, symbol: str, enabled: bool) -> LearningSlotRow:
        row = self.slot(session_id, symbol)
        return self._update(LearningSlotRow, row.id, auto_promote=bool(enabled))

    # ----------------------------------------------------------- challengers
    def challengers(self, symbol: str, timeframe: str, status: str | None = "active") -> list[ChallengerRow]:
        with self._db() as db:
            q = select(ChallengerRow).where(ChallengerRow.symbol == symbol, ChallengerRow.timeframe == timeframe)
            if status:
                q = q.where(ChallengerRow.status == status)
            return list(db.exec(q.order_by(ChallengerRow.id)))

    def challenger(self, challenger_id: int) -> ChallengerRow | None:
        with self._db() as db:
            return db.get(ChallengerRow, challenger_id)

    def add_challenger(self, **fields: Any) -> ChallengerRow:
        return self._save(ChallengerRow(**fields))

    def update_challenger(self, challenger_id: int, **fields: Any) -> ChallengerRow | None:
        return self._update(ChallengerRow, challenger_id, **fields)

    # --------------------------------------------------------- shadow trades
    def open_shadows(self, symbol: str, timeframe: str) -> list[ShadowTradeRow]:
        with self._db() as db:
            q = select(ShadowTradeRow).where(ShadowTradeRow.symbol == symbol, ShadowTradeRow.timeframe == timeframe, ShadowTradeRow.status == "open")
            return list(db.exec(q))

    def add_shadow(self, **fields: Any) -> ShadowTradeRow:
        return self._save(ShadowTradeRow(**fields))

    def close_shadow(self, shadow_id: int, **fields: Any) -> None:
        self._update(ShadowTradeRow, shadow_id, status="closed", **fields)

    def void_open_shadows(self) -> int:
        with self._db() as db:
            rows = list(db.exec(select(ShadowTradeRow).where(ShadowTradeRow.status == "open")))
            for r in rows:
                r.status = "void"
                db.add(r)
            db.commit()
            return len(rows)

    def shadow_closed(self, symbol: str, timeframe: str, key: str | None = None, since: int = 0, limit: int = 5000) -> list[ShadowTradeRow]:
        with self._db() as db:
            q = select(ShadowTradeRow).where(
                ShadowTradeRow.symbol == symbol,
                ShadowTradeRow.timeframe == timeframe,
                ShadowTradeRow.status == "closed",
                ShadowTradeRow.open_ts >= since,
            )
            if key:
                q = q.where(ShadowTradeRow.candidate_key == key)
            return list(db.exec(q.order_by(ShadowTradeRow.close_ts).limit(limit)))

    def shadow_samples(self, symbol: str, timeframe: str, strategy: str, limit: int = 3000) -> list[tuple[list, float]]:
        """(features, R) of closed Shadow Trades of every Candidate of ``strategy`` in the Arena, oldest first."""
        with self._db() as db:
            keys = [r.key for r in db.exec(select(CandidateRow).where(CandidateRow.strategy == strategy))]
            if not keys:
                return []
            q = select(ShadowTradeRow).where(
                ShadowTradeRow.symbol == symbol,
                ShadowTradeRow.timeframe == timeframe,
                ShadowTradeRow.status == "closed",
                ShadowTradeRow.candidate_key.in_(keys),
            )
            rows = list(db.exec(q.order_by(ShadowTradeRow.close_ts.desc()).limit(limit)))[::-1]
            return [(r.features, float(r.r)) for r in rows if r.features and r.r is not None]

    # ---------------------------------------------------------- optimizer runs
    def add_run(self, **fields: Any) -> OptimizerRunRow:
        return self._save(OptimizerRunRow(queued_wall=time.time(), **fields))

    def update_run(self, run_id: int, **fields: Any) -> OptimizerRunRow | None:
        return self._update(OptimizerRunRow, run_id, **fields)

    def runs(self, symbol: str | None = None, timeframe: str | None = None, limit: int = 20) -> list[OptimizerRunRow]:
        with self._db() as db:
            q = select(OptimizerRunRow)
            if symbol:
                q = q.where(OptimizerRunRow.symbol == symbol)
            if timeframe:
                q = q.where(OptimizerRunRow.timeframe == timeframe)
            return list(db.exec(q.order_by(OptimizerRunRow.id.desc()).limit(limit)))

    def run(self, run_id: int) -> OptimizerRunRow | None:
        with self._db() as db:
            return db.get(OptimizerRunRow, run_id)

    # ---------------------------------------------------------------- filters
    def filter_state(self, key: str) -> FilterStateRow | None:
        with self._db() as db:
            return db.get(FilterStateRow, key)

    def save_filter(self, key: str, state: dict, trained_on: int) -> None:
        with self._db() as db:
            row = db.get(FilterStateRow, key)
            if row is None:
                row = FilterStateRow(key=key)
            row.state, row.trained_on = state, trained_on
            db.add(row)
            db.commit()

    def filters(self) -> list[FilterStateRow]:
        with self._db() as db:
            return list(db.exec(select(FilterStateRow)))

    # ---------------------------------------------------------------- signals
    def add_signal(self, **fields: Any) -> SignalRecordRow:
        return self._save(SignalRecordRow(**fields))

    def update_signal(self, record_id: int, **fields: Any) -> None:
        self._update(SignalRecordRow, record_id, **fields)

    def signal_by_ticket(self, ticket: int) -> SignalRecordRow | None:
        with self._db() as db:
            return db.exec(select(SignalRecordRow).where(SignalRecordRow.ticket == ticket)).first()

    def signals(self, symbol: str, timeframe: str, limit: int = 50) -> list[SignalRecordRow]:
        with self._db() as db:
            q = select(SignalRecordRow).where(SignalRecordRow.symbol == symbol, SignalRecordRow.timeframe == timeframe)
            return list(db.exec(q.order_by(SignalRecordRow.id.desc()).limit(limit)))

    def live_rs(self, session_id: int, symbol: str, since: int) -> list[float]:
        with self._db() as db:
            q = select(SignalRecordRow).where(
                SignalRecordRow.session_id == session_id,
                SignalRecordRow.symbol == symbol,
                SignalRecordRow.ts >= since,
                SignalRecordRow.r != None,  # noqa: E711
            )
            return [float(r.r) for r in db.exec(q.order_by(SignalRecordRow.ts))]

    # ---------------------------------------------------------------- pending
    def add_pending(self, **fields: Any) -> PendingChangeRow:
        with self._db() as db:  # one pending change per slot: a newer request replaces an older one
            for old in db.exec(
                select(PendingChangeRow).where(
                    PendingChangeRow.session_id == fields["session_id"],
                    PendingChangeRow.symbol == fields["symbol"],
                    PendingChangeRow.status == "pending",
                )
            ):
                old.status = "cancelled"
                db.add(old)
            db.commit()
        return self._save(PendingChangeRow(created_wall=time.time(), **fields))

    def pending(self, session_id: int, symbol: str) -> PendingChangeRow | None:
        with self._db() as db:
            q = select(PendingChangeRow).where(
                PendingChangeRow.session_id == session_id, PendingChangeRow.symbol == symbol, PendingChangeRow.status == "pending"
            )
            return db.exec(q).first()

    def all_pending(self) -> list[PendingChangeRow]:
        with self._db() as db:
            return list(db.exec(select(PendingChangeRow).where(PendingChangeRow.status == "pending")))

    def update_pending(self, pending_id: int, **fields: Any) -> None:
        self._update(PendingChangeRow, pending_id, **fields)

    # -------------------------------------------------------------- retention
    def cleanup(self, older_than_ts: int, keep_runs: int = 20) -> dict:
        from sqlmodel import delete

        with self._db() as db:
            s = db.exec(delete(ShadowTradeRow).where(ShadowTradeRow.status != "open", ShadowTradeRow.open_ts < older_than_ts))
            g = db.exec(delete(SignalRecordRow).where(SignalRecordRow.ts < older_than_ts))
            removed_runs = 0
            arenas = {(r.symbol, r.timeframe) for r in db.exec(select(OptimizerRunRow))}
            for sym, tf in arenas:
                ids = [r.id for r in db.exec(select(OptimizerRunRow).where(OptimizerRunRow.symbol == sym, OptimizerRunRow.timeframe == tf).order_by(OptimizerRunRow.id.desc()))]
                for rid in ids[keep_runs:]:
                    db.delete(db.get(OptimizerRunRow, rid))
                    removed_runs += 1
            db.commit()
            return {"shadow_trades": s.rowcount, "signals": g.rowcount, "runs": removed_runs}
