"""Milestone 7: the Evidence, scorecard, Paper Account, Cost Check and Strategy Review endpoints, and
the MCP tools on top of them (spec: API and MCP; D29, D42, D44, D47)."""

import time

import pytest
from fastapi.testclient import TestClient

import fxcommand.mcp_server as mcp_server
from fxcommand.app import create_app
from fxcommand.config import Config
from fxcommand.learning import research

from .conftest import FAST, MON_08

H4 = 4 * 3600
TREND = {"symbol": "GOLD", "timeframe": "H4", "strategy": "trend_breakout"}
MCP_TOOLS = {
    # the original operator tools
    "get_overview", "list_sessions", "start_session", "pause_session", "resume_session", "stop_session", "kill_switch",
    "list_positions", "get_journal", "get_preflight", "get_learning_status", "run_learning",
    # Strategy Review: read-only views plus exactly two narrow writes
    "get_evidence", "get_scorecard", "get_research_snapshot", "get_trials", "submit_review", "submit_challenger",
}


@pytest.fixture
def client(tmp_path):
    cfg = Config(broker="sim", db_url=f"sqlite:///{tmp_path / 'rv.db'}", sim_speed=0, sim_start=MON_08, run_engine=False, sim_deep_days=600)
    with TestClient(create_app(cfg)) as c:
        c.post("/api/account/reconnect")
        yield c


def until(fn, timeout=120.0, every=0.25):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(every)
    raise AssertionError("condition not met in time")


def holdout(client, entry, status="passed"):
    """A ledger row as a holdout evaluation leaves it (the evaluation itself is tested separately)."""
    from fxcommand.learning.candidate import Candidate

    key = Candidate.of("trend_breakout", {"entry": entry}).key
    client.app.state.rt.learning.repo.add_trial(symbol="GOLD", timeframe="H4", hypothesis=f"entry {entry}", strategy="trend_breakout",
                                                params_hash=key, holdout_used=True, status=status, source="holdout", ts=MON_08)


def gold_trend_session(client, name="Trend", **kw):
    r = client.post("/api/sessions", json={"name": name, "daily_loss_pct": 50, "assignments": [{**TREND}], **kw})
    assert r.status_code == 201, r.text
    return r.json()


# ------------------------------------------------------------------- Cost Check
def test_the_cost_check_blocks_over_http_and_the_override_travels_with_the_session(client):
    body = {"name": "Scalp", "daily_loss_pct": 50, "assignments": [{"symbol": "GOLD", "timeframe": "M1", "strategy": "ema_cross", "params": FAST}]}
    s = client.post("/api/sessions", json=body).json()
    sid = s["id"]
    cc = client.get(f"/api/sessions/{sid}/cost-check").json()
    assert cc["threshold"] == 0.15 and cc["assignments"][0]["allowed"] is False
    r = client.post(f"/api/sessions/{sid}/start")
    assert r.status_code == 422 and r.json()["code"] == "cost_check"

    bad = client.put(f"/api/sessions/{sid}", json={**body, "cost_overrides": [{"symbol": "GOLD", "timeframe": "M5"}]})
    assert bad.status_code == 422 and bad.json()["code"] == "invalid_override"
    s = client.put(f"/api/sessions/{sid}", json={**body, "cost_overrides": [{"symbol": "GOLD", "timeframe": "M1", "reason": "demo only"}]}).json()
    assert s["cost_overrides"][0]["reason"] == "demo only"
    assert client.get(f"/api/sessions/{sid}/cost-check").json()["assignments"][0]["allowed"] is True
    assert client.post(f"/api/sessions/{sid}/start").status_code == 200
    client.post(f"/api/sessions/{sid}/stop", json={"close_positions": True})

    s = client.put(f"/api/sessions/{sid}", json=body).json()  # an editor that does not send the field keeps it
    assert len(s["cost_overrides"]) == 1
    s = client.put(f"/api/sessions/{sid}", json={**body, "cost_overrides": []}).json()  # the full set: none
    assert s["cost_overrides"] == []
    assert client.post(f"/api/sessions/{sid}/start").json()["code"] == "cost_check"
    msgs = [j["message"] for j in client.get("/api/journal", params={"session_id": sid, "limit": 200}).json()]
    assert any("Cost Check overridden for GOLD M1: demo only" in m for m in msgs)
    assert any("Cost Check override removed for GOLD M1" in m for m in msgs)


