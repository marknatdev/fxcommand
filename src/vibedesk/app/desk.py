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
from vibedesk.gauntlet.trial import run_trial
from vibedesk.ledger import Ledger
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
        self.update = DataUpdate(self.root)

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
            result = run_trial(self.ledger, self.market, hypothesis, cost,
                               use_m1=self._has(frozen.symbol, Timeframe.M1))  # fmt: skip
        return summarize(result.trial_id, result.record, idea, result.record["spec"])

    def trial(self, trial_id: int) -> dict:
        record = self.ledger.trial(trial_id)
        result = json.loads(record.result_json)
        idea = self.ledger.family(result["family_id"]).idea
        return summarize(trial_id, result, idea, result["spec"])

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
