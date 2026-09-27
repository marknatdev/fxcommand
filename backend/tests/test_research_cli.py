"""The research CLI (spec D44, D47): every backtest is recorded in the ledger, research never sees the
sealed holdout, the finalist bar (every period, selection penalty, neighbours), new-code hypotheses
with multi-timeframe context, and the holdout only with --confirm."""

import json
import sys
import textwrap

import pytest
from fastapi.testclient import TestClient

from fxcommand import research_cli as cli
from fxcommand.app import create_app
from fxcommand.broker import Timeframe
from fxcommand.broker.sim import SimBroker
from fxcommand.config import Config
from fxcommand.learning.candidate import Candidate
from fxcommand.learning.costs import CostModel
from fxcommand.learning.paper import EntryGate, ExitRules
from fxcommand.learning.research import MIN_RESEARCH_TRADES, evaluate_hypothesis
from fxcommand.strategies import STRATEGIES

from .conftest import MON_08

H4 = 4 * 3600


@pytest.fixture
def client(tmp_path, monkeypatch):
    cfg = Config(broker="sim", db_url=f"sqlite:///{tmp_path / 'rc.db'}", sim_speed=0, sim_start=MON_08, run_engine=False, sim_deep_days=600)
    with TestClient(create_app(cfg)) as c:
        c.post("/api/account/reconnect")

        def via_client(method, path, **kw):
            r = c.request(method, f"/api{path}", **kw)
            if r.status_code >= 400:
                raise cli.ResearchError(f"{method} {path}: {r.status_code} {r.json().get('message')}")
            return r.json() if r.content else None

        monkeypatch.setattr(cli, "_http", via_client)
        monkeypatch.setitem(sys.modules, "MetaTrader5", sys.modules.get("MetaTrader5"))  # load_script blocks it; restore after
        monkeypatch.setattr(cli, "CACHE", tmp_path / "research")
        yield c


def run(capsys, *argv):
    code = cli.main(["--json", *argv])
    out = capsys.readouterr()
    return code, (json.loads(out.out) if code == 0 and out.out.strip() else None), out.err


def test_the_finalist_bar():
    sim = SimBroker(seed=5, start=MON_08, history_days=10, deep_history_days=600)
    bars = sim.closed_bars("GOLD", Timeframe.H4, 10**6)
    costs = CostModel.from_symbol(sim.symbol_info("GOLD"), 0.6, ref_price=float(bars["close"].iloc[-1]))
    trend = Candidate.of("trend_breakout", None)
    args = (costs, ExitRules(), EntryGate(None, H4), None)
    r = evaluate_hypothesis(bars, trend, trend, *args, trials=1)
    names = {c["name"]: c for c in r["checks"]}
    assert set(names) == {"trades", "every_period", "selection_penalty", "neighbours"}
    assert not names["every_period"]["ok"] and not r["finalist"]  # never "beats" itself
    assert len(r["candidate"]["periods"]) == 3 and len(r["neighbours"]) == 4
    assert r["candidate"]["trades"] == r["champion"]["trades"]
    more = evaluate_hypothesis(bars, trend, None, *args, trials=500)
    assert more["penalty"] > r["penalty"]  # the ledger count raises the bar
    none = evaluate_hypothesis(bars, Candidate.of("trend_breakout", {"entry": 400}), None, *args, trials=1)
    assert none["candidate"]["trades"] < MIN_RESEARCH_TRADES and not none["finalist"]