def test_removing_an_assignment_drops_its_override(client):
    both = [{"symbol": "GOLD", "timeframe": "M1", "strategy": "ema_cross", "params": FAST}, {**TREND}]
    body = {"name": "Two", "daily_loss_pct": 50, "assignments": both}
    s = client.post("/api/sessions", json={**body, "cost_overrides": [{"symbol": "GOLD", "timeframe": "M1"}]}).json()
    assert len(s["cost_overrides"]) == 1
    s = client.put(f"/api/sessions/{s['id']}", json={**body, "assignments": [{**TREND}]}).json()
    assert s["cost_overrides"] == []  # it is not silently inherited when GOLD M1 comes back


# --------------------------------------------------------------- Paper Account
def test_paper_account_endpoints_and_their_guards(client):
    a = client.get("/api/paper-account").json()
    assert a["start_balance"] == 5000 and a["epoch"] == 0 and a["equity"] == 5000
    a = client.put("/api/paper-account", json={"start_balance": 8000}).json()
    assert a["equity"] == 8000 and a["day_start_equity"] == 8000 and a["day_pnl"] == 0
    client.put("/api/settings", json={"cost_check_max_r": 10})
    s = client.post("/api/sessions", json={"name": "P", "daily_loss_pct": 50, "execution": "paper",
                                           "assignments": [{"symbol": "EURUSD", "timeframe": "M1", "strategy": "ema_cross", "params": FAST}]}).json()
    assert client.post(f"/api/sessions/{s['id']}/start").status_code == 200
    r = client.put("/api/paper-account", json={"start_balance": 9000})
    assert r.status_code == 409 and r.json()["code"] == "paper_active"
    assert client.post("/api/paper-account/reset", json={"confirm": "RESET PAPER"}).json()["code"] == "paper_active"
    client.post(f"/api/sessions/{s['id']}/stop", json={"close_positions": True})
    assert client.post("/api/paper-account/reset", json={"confirm": "reset"}).status_code == 422
    a = client.post("/api/paper-account/reset", json={"confirm": "RESET PAPER", "start_balance": 3000}).json()
    assert a["epoch"] == 1 and a["equity"] == 3000


# -------------------------------------------------------------------- Evidence
def test_evidence_over_http(client):
    s = gold_trend_session(client, weekend_close=True)
    queued = client.post("/api/evidence/run", json={**TREND, "session_id": s["id"]})
    assert queued.status_code == 202 and queued.json()["status"] == "queued"
    eid = queued.json()["id"]
    row = until(lambda: (e := client.get(f"/api/evidence/{eid}").json())["status"] in ("done", "incomplete", "failed") and e)
    assert row["status"] == "done" and row["trades"] > 0 and "rs" not in row
    assert [e["id"] for e in client.get("/api/evidence", params={"symbol": "GOLD"}).json()] == [eid]
    match = client.post("/api/evidence/match", json={**TREND, "session_id": s["id"]}).json()
    assert match["status"] == "match" and match["without_weekend_close"]["status"] == "mismatch"  # not run yet: only other settings
    assert client.post("/api/evidence/match", json={**TREND}).json()["status"] == "mismatch"  # Weekend Close off
    [badge] = client.get(f"/api/sessions/{s['id']}/evidence").json()
    assert badge["status"] == "match" and badge["evidence"]["id"] == eid
    cards = client.get("/api/scorecard").json()
    assert cards[0]["symbol"] == "GOLD" and cards[0]["verdict"] == "too few trades" and cards[0]["min_lot_risk_pct"] > 0
    assert client.post("/api/evidence/run", json={**TREND, "strategy": "nope"}).status_code == 422


