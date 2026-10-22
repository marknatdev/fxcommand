"""The Ledger: the permanent, append-only record of every Idea Family, Hypothesis and Trial.

ADR 0003 counts every walk-forward run as a Trial against its Idea Family's budget, and deflates
the Pass Bar by every grid combination the family has evaluated. Those numbers are honest only
if no row can change after it is written. So every table has triggers that abort UPDATE and
DELETE, and a change of state (a budget grant, a Holdout approval, a spend) is a new row. The
app process is the only writer (ADR 0006): one connection, with a lock around each transaction.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import secrets
import sqlite3
import statistics
import threading
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

import numpy as np

from vibedesk.blocks.spec import Combination, StrategySpec, expand_grid, validate
from vibedesk.engine.costs import CostSnapshot
from vibedesk.market.bars import Bars
from vibedesk.market.grant import HoldoutGrant

TRIAL_BUDGET = 25  # Trials an Idea Family may run before the owner must extend its budget
BUDGET_STEP = 25  # the owner's usual grant (spec §6.4)
TRIAL_VERDICTS = ("CANDIDATE", "REJECT")
HOLDOUT_VERDICTS = ("PASS", "HOLDOUT FAIL")

# Migration 1's tables. Never add to this list: a later table's own migration calls
# _append_only for it, or a fresh database's migration 1 would reference a missing table.
_TABLES = (
    "families",
    "hypotheses",
    "trials",
    "trial_combinations",
    "budget_grants",
    "holdout_requests",
    "holdout_approvals",
    "holdout_spends",
)


def _append_only(*tables: str) -> str:
    """Triggers that make each table insert-only. With recursive_triggers on, they also stop
    the REPLACE conflict resolution, which would otherwise delete the old row silently."""
    return "\n".join(
        f"CREATE TRIGGER {table}_no_{action.lower()} BEFORE {action} ON {table}\n"
        f"BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END;"
        for table in tables
        for action in ("UPDATE", "DELETE")
    )


_NOW = "strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"

# Applied once each, in order; PRAGMA user_version counts how many have run. An entry never
# changes after it ships: a schema change is a new entry.
MIGRATIONS: tuple[str, ...] = (
    f"""
    CREATE TABLE families (
        id INTEGER PRIMARY KEY,
        idea TEXT NOT NULL CHECK (length(trim(idea)) > 0),
        created_at TEXT NOT NULL DEFAULT ({_NOW})
    );

    CREATE TABLE hypotheses (
        id INTEGER PRIMARY KEY,
        family_id INTEGER NOT NULL REFERENCES families (id),
        spec_json TEXT NOT NULL,
        entry_block TEXT NOT NULL,
        symbol TEXT NOT NULL,
        timeframe TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT ({_NOW})
    );
    CREATE INDEX hypotheses_by_entry_and_symbol ON hypotheses (entry_block, symbol);

    -- ADR 0003: an (entry Block, symbol) pair belongs to the first family that registered it,
    -- so a renamed Idea cannot start its Trial count again.
    CREATE TRIGGER hypotheses_one_family BEFORE INSERT ON hypotheses
    WHEN EXISTS (
        SELECT 1 FROM hypotheses
        WHERE entry_block = NEW.entry_block AND symbol = NEW.symbol
          AND family_id <> NEW.family_id
    )
    BEGIN SELECT RAISE(ABORT, 'this entry block and symbol belong to another Idea Family'); END;

    CREATE TABLE trials (
        id INTEGER PRIMARY KEY,
        hypothesis_id INTEGER NOT NULL REFERENCES hypotheses (id),
        verdict TEXT NOT NULL CHECK (verdict IN ('CANDIDATE', 'REJECT')),
        closest_miss_json TEXT,
        cost_json TEXT NOT NULL,
        bars_count INTEGER NOT NULL CHECK (bars_count > 0),
        bars_first INTEGER NOT NULL,
        bars_last INTEGER NOT NULL,
        bars_sha256 TEXT NOT NULL,
        result_json TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT ({_NOW}),
        CHECK ((verdict = 'REJECT') = (closest_miss_json IS NOT NULL))
    );

    -- One row per grid combination a Trial evaluated: N and V[SR] for the Deflated Sharpe.
    CREATE TABLE trial_combinations (
        trial_id INTEGER NOT NULL REFERENCES trials (id),
        position INTEGER NOT NULL,
        label TEXT NOT NULL,
        blind_sharpe REAL NOT NULL,
        PRIMARY KEY (trial_id, position),
        UNIQUE (trial_id, label)
    );

    CREATE TABLE budget_grants (
        id INTEGER PRIMARY KEY,
        family_id INTEGER NOT NULL REFERENCES families (id),
        amount INTEGER NOT NULL CHECK (amount > 0),
        created_at TEXT NOT NULL DEFAULT ({_NOW})
    );

    CREATE TABLE holdout_requests (
        id INTEGER PRIMARY KEY,
        hypothesis_id INTEGER NOT NULL REFERENCES hypotheses (id),
        created_at TEXT NOT NULL DEFAULT ({_NOW})
    );

    CREATE TABLE holdout_approvals (
        id INTEGER PRIMARY KEY,
        request_id INTEGER NOT NULL UNIQUE REFERENCES holdout_requests (id),
        token TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT ({_NOW})
    );

    -- ADR 0002: each Idea Family spends its Holdout at most once, so family_id is UNIQUE.
    CREATE TABLE holdout_spends (
        id INTEGER PRIMARY KEY,
        family_id INTEGER NOT NULL UNIQUE REFERENCES families (id),
        request_id INTEGER NOT NULL UNIQUE REFERENCES holdout_approvals (request_id),
        verdict TEXT NOT NULL CHECK (verdict IN ('PASS', 'HOLDOUT FAIL')),
        result_json TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT ({_NOW})
    );
    """
    + _append_only(*_TABLES),
)


class LedgerError(Exception):
    """A Ledger rule refused the call."""


class NotFound(LedgerError):
    pass


class FamilyConflict(LedgerError):
    """ADR 0003: this entry Block and symbol already belong to another Idea Family."""

    def __init__(self, family_id: int, idea: str, entry_block: str, symbol: str) -> None:
        super().__init__(
            f"{entry_block} on {symbol} already belongs to Idea Family {family_id} ({idea!r}); "
            f"register this Spec in family {family_id}"
        )
        self.family_id = family_id


class BudgetExhausted(LedgerError):
    def __init__(self, family_id: int, used: int, budget: int) -> None:
        super().__init__(
            f"Idea Family {family_id} has run {used} of its {budget} Trials; "
            "only the owner can extend the Trial Budget"
        )
        self.family_id = family_id


class HoldoutSpent(LedgerError):
    def __init__(self, family_id: int) -> None:
        super().__init__(f"Idea Family {family_id} has already spent its Holdout")
        self.family_id = family_id


@dataclass(frozen=True, slots=True)
class Family:
    id: int
    idea: str
    created_at: str


@dataclass(frozen=True, slots=True)
class Hypothesis:
    id: int
    family_id: int
    spec_json: str  # the validated Spec as frozen at registration
    entry_block: str
    symbol: str
    timeframe: str
    created_at: str

    @property
    def spec(self) -> StrategySpec:
        """The frozen Spec, parsed but not re-checked against today's Block Library."""
        return StrategySpec.model_validate_json(self.spec_json)


