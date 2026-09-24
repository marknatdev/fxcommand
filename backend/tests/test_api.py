import pytest
from fastapi.testclient import TestClient

from fxcommand.app import create_app
from fxcommand.config import Config

from .conftest import FAST, MON_08


@pytest.fixture
def client(tmp_path):
    cfg = Config(broker="sim", db_url=f"sqlite:///{tmp_path / 'api.db'}", sim_speed=0, sim_start=MON_08, run_engine=False)
    with TestClient(create_app(cfg)) as c:
        c.post("/api/account/reconnect")
        yield c


def make_session(client, name="API", symbols=("EURUSD", "GBPUSD", "USDJPY")):
    r = client.post(
        "/api/sessions",
        json={"name": name, "daily_loss_pct": 50, "assignments": [{"symbol": s, "timeframe": "M1", "strategy": "ema_cross", "params": FAST} for s in symbols]},
    )
    assert r.status_code == 201, r.text
    return r.json()


def test_status_and_account(client):
    s = client.get("/api/status").json()
    assert s["mode"] == "sim" and s["connected"] and s["account"]["is_demo"]
    a = client.get("/api/account").json()
    assert a["account"]["login"] == 99_000_001 and a["live_enabled"] is False


def test_every_page_endpoint_responds(client):
    make_session(client)
    for path in [
        "/api/overview",
        "/api/sessions",
        "/api/sessions/1",
        "/api/symbols",
        "/api/symbols/EURUSD/bars?timeframe=M5&count=50",
        "/api/strategies",
        "/api/risk",
        "/api/positions",
        "/api/trades",
        "/api/journal",
        "/api/logs",
        "/api/account",
        "/api/settings",
        "/api/sim/state",
    ]:
        r = client.get(path)
        assert r.status_code == 200, (path, r.text)


def test_session_lifecycle_over_http(client):
    client.put("/api/risk/limits", json={"max_positions_global": 10, "daily_loss_pct_global": 50})
    s = make_session(client)
    sid = s["id"]
    assert client.post(f"/api/sessions/{sid}/start").json()["status"] == "running"
    client.post("/api/sim/advance", json={"bars": 80})
    detail = client.get(f"/api/sessions/{sid}").json()
    assert detail["trades"], "sim should have produced trades"
    assert all(a["state"] for a in detail["assignments"])
    assert client.post(f"/api/sessions/{sid}/pause").json()["status"] == "paused"
    assert client.post(f"/api/sessions/{sid}/resume").json()["status"] == "running"
    r = client.post(f"/api/sessions/{sid}/stop", json={"close_positions": True})
    assert r.json()["status"] == "stopped"
    assert [p for p in client.get("/api/positions").json() if p["owned"]] == []
    hist = client.get(f"/api/trades?session_id={sid}").json()
    assert hist["stats"]["trades"] >= 1 and hist["curve"]
    bars = client.get("/api/symbols/EURUSD/bars?timeframe=M1&count=200").json()
    assert bars["bars"] and bars["trades"]


def test_domain_errors_map_to_http(client):
    make_session(client, "A", ("EURUSD",))
    make_session(client, "B", ("EURUSD",))
    client.post("/api/sessions/1/start")
    r = client.post("/api/sessions/2/start")
    assert r.status_code == 409 and r.json()["code"] == "symbol_conflict"
    r = client.post("/api/sessions", json={"name": "C", "assignments": [{"symbol": "GOLD"}, {"symbol": "GOLD"}]})
    assert r.status_code == 422 and r.json()["code"] == "duplicate_symbol"
    r = client.post("/api/sessions", json={"name": "D", "assignments": []})
    assert r.status_code == 422
    assert client.get("/api/sessions/999").status_code == 404
    assert client.post("/api/positions/424242/close").status_code == 404


def test_kill_switch_and_auto_stop(client):
    s = make_session(client)
    client.post(f"/api/sessions/{s['id']}/start")
    for _ in range(50):
        client.post("/api/sim/advance", json={"bars": 2})
        if any(p["owned"] for p in client.get("/api/positions").json()):
            break
    res = client.post("/api/kill-switch").json()
    assert res["closed_positions"] >= 1 and "API" in res["stopped_sessions"]
    assert client.get(f"/api/sessions/{s['id']}").json()["status"] == "stopped"
    alerts = client.get("/api/journal?alerts=true").json()
    assert any("KILL SWITCH" in a["message"] for a in alerts)


def test_risk_profiles_and_limits(client):
    r = client.post("/api/risk/profiles", json={"name": "Tiny", "risk_pct": 0.25, "max_spread_points": 15})
    assert r.status_code == 201
    pid = r.json()["id"]
    assert client.post("/api/risk/profiles", json={"name": "Tiny"}).status_code == 409
    r = client.put(f"/api/risk/profiles/{pid}", json={"name": "Tiny", "risk_pct": 0.3, "breakeven": True})
    assert r.json()["risk_pct"] == 0.3
    r = client.put("/api/risk/limits", json={"max_positions_global": 7, "daily_loss_pct_global": 2.5})
    assert r.json()["limits"] == {"max_positions_global": 7, "daily_loss_pct_global": 2.5}
    assert client.delete(f"/api/risk/profiles/{pid}").status_code == 204
    assert client.post("/api/risk/profiles", json={"name": "Bad", "risk_pct": 50}).status_code == 422