# ------------------------------------------------------------ research ledger
def test_the_snapshot_never_contains_holdout_bars_and_trials_cannot_reach_it(client):
    snap = client.get("/api/research/snapshot", params={"symbol": "GOLD", "timeframe": "H4"}).json()
    cutoff = snap["holdout_from"]
    assert cutoff == research.holdout_from(snap["server_time"], "H4") == 1672531200  # 2023-01-01: 12 months before January 2024
    assert snap["bars_total"] > 500 and all(t + H4 <= cutoff for t in snap["bars"]["time"])
    assert snap["costs"]["spread"] > 0 and snap["arena_trials"] == 0
    tail = client.get("/api/research/snapshot", params={"symbol": "GOLD", "timeframe": "H4", "tail": 5}).json()
    assert len(tail["bars"]["time"]) == 5 and tail["bars"]["time"][-1] == snap["bars"]["time"][-1]
    assert research.holdout_from(snap["server_time"], "M5") == 1696118400  # scalping Arenas: 3 months (2023-10-01)

    trial = {"symbol": "GOLD", "timeframe": "H4", "hypothesis": "Turtle 40/20", "strategy": "trend_breakout", "params": {"entry": 40},
             "data_from": snap["bars"]["time"][0], "result": {"mean_r": 0.1}}
    ok = client.post("/api/research/trials", json={**trial, "data_to": cutoff - H4})  # the last bar closes at the cutoff
    assert ok.status_code == 201 and ok.json()["arena_trials"] == 1
    leak = client.post("/api/research/trials", json={**trial, "data_to": cutoff - H4 + 1})
    assert leak.status_code == 422 and leak.json()["code"] == "holdout"
    client.post("/api/research/trials", json={**trial, "strategy": None, "hypothesis": "new: H4 filter", "data_to": cutoff - H4})
    ledger = client.get("/api/research/trials", params={"symbol": "GOLD"}).json()
    assert ledger["arenas"] == [{"symbol": "GOLD", "timeframe": "H4", "trials": 2, "holdout_uses": 0, "holdout_from": cutoff}]
    assert len({t["params_hash"] for t in ledger["trials"]}) == 2


def test_a_finalist_is_scored_on_the_holdout_once(client):
    gold_trend_session(client)
    body = {**TREND, "params": {"entry": 40}, "hypothesis": "shorter entry channel"}
    first = client.post("/api/research/holdout", json=body)
    assert first.status_code == 200, first.text
    res = first.json()
    assert res["holdout_used"] and res["status"] in ("passed", "failed")
    assert res["result"]["champion"] is not None and res["result"]["rule"] == research.HOLDOUT_RULE
    again = client.post("/api/research/holdout", json=body)
    assert again.status_code == 409 and again.json()["code"] == "holdout_used" and res["status"] in again.json()["message"]
    same = client.post("/api/research/holdout", json={**body, "params": {"entry": 40.0}, "hypothesis": "reworded"})
    assert same.status_code == 409  # the same Candidate, however it is described
    arenas = client.get("/api/research/trials").json()["arenas"]
    assert arenas == [{"symbol": "GOLD", "timeframe": "H4", "trials": 1, "holdout_uses": 1, "holdout_from": 1672531200}]


# ------------------------------------------------------- reviewer submissions
def test_a_submitted_challenger_is_judged_and_never_touches_a_session(client):
    s = gold_trend_session(client)
    before = client.get(f"/api/sessions/{s['id']}").json()
    cross = client.post("/api/learning/challengers", json={**TREND, "strategy": "ema_cross"})
    assert cross.status_code == 422 and cross.json()["code"] == "cross_family"
    assert client.post("/api/learning/challengers", json={**TREND, "symbol": "EURUSD"}).status_code == 404
    assert client.post("/api/learning/challengers", json={**TREND, "params": {"entry": 40}}).json()["code"] == "holdout_required"
    holdout(client, 39, "failed")
    assert client.post("/api/learning/challengers", json={**TREND, "params": {"entry": 39}}).json()["code"] == "holdout_failed"
    for entry in (40, 41, 42, 43):
        holdout(client, entry)
    r = client.post("/api/learning/challengers", json={**TREND, "params": {"entry": 40}, "note": "shorter channel"})
    assert r.status_code == 202
    assert client.post("/api/learning/challengers", json={**TREND, "params": {"entry": 40}}).json()["code"] == "duplicate"
    run = until(lambda: (x := client.get(f"/api/learning/runs/{r.json()['run_id']}").json())["status"] not in ("queued", "running") and x)
    assert run["status"] == "done" and run["trigger"] == "reviewer", run
    arena = client.get("/api/learning/arenas/GOLD/H4").json()
    [ch] = arena["challengers"]
    assert ch["candidate"]["params"]["entry"] == 40 and "Strategy Review" in ch["note"]
    assert not ch["promotable"] and arena["pending"] is None
    after = client.get(f"/api/sessions/{s['id']}").json()
    assert after["status"] == before["status"] == "stopped" and after["assignments"][0]["params"] == before["assignments"][0]["params"]
    for entry in (41, 42):
        client.post("/api/learning/challengers", json={**TREND, "params": {"entry": entry}})
    full = client.post("/api/learning/challengers", json={**TREND, "params": {"entry": 43}})
    assert full.status_code == 409 and full.json()["code"] == "full"