@dataclass(frozen=True, slots=True)
class BarsFingerprint:
    """Identifies the bars a Trial ran on, so a rerun can show it used the same data."""

    count: int
    first_time: int
    last_time: int
    sha256: str

    @classmethod
    def of(cls, bars: Bars) -> BarsFingerprint:
        if len(bars.time) == 0:
            raise ValueError("a Trial needs at least one bar")
        digest = hashlib.sha256(f"{bars.symbol}|{bars.timeframe}|{bars.point!r}".encode())
        # Fixed little-endian dtypes, so the checksum does not depend on the machine.
        for name, dtype in [
            ("time", "<i8"),
            ("open", "<f8"),
            ("high", "<f8"),
            ("low", "<f8"),
            ("close", "<f8"),
            ("spread", "<i4"),
        ]:
            digest.update(np.ascontiguousarray(getattr(bars, name), dtype=dtype).tobytes())
        return cls(len(bars.time), int(bars.time[0]), int(bars.time[-1]), digest.hexdigest())


@dataclass(frozen=True, slots=True)
class TrialRecord:
    id: int
    hypothesis_id: int
    verdict: str
    closest_miss: dict[str, Any] | None
    combinations: tuple[tuple[str, float], ...]  # (Combination label, blind-year Sharpe)
    cost: CostSnapshot
    bars: BarsFingerprint
    result_json: str
    created_at: str