def test_settings_roundtrip_and_validation(client):
    r = client.put("/api/settings", json={"poll_interval": 2, "close_on_auto_stop": False})
    assert r.json()["app"]["poll_interval"] == 2 and r.json()["app"]["close_on_auto_stop"] is False
    bad = client.put("/api/settings", json={"default_window": {"open_time": "99:00"}})
    assert bad.status_code == 422


def test_live_enable_requires_typed_confirmation(client):
    r = client.put("/api/account/live-enabled", json={"enabled": True, "confirm": "nope"})
    assert r.status_code == 422 and r.json()["code"] == "confirm_required"
    r = client.put("/api/account/live-enabled", json={"enabled": True, "confirm": "99000001"})
    assert r.json()["live_enabled"] is True


def test_sim_shock_and_clock(client):
    before = client.get("/api/sim/state").json()
    after = client.post("/api/sim/shock", json={"symbol": "GOLD", "pct": 0.01}).json()
    assert after["prices"]["GOLD"] == pytest.approx(before["prices"]["GOLD"] * 1.01, rel=1e-3)
    assert client.post("/api/sim/clock", json={"speed": 120}).json()["speed"] == 120
    assert client.post("/api/sim/shock", json={"symbol": "NOPE", "pct": 0.01}).status_code == 503


def test_websocket_pushes_snapshot_and_journal(client):
    make_session(client)
    with client.websocket_connect("/ws") as ws:
        first = ws.receive_json()
        assert first["type"] == "snapshot" and first["data"]["mode"] == "sim"
        client.post("/api/sessions/1/start")
        seen = set()
        for _ in range(20):
            seen.add(ws.receive_json()["type"])
            if {"journal", "snapshot"} <= seen:
                break
        assert "journal" in seen


def test_sim_controls_refused_outside_sim():
    from types import SimpleNamespace

    from fxcommand.api.routes import _require_sim
    from fxcommand.engine import DomainError

    with pytest.raises(DomainError) as e:
        _require_sim(SimpleNamespace(config=SimpleNamespace(broker="mt5")))
    assert e.value.status == 403 and e.value.code == "sim_only"


def test_symbols_filter_and_names(client):
    assert client.get("/api/symbols/names").json() == ["EURUSD", "GBPUSD", "USDJPY", "GOLD"]
    rows = client.get("/api/symbols?q=usd&limit=2").json()
    assert len(rows) == 2 and all("USD" in r["name"] for r in rows)
    make_session(client, "Uses GOLD", ("GOLD",))
    assert client.get("/api/symbols?limit=1").json()[0]["name"] == "GOLD"  # used symbols first


def test_learning_endpoints(client):
    client.put("/api/risk/limits", json={"max_positions_global": 20, "daily_loss_pct_global": 90})
    client.put("/api/settings", json={"learning_candidates": 20, "learning_bars": 2000})
    s = make_session(client, "Learner", ("EURUSD",))
    sid = s["id"]
    client.post(f"/api/sessions/{sid}/start")
    client.post("/api/sim/advance", json={"bars": 30})
    ov = client.get("/api/learning").json()
    assert ov["enabled"] and ov["arenas"][0]["symbol"] == "EURUSD"
    detail = client.get("/api/learning/arenas/EURUSD/M1").json()
    assert detail["history"][0]["kind"] == "initial"
    run = client.post("/api/learning/arenas/EURUSD/M1/optimize").json()
    assert client.get(f"/api/learning/runs/{run['run_id']}").status_code == 200
    r = client.post(f"/api/learning/slots/{sid}/EURUSD/rollback")
    assert r.status_code == 409 and r.json()["code"] == "nothing_to_roll_back"
    r = client.put(f"/api/learning/slots/{sid}/EURUSD/auto-promote", json={"enabled": True})
    assert r.json()["auto_promote"] is True
    assert client.post("/api/learning/challengers/9999/promote", json={"session_id": sid}).status_code == 404
    assert client.get("/api/learning/arenas/GOLD/H4").status_code == 404
    assert client.post("/api/learning/run").json()["queued"]


def test_sim_resumes_after_recorded_history():
    from fxcommand.app import sim_resume_start

    assert sim_resume_start(0) is None
    fri_22 = 1_704_700_800 + 4 * 86400 + 14 * 3600  # Friday 22:00
    assert sim_resume_start(fri_22) == 1_704_700_800 + 7 * 86400  # next Monday 08:00
    tue_10 = 1_704_700_800 + 86400 + 2 * 3600
    assert sim_resume_start(tue_10) == 1_704_700_800 + 2 * 86400  # Wednesday 08:00


def test_sim_injected_challenger_is_deduplicated(client):
    s = make_session(client, "Inj", ("GOLD",))
    client.post(f"/api/sessions/{s['id']}/start")
    body = {"symbol": "GOLD", "timeframe": "M1", "strategy": "ema_cross", "params": {"fast": 5, "slow": 20}}
    a = client.post("/api/sim/learning/challenger", json=body).json()
    b = client.post("/api/sim/learning/challenger", json=body).json()
    assert a["id"] == b["id"]
    arena = client.get("/api/learning/arenas/GOLD/M1").json()
    assert len(arena["challengers"]) == 1
