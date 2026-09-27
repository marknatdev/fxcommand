"""``fxcommand-research``: the Strategy Review's research tool (spec D44, D47). Read-only on the market.

It talks to the running app over HTTP (``FXC_URL``, default http://127.0.0.1:8000) and never touches
the Broker: history comes from the research snapshot, which ends where the Arena's sealed holdout
starts. Every backtest it runs is appended to the trial ledger, whatever its result — there is no
way to try a hypothesis quietly. A finalist is scored on the holdout once, with ``holdout --confirm``.

    fxcommand-research snapshot GOLD H4
    fxcommand-research backtest GOLD H4 --strategy trend_breakout --param entry=40 --hypothesis "shorter entry channel"
    fxcommand-research backtest GOLD M5 --script hypotheses/orb.py --hypothesis "NY opening-range breakout"
    fxcommand-research ledger GOLD H4
    fxcommand-research holdout GOLD H4 --strategy trend_breakout --param entry=40 --hypothesis "shorter entry channel" --confirm

A ``--script`` hypothesis is new strategy code: a Python file defining ``STRATEGY`` (a
``strategies.base.Strategy`` with a new key) and, for multi-timeframe inputs, ``CONTEXT`` (a list of
timeframes such as ["M15", "M1"]) and ``prepare(bars, context) -> bars``, which adds columns the
strategy reads. Align other timeframes only through ``strategies.mtf.align_closed`` (never a bar
still forming). Script trials are recorded by hypothesis text; they cannot become Challengers until
the code is merged.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import time
from pathlib import Path

import httpx
import pandas as pd

from .broker.types import Timeframe
from .learning.candidate import Candidate
from .learning.costs import CostModel
from .learning.evidence import SPREAD_MULTIPLIER
from .learning.paper import EntryGate, ExitRules
from .learning.research import evaluate_hypothesis
from .risk.window import WeekendClose
from .strategies import STRATEGIES, get_strategy

BASE = os.environ.get("FXC_URL", "http://127.0.0.1:8000").rstrip("/")
CACHE = Path(__file__).resolve().parents[1] / "data" / "research"  # backend/data: gitignored, local only
CACHE_HOURS = 12


class ResearchError(RuntimeError):
    pass


def _http(method: str, path: str, **kw):
    """One API call; errors become ResearchError with the server's message."""
    try:
        r = httpx.request(method, f"{BASE}/api{path}", timeout=600, **kw)
    except httpx.HTTPError as e:
        raise ResearchError(f"FXCommand is not reachable at {BASE}: {e}") from None
    if r.status_code >= 400:
        try:
            body = r.json()
            msg = body.get("message") or body.get("detail") or body
        except ValueError:
            msg = r.text
        raise ResearchError(f"{method} {path}: {r.status_code} {msg}")
    return r.json() if r.content else None


# ------------------------------------------------------------------ snapshot
def snapshot(symbol: str, timeframe: str, fresh: bool = False, arena_timeframe: str | None = None) -> dict:
    """The Arena's research snapshot (bars before its sealed holdout), cached locally for a few hours.
    ``arena_timeframe``: context bars for another Arena, cut at that Arena's holdout."""
    tf = Timeframe(timeframe).value
    arena_tf = Timeframe(arena_timeframe or tf).value
    path = CACHE / f"{symbol}_{tf}_for_{arena_tf}.json"
    params = {"symbol": symbol, "timeframe": tf, **({"arena_timeframe": arena_tf} if arena_tf != tf else {})}
    if not fresh and path.exists() and time.time() - path.stat().st_mtime < CACHE_HOURS * 3600:
        snap = json.loads(path.read_text())
    else:
        snap = _http("GET", "/research/snapshot", params=params)
        CACHE.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(snap))
    # always current, even from the cache: the ledger count and the Arena's Champion
    head = _http("GET", "/research/snapshot", params={**params, "tail": 0})
    if head["holdout_from"] != snap["holdout_from"]:  # the holdout rolled forward: the cache is stale
        return snapshot(symbol, timeframe, fresh=True, arena_timeframe=arena_timeframe)
    snap.update(arena_trials=head["arena_trials"], arena=head["arena"], costs=head["costs"] or snap["costs"])
    return snap


def bars_of(snap: dict) -> pd.DataFrame:
    return pd.DataFrame(snap["bars"])