def test_a_review_is_stored_and_sent_once(client):
    r = client.post("/api/reviews", json={"title": "Week 39", "summary": "2 hypotheses, no finalist", "report": "# Week 39\n...", "arenas": ["GOLD H4"]})
    assert r.status_code == 201
    rid = r.json()["id"]
    [listed] = client.get("/api/reviews").json()
    assert listed["id"] == rid and "report" not in listed
    assert client.get(f"/api/reviews/{rid}").json()["report"].startswith("# Week 39")
    sent = until(lambda: [o for o in client.get("/api/notify/outbox").json() if "Strategy Review" in o["text"]])
    time.sleep(0.5)
    assert len([o for o in client.get("/api/notify/outbox").json() if "Strategy Review" in o["text"]]) == len(sent) == 1


# ------------------------------------------------------------------------ MCP
def test_the_mcp_tool_set_is_pinned():
    import asyncio

    names = {t.name for t in asyncio.run(mcp_server.mcp.list_tools())}
    assert names == MCP_TOOLS
    forbidden = ("override", "reset", "evidence_run", "run_evidence", "live", "risk", "promote", "rollback", "holdout", "trial")
    assert not [n for n in names if any(f in n for f in forbidden) and n != "get_trials"]


def test_mcp_review_tools_go_through_the_api(client, monkeypatch):
    def via_client(method, url, json=None, params=None, timeout=None):
        return client.request(method, url.replace(mcp_server.BASE, ""), json=json, params=params)

    monkeypatch.setattr(mcp_server.httpx, "request", via_client)
    gold_trend_session(client)
    holdout(client, 40)
    full = client.get("/api/research/snapshot", params={"symbol": "GOLD", "timeframe": "H4"}).json()
    snap = mcp_server.get_research_snapshot("GOLD", "H4", tail=10_000)
    assert len(snap["bars"]["time"]) == 500 and snap["bars_total"] > 500  # capped for an agent
    assert snap["first_ts"] == full["bars"]["time"][0]  # where research data starts, not where the tail does
    assert [(a["trials"], a["holdout_uses"]) for a in mcp_server.get_trials()["arenas"]] == [(1, 1)]
    assert mcp_server.get_evidence() == []
    sub = mcp_server.submit_challenger("GOLD", "H4", "trend_breakout", {"entry": 40}, "mcp")
    assert sub["status"] == "queued"
    assert "error" in mcp_server.submit_challenger("GOLD", "H4", "ema_cross")
    rv = mcp_server.submit_review("Week 39", "nothing passed")
    assert set(rv) == {"id", "title", "ts", "ledger"}
    # the Cost Check holds through MCP too
    scalp = client.post("/api/sessions", json={"name": "Scalp", "daily_loss_pct": 50,
                                               "assignments": [{"symbol": "GOLD", "timeframe": "M1", "strategy": "ema_cross", "params": FAST}]}).json()
    refused = mcp_server.start_session(scalp["id"])
    assert refused["error"]["code"] == "cost_check"


