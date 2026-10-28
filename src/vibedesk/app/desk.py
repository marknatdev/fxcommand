"""The desk behind the app: run a Spec through the Gauntlet on the cached real data, list past
Trials, and update the data from the MT5 terminal (read-only)."""

from __future__ import annotations

import json
import threading
import traceback
from datetime import UTC, datetime
from pathlib import Path

from vibedesk.app.research import normalize
from vibedesk.blocks.spec import validate
from vibedesk.gauntlet.trial import (
    _current_candidate,
    run_trial,
)
from vibedesk.gauntlet.trial import (
    request_holdout as gauntlet_request_holdout,
)
from vibedesk.ledger import Ledger
from vibedesk.ledger.store import HoldoutGrant
from vibedesk.market.bars import Timeframe
from vibedesk.market.cache import CachedBarSource, CacheError, first_cached, read_meta
from vibedesk.market.costs import CostError, current_cost, measure_cost, record_cost
from vibedesk.market.data import MarketData
from vibedesk.market.universe import SYMBOLS

FRIENDLY = {"CANDIDATE": "Looks real", "REJECT": "No edge"}
MAX_POINTS = 800  # equity-curve points sent to the page


class Desk:
    def __init__(self, root: Path, db: Path | None = None) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.ledger = Ledger(db or self.root / "desk.db")
        self.market = MarketData(CachedBarSource(self.root), self.ledger)
        self._trial_lock = threading.Lock()
        self._family_locks: dict[int, threading.Lock] = {}
        self._locks_lock = threading.Lock()
        self.update = DataUpdate(self.root)

    def family_lock(self, family_id: int) -> threading.Lock:
        """Acquire or create a per-family lock to serialize Trial runs and freeze N (CCC-55)."""
        with self._locks_lock:
            if family_id not in self._family_locks:
                self._family_locks[family_id] = threading.Lock()
            return self._family_locks[family_id]

    def run(self, idea: str, spec: dict) -> dict:
        """Run one Trial of `spec` and return its summary for the page."""
        frozen = validate(normalize(spec))
        cost = current_cost(self.root, frozen.symbol)
        with self._trial_lock:
            family = self.ledger.family_for(frozen.entry.block, frozen.symbol)
            if family is None:
                family = self.ledger.create_family(idea or frozen.name)
            hypothesis = self.ledger.register_hypothesis(family, frozen)
            while self.ledger.trial_count(family) >= self.ledger.budget(family):
                self.ledger.extend_budget(family)
        with self.family_lock(family):
            result = run_trial(
                self.ledger,
                self.market,
                hypothesis,
                cost,
                use_m1=self._has(frozen.symbol, Timeframe.M1),
            )
        return summarize(result.trial_id, result.record, idea, result.record["spec"])

    def run_hypothesis(self, hypothesis_id: int, idea: str = "") -> dict:
        """Run Gauntlet for a registered hypothesis under its family lock (CCC-50, CCC-55)."""
        hyp = self.ledger.hypothesis(hypothesis_id)
        cost = current_cost(self.root, hyp.symbol)
        family = self.ledger.family(hyp.family_id)
        idea_text = idea or family.idea
        with self.family_lock(hyp.family_id):
            result = run_trial(
                self.ledger,
                self.market,
                hypothesis_id,
                cost,
                use_m1=self._has(hyp.symbol, Timeframe.M1),
            )
        spec = json.loads(hyp.spec_json)
        summary = summarize(result.trial_id, result.record, idea_text, spec)
        verdict = result.record["verdict"]
        record_dict = self.ledger.trial(result.trial_id)
        return {
            "trial_id": result.trial_id,
            "hypothesis_id": hypothesis_id,
            "family_id": hyp.family_id,
            "verdict": verdict,
            "label": FRIENDLY.get(verdict, verdict),
            "closest_miss": result.record.get("closest_miss"),
            "created_at": record_dict.created_at,
            "spec": spec,
            "summary": summary,
            **summary,
        }

    def trial(self, trial_id: int) -> dict:
        record = self.ledger.trial(trial_id)
        result = json.loads(record.result_json)
        idea = self.ledger.family(result["family_id"]).idea
        return summarize(trial_id, result, idea, result["spec"])

    def get_trial_detail(self, trial_id: int) -> dict:
        record = self.ledger.trial(trial_id)
        result = json.loads(record.result_json)
        hyp = self.ledger.hypothesis(record.hypothesis_id)
        family = self.ledger.family(hyp.family_id)
        spec = json.loads(hyp.spec_json)
        summary = summarize(trial_id, result, family.idea, spec)
        verdict = record.verdict
        return {
            "trial_id": trial_id,
            "hypothesis_id": hyp.id,
            "family_id": hyp.family_id,
            "verdict": verdict,
            "label": FRIENDLY.get(verdict, verdict),
            "closest_miss": record.closest_miss,
            "created_at": record.created_at,
            "spec": spec,
            "summary": summary,
            **summary,
        }

    def _holdout_bindings_path(self) -> Path:
        return self.root / "holdout_bindings.json"

    def _load_holdout_bindings(self) -> dict[str, int]:
        p = self._holdout_bindings_path()
        if p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                return {}
        return {}

    def _save_holdout_bindings(self, bindings: dict[str, int]) -> None:
        p = self._holdout_bindings_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(bindings, indent=2), encoding="utf-8")

    def bind_holdout_request(self, request_id: int, trial_id: int) -> None:
        """Bind a Holdout Request to the Trial ID it was made from (CCC-55)."""
        bindings = self._load_holdout_bindings()
        bindings[str(request_id)] = trial_id
        self._save_holdout_bindings(bindings)

    def holdout_request_trial(self, request_id: int) -> int | None:
        """Retrieve the Trial ID bound to a Holdout Request (CCC-55)."""
        bindings = self._load_holdout_bindings()
        return bindings.get(str(request_id))

    def request_holdout(self, hypothesis_id: int, now: datetime | None = None) -> int:
        """Request holdout for candidate hypothesis and bind to current candidate trial (CCC-55)."""
        trial = _current_candidate(self.ledger, hypothesis_id)
        req_id = gauntlet_request_holdout(self.ledger, hypothesis_id, now=now)
        self.bind_holdout_request(req_id, trial.id)
        return req_id

    def approve_holdout(self, request_id: int) -> HoldoutGrant:
        """Approve a Holdout Request and issue a HoldoutGrant (owner-only action)."""
        return self.ledger.approve_holdout(request_id)

    def extend_budget(self, family_id: int, amount: int = 25) -> int:
        """Extend family trial budget (owner-only action)."""
        return self.ledger.extend_budget(family_id, amount=amount)

    def list_families(self) -> list[dict]:
        with self.ledger._lock:
            rows = self.ledger._db.execute(
                """
                SELECT f.id, f.idea, f.created_at,
                       (SELECT COUNT(*) FROM hypotheses h
                        WHERE h.family_id = f.id) as hypothesis_count,
                       (SELECT COUNT(*) FROM trials t
                        JOIN hypotheses h ON t.hypothesis_id = h.id
                        WHERE h.family_id = f.id) as trial_count
                FROM families f
                ORDER BY f.id DESC
                """
            ).fetchall()
            return [dict(r) for r in rows]

    def get_family(self, family_id: int) -> dict:
        fam = self.ledger.family(family_id)
        budget = self.ledger.budget(family_id)
        trial_count = self.ledger.trial_count(family_id)
        with self.ledger._lock:
            hyp_rows = self.ledger._db.execute(
                """
                SELECT id, family_id, entry_block, symbol, timeframe, spec_json, created_at
                FROM hypotheses
                WHERE family_id = ?
                ORDER BY id ASC
                """,
                (family_id,),
            ).fetchall()
            hypotheses = []
            for h in hyp_rows:
                t_ids = [
                    row["id"]
                    for row in self.ledger._db.execute(
                        "SELECT id FROM trials WHERE hypothesis_id = ? ORDER BY id ASC",
                        (h["id"],),
                    ).fetchall()
                ]
                hypotheses.append(
                    {
                        "id": h["id"],
                        "family_id": h["family_id"],
                        "entry_block": h["entry_block"],
                        "symbol": h["symbol"],
                        "timeframe": h["timeframe"],
                        "spec": json.loads(h["spec_json"]),
                        "created_at": h["created_at"],
                        "trial_ids": t_ids,
                    }
                )
        return {
            "id": fam.id,
            "idea": fam.idea,
            "created_at": fam.created_at,
            "budget": budget,
            "trial_count": trial_count,
            "hypotheses": hypotheses,
        }

    def get_hypothesis(self, hypothesis_id: int) -> dict:
        hyp = self.ledger.hypothesis(hypothesis_id)
        with self.ledger._lock:
            t_ids = [
                row["id"]
                for row in self.ledger._db.execute(
                    "SELECT id FROM trials WHERE hypothesis_id = ? ORDER BY id ASC",
                    (hypothesis_id,),
                ).fetchall()
            ]
        return {
            "id": hyp.id,
            "family_id": hyp.family_id,
            "entry_block": hyp.entry_block,
            "symbol": hyp.symbol,
            "timeframe": hyp.timeframe,
            "spec": json.loads(hyp.spec_json),
            "created_at": hyp.created_at,
            "trial_ids": t_ids,
        }

    def ledger_stats(self) -> dict:
        with self.ledger._lock:
            desk_trials = self.ledger._db.execute(
                "SELECT COUNT(*) as count FROM trials"
            ).fetchone()["count"]
            deflation_combos = self.ledger._db.execute(
                "SELECT COUNT(*) as count FROM trial_combinations"
            ).fetchone()["count"]
            families = self.list_families()
            fam_stats = []
            for f in families:
                fid = f["id"]
                budget = self.ledger.budget(fid)
                n_eval, _ = self.ledger.deflation_inputs(fid)
                fam_stats.append(
                    {
                        "family_id": fid,
                        "idea": f["idea"],
                        "trials": f["trial_count"],
                        "budget": budget,
                        "combinations_evaluated": n_eval,
                    }
                )
        return {
            "desk_trial_count": desk_trials,
            "current_deflation_combinations": deflation_combos,
            "families": fam_stats,
        }

    def history(self, limit: int = 50) -> list[dict]:
        rows = self.ledger.recent_trials(limit)
        return [
            {
                "id": row["id"],
                "verdict": row["verdict"],
                "label": FRIENDLY.get(row["verdict"], row["verdict"]),
                "when": row["created_at"],
                "symbol": row["symbol"],
                "timeframe": row["timeframe"],
                "name": json.loads(row["spec_json"])["name"],
                "idea": row["idea"],
            }
            for row in rows
        ]

    def data_status(self) -> list[dict]:
        status = []
        for symbol in SYMBOLS:
            row = {"symbol": symbol, "imported": False}
            try:
                meta = read_meta(self.root, symbol)
                mask = meta.mask
                row.update(
                    imported=mask is not None,
                    first_usable_month=mask and f"{mask.first_usable_month:%Y-%m}",
                    judged_through=mask and mask.judged_through.isoformat(),
                    last_import=meta.last_import and meta.last_import.isoformat(),
                )
            except CacheError:
                pass
            try:
                cost = current_cost(self.root, symbol)
                row.update(spread_floor=cost.spread_floor_points, cost_as_of=cost.as_of)
            except CostError:
                row.update(spread_floor=None, cost_as_of=None)
            status.append(row)
        return status

    def _has(self, symbol: str, timeframe: Timeframe) -> bool:
        return first_cached(self.root, symbol, timeframe) is not None