# ---------------------------------------------------------------- hypotheses
def parse_params(items: list[str] | None) -> dict:
    out: dict = {}
    for item in items or []:
        k, _, v = item.partition("=")
        if not _:
            raise ResearchError(f"--param {item!r}: expected name=value")
        v = v.strip()
        out[k.strip()] = v.lower() == "true" if v.lower() in ("true", "false") else float(v)
    return out


def load_script(path: str):
    """Import a hypothesis script and register its Strategy for this process only."""
    spec = importlib.util.spec_from_file_location("fxcommand_hypothesis", path)
    if spec is None or spec.loader is None:
        raise ResearchError(f"cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    # a hypothesis is research code: it must never reach the terminal (not even by accident)
    sys.modules["MetaTrader5"] = None  # type: ignore[assignment]  # any "import MetaTrader5" now fails
    try:
        spec.loader.exec_module(mod)
    except ImportError as e:
        raise ResearchError(f"{path}: {e} (hypothesis scripts are research code: no terminal access)") from None
    strat = getattr(mod, "STRATEGY", None)
    if strat is None:
        raise ResearchError(f"{path} defines no STRATEGY")
    if strat.key in STRATEGIES and STRATEGIES[strat.key] is not strat:
        raise ResearchError(f"{path}: strategy key {strat.key!r} already exists; give new code a new key")
    STRATEGIES[strat.key] = strat
    return mod


def context_until(bars: pd.DataFrame, timeframe: str, cutoff: int) -> pd.DataFrame:
    """Context bars end where the Arena's holdout starts, whatever the context timeframe's own holdout
    (M1 context for an H4 Arena must not reach into the H4 holdout)."""
    return bars[bars["time"] + Timeframe(timeframe).seconds <= cutoff].reset_index(drop=True)


def run_backtest(args) -> dict:
    tf = Timeframe(args.timeframe)
    snap = snapshot(args.symbol, tf.value, fresh=args.fresh)
    bars = bars_of(snap)
    if len(bars) < 200:
        raise ResearchError(f"the research snapshot has only {len(bars)} bars (holdout from {snap['holdout_from']})")
    module = load_script(args.script) if args.script else None
    if module is not None:
        context = {}
        for ctx_tf in getattr(module, "CONTEXT", []) or []:
            ctx = bars_of(snapshot(args.symbol, ctx_tf, fresh=args.fresh, arena_timeframe=tf.value))
            context[Timeframe(ctx_tf).value] = context_until(ctx, ctx_tf, snap["holdout_from"])
        if hasattr(module, "prepare"):
            bars = module.prepare(bars, context)
        strategy = module.STRATEGY.key
    else:
        if not args.strategy:
            raise ResearchError("give --strategy (an existing Strategy) or --script (new code)")
        get_strategy(args.strategy)
        strategy = args.strategy
    candidate = Candidate.of(strategy, parse_params(args.param))
    arena = snap.get("arena") or {}
    champion = Candidate.of(arena["champion"]["strategy"], arena["champion"]["params"]) if arena.get("champion") else None
    if champion is not None and champion.strategy not in STRATEGIES:
        champion = None
    rules = ExitRules(**arena["rules"]) if arena.get("rules") else ExitRules()
    window = arena.get("window") or None
    weekend_close = args.weekend_close if args.weekend_close is not None else arena.get("weekend_close") or ""
    if not snap.get("costs"):
        raise ResearchError("no typical spread is known yet (market shut since the app started): try when it is open")
    base = CostModel(**snap["costs"])
    costs = CostModel(**{**base.to_dict(), "spread": base.spread * args.spread_x})
    trials = int(snap["arena_trials"]) + 1  # this run counts
    result = evaluate_hypothesis(
        bars, candidate, champion, costs, rules, EntryGate(window, tf.seconds),
        WeekendClose(weekend_close, tf.seconds) if weekend_close else None, trials,
    )
    result.update(
        hypothesis=args.hypothesis, candidate_label=candidate.label(), champion_label=champion.label() if champion else None,
        spread_x=args.spread_x, holdout_from=snap["holdout_from"], new_code=module is not None,
    )
    trial = _http("POST", "/research/trials", json={
        "symbol": args.symbol, "timeframe": tf.value,
        "hypothesis": args.hypothesis + (f" [script {Path(args.script).name}: {strategy}]" if module is not None else ""),
        "strategy": None if module is not None else strategy,
        "params": candidate.param_dict,
        "data_from": result["first_ts"], "data_to": result["last_ts"],
        "result": _ledger_result(result), "source": "cli",
    })
    result["trial_id"], result["arena_trials"] = trial["id"], trial["arena_trials"]
    return result


def _ledger_result(r: dict) -> dict:
    c = r["candidate"]
    return {
        "finalist": r["finalist"], "mean_r": c["mean_r"], "sqn": c["sqn"], "trades": c["trades"], "win_rate": c["win_rate"],
        "periods": [(p["label"], p["n"], p["mean"]) for p in c["periods"]],
        "failed": [k["name"] for k in r["checks"] if not k["ok"]], "penalty": r["penalty"], "spread_x": r["spread_x"],
        "champion_mean_r": r["champion"]["mean_r"] if r["champion"] else None,
    }


# ------------------------------------------------------------------- output
def _print_backtest(r: dict) -> None:
    c = r["candidate"]
    print(f"{r['candidate_label']} — {r['hypothesis']}")
    print(f"  {c['trades']} trades ({c['trades_per_year']}/yr), mean {c['mean_r']:+.3f}R, SQN {c['sqn']:.2f}, win {c['win_rate']:.0%}, max DD {c['max_dd_r']:.1f}R")
    for p in c["periods"]:
        print(f"    {p['label']}: {p['mean']:+.3f}R over {p['n']}")
    if r["champion"]:
        ch = r["champion"]
        print(f"  Champion {r['champion_label']}: mean {ch['mean_r']:+.3f}R, SQN {ch['sqn']:.2f}")
    for k in r["checks"]:
        print(f"  [{'x' if k['ok'] else ' '}] {k['name']}: {k['detail']}")
    print(f"  {'FINALIST' if r['finalist'] else 'rejected'} · ledger trial #{r['trial_id']} ({r['arena_trials']} on this Arena)")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="fxcommand-research", description=__doc__.split("\n\n")[0])
    ap.add_argument("--json", action="store_true", help="print JSON")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("snapshot", help="fetch an Arena's research snapshot (bars before the sealed holdout)")
    s.add_argument("symbol")
    s.add_argument("timeframe")
    s.add_argument("--fresh", action="store_true")

    b = sub.add_parser("backtest", help="test a hypothesis on the snapshot; always recorded in the ledger")
    b.add_argument("symbol")
    b.add_argument("timeframe")
    b.add_argument("--strategy")
    b.add_argument("--script", help="new strategy code (see the module help)")
    b.add_argument("--param", action="append", metavar="NAME=VALUE")
    b.add_argument("--hypothesis", required=True)
    b.add_argument("--spread-x", type=float, default=SPREAD_MULTIPLIER, help="spread multiple (default: as the Evidence Run)")
    b.add_argument("--weekend-close", help="HH:MM to model Weekend Close ('' for off); default: the Arena's Session")
    b.add_argument("--fresh", action="store_true")

    ld = sub.add_parser("ledger", help="trials and holdout uses per Arena")
    ld.add_argument("symbol", nargs="?")
    ld.add_argument("timeframe", nargs="?")

    h = sub.add_parser("holdout", help="score a finalist ONCE on the sealed holdout (final)")
    h.add_argument("symbol")
    h.add_argument("timeframe")
    h.add_argument("--strategy", required=True)
    h.add_argument("--param", action="append", metavar="NAME=VALUE")
    h.add_argument("--hypothesis", required=True)
    h.add_argument("--confirm", action="store_true", help="required: a holdout use is final and recorded")

    args = ap.parse_args(argv)
    try:
        if args.cmd == "snapshot":
            snap = snapshot(args.symbol, args.timeframe, fresh=args.fresh)
            out = {k: snap[k] for k in ("symbol", "timeframe", "holdout_from", "bars_total", "first_ts", "arena_trials", "arena", "costs", "note")}
            print(json.dumps(out, indent=2))
        elif args.cmd == "backtest":
            r = run_backtest(args)
            print(json.dumps(r, indent=2) if args.json else "", end="\n" if args.json else "")
            if not args.json:
                _print_backtest(r)
        elif args.cmd == "ledger":
            params = {k: v for k, v in (("symbol", args.symbol), ("timeframe", args.timeframe)) if v}
            print(json.dumps(_http("GET", "/research/trials", params={**params, "limit": 50}), indent=2))
        elif args.cmd == "holdout":
            if not args.confirm:
                raise ResearchError("a holdout evaluation is final and happens once per Candidate: add --confirm")
            print(json.dumps(_http("POST", "/research/holdout", json={
                "symbol": args.symbol, "timeframe": args.timeframe, "strategy": args.strategy,
                "params": parse_params(args.param), "hypothesis": args.hypothesis,
            }), indent=2))
    except (ResearchError, KeyError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