def test_every_backtest_is_recorded_and_never_reaches_the_holdout(client, capsys):
    code, snap, _ = run(capsys, "snapshot", "GOLD", "H4")
    assert code == 0 and snap["bars_total"] > 500 and snap["arena"] is None  # a research-only Arena: judged against zero
    code, r, err = run(capsys, "backtest", "GOLD", "H4", "--strategy", "trend_breakout", "--param", "entry=40", "--hypothesis", "shorter entry")
    assert code == 0, err
    assert r["trial_id"] and r["arena_trials"] == 1 and r["trials"] == 1 and r["spread_x"] == 2.0
    assert r["last_ts"] + H4 <= r["holdout_from"]
    code, r2, _ = run(capsys, "backtest", "GOLD", "H4", "--strategy", "trend_breakout", "--param", "entry=40", "--hypothesis", "the same again")
    assert r2["arena_trials"] == 2 and r2["penalty"] > r["penalty"]  # repeating a test still counts
    ledger = client.get("/api/research/trials").json()
    assert ledger["arenas"][0]["trials"] == 2 and ledger["trials"][0]["result"]["failed"] is not None


def test_a_session_arena_is_judged_against_its_champion_with_its_settings(client, capsys):
    client.post("/api/sessions", json={"name": "T", "daily_loss_pct": 50, "weekend_close": True,
                                       "assignments": [{"symbol": "GOLD", "timeframe": "H4", "strategy": "trend_breakout"}]})
    code, r, err = run(capsys, "backtest", "GOLD", "H4", "--strategy", "trend_breakout", "--param", "entry=40", "--hypothesis", "vs champion")
    assert code == 0, err
    assert r["champion"] is not None and r["champion_label"].startswith("Trend Breakout")
    code, snap, _ = run(capsys, "snapshot", "GOLD", "H4")
    assert snap["arena"]["weekend_close"] == "22:30" and snap["arena_trials"] == 1


def test_new_code_with_multi_timeframe_context(client, capsys, tmp_path):
    script = tmp_path / "d1_filter.py"
    script.write_text(textwrap.dedent('''
        import dataclasses
        from fxcommand.strategies.gold import TREND_BREAKOUT
        from fxcommand.strategies.mtf import align_closed

        CONTEXT = ["D1"]

        def prepare(bars, context):
            d1 = context["D1"]
            up = d1["close"] > d1["close"].rolling(20).mean()
            bars = bars.copy()
            bars["d1_up"] = align_closed(bars["time"], 4 * 3600, d1["time"], 86400, up.astype(float), fill=0.0)
            return bars

        def _compute(bars, p):
            f = TREND_BREAKOUT.compute(bars, p)
            f["long"] = f["long"] & (bars["d1_up"] > 0)
            return f

        STRATEGY = dataclasses.replace(TREND_BREAKOUT, key="trend_d1_filter", title="Trend Breakout + D1 filter", compute=_compute)
    '''))
    try:
        code, r, err = run(capsys, "backtest", "GOLD", "H4", "--script", str(script), "--hypothesis", "D1 trend filter")
        assert code == 0, err
        assert r["new_code"] and r["candidate_label"].startswith("Trend Breakout + D1 filter")
        [t] = client.get("/api/research/trials").json()["trials"]
        assert t["strategy"] == "" and "d1_filter.py" in t["hypothesis"]  # new code is recorded by its hypothesis
    finally:
        STRATEGIES.pop("trend_d1_filter", None)