class DataUpdate:
    """Import or refresh every symbol from the MT5 terminal, then measure spreads and swaps.
    Read-only: the terminal is reached only through `real_api()`."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.running = False
        self.log: list[str] = []
        self._lock = threading.Lock()

    def start(self, symbols=SYMBOLS) -> bool:
        with self._lock:
            if self.running:
                return False
            self.running, self.log = True, []
        threading.Thread(target=self._run, args=(tuple(symbols),), daemon=True).start()
        return True

    def state(self) -> dict:
        return {"running": self.running, "log": self.log[-30:]}

    def _say(self, line: str) -> None:
        self.log.append(f"{datetime.now(UTC):%H:%M:%S}  {line}")

    def _run(self, symbols: tuple[str, ...]) -> None:
        from vibedesk.market.importer import import_history, refresh
        from vibedesk.market.mt5_api import real_api

        try:
            api = real_api()
            for symbol in symbols:
                self._say(f"{symbol}: reading history ...")
                try:
                    known = read_meta(self.root, symbol).last_import is not None
                except CacheError:
                    known = False
                run = refresh if known else import_history
                report = run(api, self.root, symbols=(symbol,))
                [one] = report.symbols
                self._say(f"{symbol}: {report.bars:,} bars, judged through {one.judged_through}")
            self._say("measuring spreads and swaps ...")
            if not api.initialize():
                raise RuntimeError(f"MT5 initialize failed: {api.last_error()}")
            try:
                now = datetime.now(UTC)
                for symbol in symbols:
                    api.symbol_select(symbol, True)
                    try:
                        cost = measure_cost(api, symbol, now)
                        record_cost(self.root, cost)
                        self._say(f"{symbol}: spread floor {cost.spread_floor_points:g} points")
                    except CostError as error:
                        self._say(f"{symbol}: {error}")
            finally:
                api.shutdown()
            self._say("done")
        except Exception as error:  # shown on the page; the server keeps running
            self._say(f"FAILED: {error}")
            traceback.print_exc()
        finally:
            self.running = False


def summarize(trial_id: int, record: dict, idea: str, spec: dict) -> dict:
    """What the page shows for one Trial: the Verdict, the checks, the numbers, the curves."""
    wf = record["walk_forward"]
    metrics = wf["metrics"]
    trades = wf["trade_list"]
    order = sorted(range(len(trades["exit_time"])), key=lambda i: trades["exit_time"][i])
    equity, total = [], 0.0
    for i in order:
        total += trades["r"][i]
        equity.append([trades["exit_time"][i], round(total, 3)])
    step = max(1, len(equity) // MAX_POINTS)
    curve = equity[::step] + ([equity[-1]] if equity and (len(equity) - 1) % step else [])
    return {
        "id": trial_id,
        "idea": idea,
        "spec": spec,
        "verdict": record["verdict"],
        "label": FRIENDLY.get(record["verdict"], record["verdict"]),
        "closest_miss": record["closest_miss"],
        "checks": record["checks"],
        "metrics": metrics,
        "stress": wf["stress_metrics"],
        "years": wf["years"],
        "year_net_r": wf["fold_net_r"],
        "equity": curve,
        "baseline": {k: record["baseline"][k] for k in ("p95", "percentile", "strategy_net_r")},
        "cost": record["cost"],
        "combinations": len(wf["combinations"]),
    }