async def test_reviewer_challengers_pay_the_ledger_penalty_and_never_less_than_the_optimizers(tmp_path):
    from fxcommand.learning.candidate import Candidate
    from fxcommand.learning.objective import deflation, r_stats
    from fxcommand.learning.optimizer import TOP_K

    from .test_learning_service import LH

    h = LH(tmp_path)
    await h.mgr.tick_once()
    await h.session("S")
    champion = Candidate.of("ema_cross", FAST)
    cand = Candidate.of("ema_cross", {**FAST, "fast": 3})
    h.L.repo.ensure_candidate(cand)
    champ_oos = r_stats([0.1, -1, 0.9] * 10)
    ch = h.L.repo.add_challenger(symbol="EURUSD", timeframe="M1", candidate_key=cand.key, oos=r_stats([0.6, -1, 1.2, 0.8] * 15).to_dict(),
                                 champion_oos=champ_oos.to_dict(), trials=1, robust=True, source="reviewer")

    def need():
        return next(c for c in h.L.challenger_report(ch, champion)["checks"] if c["name"] == "walk_forward")["threshold"]

    floor = round(max(champ_oos.sqn, 0) + deflation(TOP_K), 2)
    assert need() == floor  # a ledger of 1 trial never lowers the penalty below an Optimizer pick's
    for i in range(40):
        h.L.repo.add_trial(symbol="EURUSD", timeframe="M1", hypothesis=f"h{i}", params_hash=f"x{i}", ts=MON_08)
    assert need() == round(max(champ_oos.sqn, 0) + deflation(40), 2) > floor  # trials logged later still count
    await h.close()


async def test_two_concurrent_holdout_requests_score_once(tmp_path):
    import asyncio

    from fxcommand.broker.sim import SimBroker
    from fxcommand.engine import DomainError

    from .test_learning_service import LH

    h = LH(tmp_path, sim=SimBroker(seed=5, start=MON_08, history_days=10, deep_history_days=600))
    await h.mgr.tick_once()
    calls = [h.L.evaluate_holdout("GOLD", "H4", "trend_breakout", {"entry": 40}, f"try {i}") for i in range(2)]
    results = await asyncio.gather(*calls, return_exceptions=True)
    errors = [r for r in results if isinstance(r, DomainError)]
    assert len(errors) == 1 and errors[0].code == "holdout_used"
    assert [t.holdout_used for t in h.L.repo.trials("GOLD", "H4")] == [True]
    await h.close()


# ------------------------------------------------------------------ UI support
def test_the_cost_check_previews_unsaved_assignments(client):
    body = {"assignments": [{**TREND}, {"symbol": "GOLD", "timeframe": "M1", "strategy": "ema_cross", "params": FAST},
                            {"symbol": "NOPE", "timeframe": "H1", "strategy": "ema_cross"}]}
    r = client.post("/api/cost-check", json=body).json()
    trend, scalp, bad = r["assignments"]
    assert r["threshold"] == 0.15 and trend["allowed"] and not scalp["allowed"] and scalp["override"] is False
    assert bad["allowed"] is False and bad["error"]
    s = client.post("/api/sessions", json={"name": "S", "daily_loss_pct": 50, "assignments": body["assignments"][:2],
                                           "cost_overrides": [{"symbol": "GOLD", "timeframe": "M1"}]}).json()
    r = client.post("/api/cost-check", json={**body, "session_id": s["id"]}).json()
    assert r["assignments"][1]["override"] and r["assignments"][1]["allowed"]  # the saved Session's override counts


def test_statistics_never_mix_the_real_account_and_the_paper_account(client):
    from fxcommand.store import TradeRow

    store = client.app.state.rt.store
    for ticket, paper, profit, epoch in ((1, False, 10.0, 0), (-1, True, -4.0, 0), (-2, True, 100.0, 5)):
        store.add_trade(TradeRow(ticket=ticket, magic=1, symbol="GOLD", side="long", volume=0.01, strategy="trend_breakout", timeframe="H4",
                                 open_time=MON_08, open_price=2000, status="closed", close_time=MON_08 + 60, close_price=2001,
                                 profit=profit, paper=paper, paper_epoch=epoch))
    o = client.get("/api/overview").json()
    assert o["all_time"]["trades"] == 1 and o["all_time"]["net_profit"] == 10.0
    assert o["all_time_paper"]["trades"] == 1 and o["all_time_paper"]["net_profit"] == -4.0  # this epoch only
    assert o["paper_account"]["epoch"] == 0
    [trend] = [s for s in client.get("/api/strategies").json() if s["key"] == "trend_breakout"]
    assert trend["stats"]["net_profit"] == 10.0 and trend["stats_paper"]["net_profit"] == -4.0
    real = client.get("/api/trades", params={"account": "real"}).json()
    assert [t["ticket"] for t in real["trades"]] == [1] and real["stats"]["net_profit"] == 10.0
    paper = client.get("/api/trades", params={"account": "paper"}).json()
    assert paper["stats"]["trades"] == 2 and paper["stats_real"]["trades"] == 0