@dataclass(frozen=True, slots=True)
class HoldoutSpend:
    id: int
    family_id: int
    request_id: int
    verdict: str
    result_json: str
    created_at: str


class Ledger:
    """The SQLite store behind Trial counting, Trial Budgets and Holdout Requests.

    `Ledger(path)` creates or opens the database and applies any MIGRATIONS it has not run.
    `path` may be ":memory:" for a throwaway Ledger.
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self._db = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA foreign_keys = ON")
        self._db.execute("PRAGMA recursive_triggers = ON")  # see _append_only
        self._lock = threading.RLock()
        self._migrate()

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> Ledger:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # Idea Families and Hypotheses

    def create_family(self, idea: str) -> int:
        """Start an Idea Family for the owner's Idea, kept as typed; returns its id."""
        if not idea.strip():
            raise ValueError("an Idea needs some text")
        with self._write() as db:
            return db.execute("INSERT INTO families (idea) VALUES (?)", (idea,)).lastrowid

    def family(self, family_id: int) -> Family:
        with self._lock:
            return Family(**self._family_row(self._db, family_id))

    def register_hypothesis(
        self, family_id: int, spec: StrategySpec | Mapping[str, Any] | str
    ) -> int:
        """Freeze a Spec as a Hypothesis of `family_id`; returns the Hypothesis id.

        The Spec is validated against the Block Library and stored as the validated model's
        JSON, which fixes one symbol and one timeframe. Raises SpecInvalid for a bad Spec, and
        FamilyConflict when another family already holds a Hypothesis with the same entry Block
        and symbol: that Spec belongs in that family, so its Trials count there (ADR 0003).
        """
        if isinstance(spec, StrategySpec):
            spec = spec.model_dump(mode="json")
        frozen = validate(spec)
        entry, symbol = frozen.entry.block, frozen.symbol
        with self._write() as db:
            self._family_row(db, family_id)
            owner = db.execute(
                "SELECT f.id, f.idea FROM hypotheses h JOIN families f ON f.id = h.family_id"
                " WHERE h.entry_block = ? AND h.symbol = ? AND h.family_id <> ? LIMIT 1",
                (entry, symbol, family_id),
            ).fetchone()
            if owner is not None:
                raise FamilyConflict(owner["id"], owner["idea"], entry, symbol)
            return db.execute(
                "INSERT INTO hypotheses (family_id, spec_json, entry_block, symbol, timeframe)"
                " VALUES (?, ?, ?, ?, ?)",
                (family_id, frozen.model_dump_json(), entry, symbol, frozen.timeframe),
            ).lastrowid

    def hypothesis(self, hypothesis_id: int) -> Hypothesis:
        with self._lock:
            return Hypothesis(**self._hypothesis_row(self._db, hypothesis_id))

    # Trials and the Trial Budget

    def record_trial(
        self,
        hypothesis_id: int,
        *,
        combinations: Sequence[tuple[Combination, float]],
        verdict: str,
        closest_miss: Mapping[str, Any] | None,
        cost: CostSnapshot,
        bars: Bars,
        result_json: str,
    ) -> int:
        """Record one Trial of a Hypothesis, complete at write time; returns the Trial id.

        - `combinations`: every grid Combination the Trial evaluated, with its blind-year
          Sharpe. They must be exactly the Hypothesis's grid, so no Trial can shrink N.
        - `verdict`: CANDIDATE or REJECT. A REJECT names its Closest Miss as a JSON object;
          a CANDIDATE passes None.
        - `cost`: the CostSnapshot the Trial ran with.
        - `bars`: the Research-Window bars; only their BarsFingerprint is stored.
        - `result_json`: the full Gauntlet result, which every Verdict Card figure comes from.

        Raises BudgetExhausted once the family has run as many Trials as its budget allows.
        """
        if verdict not in TRIAL_VERDICTS:
            raise ValueError(f"a Trial's verdict is one of {TRIAL_VERDICTS}, not {verdict!r}")
        if (verdict == "REJECT") != (closest_miss is not None):
            raise ValueError("a REJECT names its Closest Miss and a CANDIDATE has none")
        miss_json = None if closest_miss is None else json.dumps(dict(closest_miss), sort_keys=True)
        rows = [(combination.label(), float(sharpe)) for combination, sharpe in combinations]
        _require_finite([sharpe for _, sharpe in rows])
        json.loads(result_json)  # refuse text that is not JSON
        fingerprint = BarsFingerprint.of(bars)

        with self._write() as db:
            hypothesis = Hypothesis(**self._hypothesis_row(db, hypothesis_id))
            if (bars.symbol, str(bars.timeframe)) != (hypothesis.symbol, hypothesis.timeframe):
                raise ValueError(
                    f"bars are {bars.symbol} {bars.timeframe}; Hypothesis {hypothesis_id} trades"
                    f" {hypothesis.symbol} {hypothesis.timeframe}"
                )
            grid = [c.label() for c in expand_grid(validate(hypothesis.spec_json))]
            labels = [label for label, _ in rows]
            if len(labels) != len(grid) or set(labels) != set(grid):
                raise ValueError(
                    f"a Trial evaluates the whole grid: {len(grid)} combinations, each once;"
                    f" got {len(labels)} that do not match it"
                )
            used = self._trial_count(db, hypothesis.family_id)
            budget = self._budget(db, hypothesis.family_id)
            if used >= budget:
                raise BudgetExhausted(hypothesis.family_id, used, budget)
            trial_id = db.execute(
                "INSERT INTO trials (hypothesis_id, verdict, closest_miss_json, cost_json,"
                " bars_count, bars_first, bars_last, bars_sha256, result_json)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    hypothesis_id,
                    verdict,
                    miss_json,
                    cost.to_json(),
                    fingerprint.count,
                    fingerprint.first_time,
                    fingerprint.last_time,
                    fingerprint.sha256,
                    result_json,
                ),
            ).lastrowid
            db.executemany(
                "INSERT INTO trial_combinations (trial_id, position, label, blind_sharpe)"
                " VALUES (?, ?, ?, ?)",
                [(trial_id, i, label, sharpe) for i, (label, sharpe) in enumerate(rows)],
            )
            return trial_id

    def trial(self, trial_id: int) -> TrialRecord:
        with self._lock:
            row = self._db.execute("SELECT * FROM trials WHERE id = ?", (trial_id,)).fetchone()
            if row is None:
                raise NotFound(f"no Trial {trial_id}")
            return self._trial_record(row)

    def trials_for(self, hypothesis_id: int) -> list[TrialRecord]:
        """Every Trial of a Hypothesis, oldest first."""
        with self._lock:
            self._hypothesis_row(self._db, hypothesis_id)
            rows = self._db.execute(
                "SELECT * FROM trials WHERE hypothesis_id = ? ORDER BY id", (hypothesis_id,)
            ).fetchall()
            return [self._trial_record(row) for row in rows]

    def trial_count(self, family_id: int) -> int:
        """Trials recorded across all of the family's Hypotheses."""
        with self._lock:
            self._family_row(self._db, family_id)
            return self._trial_count(self._db, family_id)

    def budget(self, family_id: int) -> int:
        """TRIAL_BUDGET plus every grant the owner has made to the family."""
        with self._lock:
            self._family_row(self._db, family_id)
            return self._budget(self._db, family_id)

    def extend_budget(self, family_id: int, amount: int = BUDGET_STEP) -> int:
        """Record an owner's grant of `amount` more Trials as its own row; returns the new
        budget. Only the owner may call this; the API enforces that in M5."""
        if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
            raise ValueError("a budget grant is a positive whole number of Trials")
        with self._write() as db:
            self._family_row(db, family_id)
            db.execute(
                "INSERT INTO budget_grants (family_id, amount) VALUES (?, ?)", (family_id, amount)
            )
            return self._budget(db, family_id)

    def deflation_inputs(
        self, family_id: int, pending_sharpes: Iterable[float] = ()
    ) -> tuple[int, float]:
        """(N, V[SR]) for the Deflated Sharpe, over every combination of every family Trial.

        N counts each evaluation, so a combination run again in a later Trial counts again
        (ADR 0003). V[SR] is the sample variance (n - 1) of their blind-year Sharpes, and 0.0
        when N < 2. `pending_sharpes` adds the Trial being judged, before it is recorded.
        """
        pending = [float(sharpe) for sharpe in pending_sharpes]
        _require_finite(pending)
        with self._lock:
            self._family_row(self._db, family_id)
            rows = self._db.execute(
                "SELECT c.blind_sharpe FROM trial_combinations c"
                " JOIN trials t ON t.id = c.trial_id"
                " JOIN hypotheses h ON h.id = t.hypothesis_id"
                " WHERE h.family_id = ?",
                (family_id,),
            ).fetchall()
        sharpes = [row[0] for row in rows] + pending
        return len(sharpes), statistics.variance(sharpes) if len(sharpes) > 1 else 0.0

    # The Holdout (ADR 0002)

    def request_holdout(self, hypothesis_id: int) -> int:
        """Record a Holdout Request awaiting the owner; returns its id.

        It records only. The Gauntlet's gate (CANDIDATE, enough expected trades) runs before
        this. Raises HoldoutSpent if the family has already spent its Holdout.
        """
        with self._write() as db:
            family_id = self._hypothesis_row(db, hypothesis_id)["family_id"]
            if self._spent(db, family_id):
                raise HoldoutSpent(family_id)
            return db.execute(
                "INSERT INTO holdout_requests (hypothesis_id) VALUES (?)", (hypothesis_id,)
            ).lastrowid

    def approve_holdout(self, request_id: int) -> HoldoutGrant:
        """Approve a Holdout Request and issue the grant that opens the Holdout's bars.

        Only the owner may call this; the API enforces that in M5. A request is approved at
        most once, and never after its family has spent its Holdout.
        """
        with self._write() as db:
            row = db.execute(
                "SELECT h.family_id, a.id AS approval FROM holdout_requests r"
                " JOIN hypotheses h ON h.id = r.hypothesis_id"
                " LEFT JOIN holdout_approvals a ON a.request_id = r.id"
                " WHERE r.id = ?",
                (request_id,),
            ).fetchone()
            if row is None:
                raise NotFound(f"no Holdout Request {request_id}")
            if row["approval"] is not None:
                raise LedgerError(f"Holdout Request {request_id} is already approved")
            if self._spent(db, row["family_id"]):
                raise HoldoutSpent(row["family_id"])
            token = secrets.token_urlsafe(32)
            db.execute(
                "INSERT INTO holdout_approvals (request_id, token) VALUES (?, ?)",
                (request_id, token),
            )
            return HoldoutGrant(request_id, row["family_id"], token)

    def verify(self, grant: HoldoutGrant, symbol: str) -> bool:
        """The GrantVerifier the market facade asks before it serves Holdout bars.

        True only if the Ledger approved `grant.request_id` with this token, the grant names
        the request's Idea Family, the requested Hypothesis trades `symbol`, and the family
        has not spent its Holdout. It records nothing, so it answers the same until the spend.
        """
        if not _well_formed(grant):
            return False
        with self._lock:
            row = self._approval(self._db, grant.request_id)
            return (
                row is not None
                and _same_token(row["token"], grant.token)
                and row["family_id"] == grant.family_id
                and row["symbol"] == symbol
                and not self._spent(self._db, row["family_id"])
            )

    def record_holdout(self, grant: HoldoutGrant, verdict: str, result_json: str) -> int:
        """Record the family's one Holdout spend, PASS or HOLDOUT FAIL; returns its id.

        The grant must be one the Ledger issued. A second spend in the same family violates
        the UNIQUE constraint on family_id and raises HoldoutSpent from that IntegrityError.
        """
        if verdict not in HOLDOUT_VERDICTS:
            raise ValueError(f"a Holdout verdict is one of {HOLDOUT_VERDICTS}, not {verdict!r}")
        json.loads(result_json)
        with self._write() as db:
            row = self._approval(db, grant.request_id) if _well_formed(grant) else None
            if (
                row is None
                or not _same_token(row["token"], grant.token)
                or row["family_id"] != grant.family_id
            ):
                raise LedgerError("this Holdout grant was not issued by the Ledger")
            try:
                return db.execute(
                    "INSERT INTO holdout_spends (family_id, request_id, verdict, result_json)"
                    " VALUES (?, ?, ?, ?)",
                    (row["family_id"], grant.request_id, verdict, result_json),
                ).lastrowid
            except sqlite3.IntegrityError as exc:
                if "UNIQUE constraint failed: holdout_spends" in str(exc):
                    raise HoldoutSpent(row["family_id"]) from exc
                raise

    def holdout_spend(self, family_id: int) -> HoldoutSpend | None:
        with self._lock:
            self._family_row(self._db, family_id)
            row = self._db.execute(
                "SELECT * FROM holdout_spends WHERE family_id = ?", (family_id,)
            ).fetchone()
            return None if row is None else HoldoutSpend(**row)

    # Internals

    def _migrate(self) -> None:
        (version,) = self._db.execute("PRAGMA user_version").fetchone()
        if version > len(MIGRATIONS):
            raise LedgerError(
                f"the Ledger is at schema version {version}; this code knows {len(MIGRATIONS)}"
            )
        for number, script in enumerate(MIGRATIONS[version:], start=version + 1):
            try:
                self._db.executescript(
                    f"BEGIN;\n{script}\nPRAGMA user_version = {number};\nCOMMIT;"
                )
            except BaseException:
                if self._db.in_transaction:
                    self._db.execute("ROLLBACK")
                raise

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        """One write transaction. IMMEDIATE takes the write lock up front, so a check (the
        budget, the anti-reset rule) and the insert it guards see the same Ledger."""
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield self._db
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            self._db.execute("COMMIT")

    @staticmethod
    def _family_row(db: sqlite3.Connection, family_id: int) -> sqlite3.Row:
        row = db.execute("SELECT * FROM families WHERE id = ?", (family_id,)).fetchone()
        if row is None:
            raise NotFound(f"no Idea Family {family_id}")
        return row

    @staticmethod
    def _hypothesis_row(db: sqlite3.Connection, hypothesis_id: int) -> sqlite3.Row:
        row = db.execute("SELECT * FROM hypotheses WHERE id = ?", (hypothesis_id,)).fetchone()
        if row is None:
            raise NotFound(f"no Hypothesis {hypothesis_id}")
        return row

    @staticmethod
    def _trial_count(db: sqlite3.Connection, family_id: int) -> int:
        return db.execute(
            "SELECT COUNT(*) FROM trials t JOIN hypotheses h ON h.id = t.hypothesis_id"
            " WHERE h.family_id = ?",
            (family_id,),
        ).fetchone()[0]

    @staticmethod
    def _budget(db: sqlite3.Connection, family_id: int) -> int:
        granted = db.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM budget_grants WHERE family_id = ?",
            (family_id,),
        ).fetchone()[0]
        return TRIAL_BUDGET + granted

    @staticmethod
    def _spent(db: sqlite3.Connection, family_id: int) -> bool:
        return (
            db.execute("SELECT 1 FROM holdout_spends WHERE family_id = ?", (family_id,)).fetchone()
            is not None
        )

    @staticmethod
    def _approval(db: sqlite3.Connection, request_id: int) -> sqlite3.Row | None:
        return db.execute(
            "SELECT a.token, h.family_id, h.symbol FROM holdout_approvals a"
            " JOIN holdout_requests r ON r.id = a.request_id"
            " JOIN hypotheses h ON h.id = r.hypothesis_id"
            " WHERE a.request_id = ?",
            (request_id,),
        ).fetchone()

    def _trial_record(self, row: sqlite3.Row) -> TrialRecord:
        combinations = self._db.execute(
            "SELECT label, blind_sharpe FROM trial_combinations WHERE trial_id = ?"
            " ORDER BY position",
            (row["id"],),
        ).fetchall()
        miss = row["closest_miss_json"]
        return TrialRecord(
            id=row["id"],
            hypothesis_id=row["hypothesis_id"],
            verdict=row["verdict"],
            closest_miss=None if miss is None else json.loads(miss),
            combinations=tuple((label, sharpe) for label, sharpe in combinations),
            cost=CostSnapshot.from_json(row["cost_json"]),
            bars=BarsFingerprint(
                row["bars_count"], row["bars_first"], row["bars_last"], row["bars_sha256"]
            ),
            result_json=row["result_json"],
            created_at=row["created_at"],
        )


def _require_finite(sharpes: Sequence[float]) -> None:
    # SQLite would store NaN as NULL, and V[SR] has no meaning with one in it.
    if not all(math.isfinite(sharpe) for sharpe in sharpes):
        raise ValueError("every blind-year Sharpe must be a finite number")


def _well_formed(grant: object) -> bool:
    return (
        isinstance(grant, HoldoutGrant)
        and isinstance(grant.request_id, int)
        and isinstance(grant.token, str)
        and -(2**63) <= grant.request_id < 2**63  # what SQLite can bind
    )


def _same_token(stored: str, given: str) -> bool:
    return secrets.compare_digest(stored.encode(), given.encode())