def test_the_holdout_needs_confirm(client, capsys):
    base = ["holdout", "GOLD", "H4", "--strategy", "trend_breakout", "--param", "entry=40", "--hypothesis", "finalist"]
    assert cli.main(base) == 2
    assert "--confirm" in capsys.readouterr().err
    assert client.get("/api/research/trials").json()["arenas"] == []  # nothing used
    assert cli.main([*base, "--confirm"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["holdout_used"] and out["status"] in ("passed", "failed")
    assert cli.main([*base, "--confirm"]) == 2  # once only
    assert "final" in capsys.readouterr().err


def test_the_scheduled_review_cannot_reach_the_operator_tools():
    """D54: the weekly review runs with an allow-list. Every MCP tool is either allowed (read-only views
    and the two narrow writes) or explicitly denied (the operator's Session controls), and Bash only
    reaches the research CLI, pytest and the review-branch git steps."""
    import asyncio
    import re
    from pathlib import Path

    import fxcommand.mcp_server as mcp_server

    script = (Path(__file__).resolve().parents[2] / "scripts" / "run-gold-review.ps1").read_text()
    allowed_block = script.split("$Allowed = @(")[1].split(")\n$Denied")[0]
    denied_block = script.split("$Denied = @(")[1].split("\n)")[0]
    allowed = set(re.findall(r"mcp__fxcommand__(\w+)", allowed_block))
    denied = set(re.findall(r"mcp__fxcommand__(\w+)", denied_block))
    operator = {"start_session", "pause_session", "resume_session", "stop_session", "kill_switch", "run_learning"}
    tools = {t.name for t in asyncio.run(mcp_server.mcp.list_tools())}
    assert denied == operator and not (allowed & operator)
    assert allowed | denied == tools  # a new MCP tool must be placed on one side deliberately
    bash = re.findall(r'"Bash\(([^)]*)\)"', allowed_block)
    assert all(b.startswith(("uv run --directory backend fxcommand-research", "uv run --directory backend pytest", "git ", "gh pr create")) for b in bash)
    assert "--permission-mode\", \"dontAsk\"" in script and "--strict-mcp-config" in script


def test_a_hypothesis_script_cannot_import_the_terminal(client, capsys, tmp_path):
    script = tmp_path / "sneaky.py"
    script.write_text("import MetaTrader5" + chr(10))
    code, _, err = run(capsys, "backtest", "GOLD", "H4", "--script", str(script), "--hypothesis", "x")
    assert code == 2 and "MetaTrader5" in err and "no terminal access" in err
    assert client.get("/api/research/trials").json()["arenas"] == []  # refused before anything ran


def test_context_ends_at_the_arenas_holdout(client):
    from fxcommand.learning import research

    snap = client.get("/api/research/snapshot", params={"symbol": "GOLD", "timeframe": "M15", "arena_timeframe": "M5", "tail": 0}).json()
    assert snap["holdout_from"] == research.holdout_from(snap["server_time"], "M5")  # 3 months, not M15's 12
    h4 = client.get("/api/research/snapshot", params={"symbol": "GOLD", "timeframe": "M1", "arena_timeframe": "H4", "tail": 0}).json()
    assert h4["holdout_from"] == research.holdout_from(h4["server_time"], "H4")  # M1 context stops at the H4 holdout
    import pandas as pd

    bars = pd.DataFrame({"time": [0, 900, 1800, 2700]})
    assert list(cli.context_until(bars, "M15", 2700)["time"]) == [0, 900, 1800]  # a bar counts once it has closed


def test_research_is_judged_from_2010():
    """GOLD H4 history reaches 2001, but the research periods start in 2010: older bars only warm up."""
    import numpy as np
    import pandas as pd

    from fxcommand.learning.research import RESEARCH_START

    t0 = RESEARCH_START - 400 * 86400  # starts in 2008
    n = 3000
    t = t0 + np.arange(n) * 86400
    rng = np.random.default_rng(3)
    close = 1000 + np.cumsum(rng.normal(0, 5, n))
    bars = pd.DataFrame({"time": t, "open": close, "high": close + 3, "low": close - 3, "close": close, "volume": 1.0})
    cand = Candidate.of("ema_cross", {"fast": 3, "slow": 8, "atr_period": 5, "sl_atr": 2.0, "tp_atr": 4.0})
    r = evaluate_hypothesis(bars, cand, None, CostModel(spread=0.5), ExitRules(), EntryGate(None, 86400), None, trials=1)
    assert r["first_ts"] == RESEARCH_START
    assert r["candidate"]["periods"][0]["label"] == "2010–17" and r["candidate"]["periods"][0]["from_ts"] == RESEARCH_START
    assert r["candidate"]["trades"] == sum(p["n"] for p in r["candidate"]["periods"])  # nothing from 2008–09 counts
