"""HTTP + WebSocket API. One router section per dashboard page."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Literal

from fastapi import APIRouter, Query, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from ..broker import BrokerError, Timeframe
from ..engine import AssignmentIn, DomainError, SessionIn, trade_stats
from ..journal import journal_dict
from ..notify import mask, telegram_from_settings
from ..risk import RiskLimits, TradingWindow
from ..store import DEFAULT_APP_SETTINGS, RiskProfileRow, SessionRow, TradeRow
from ..strategies import STRATEGIES

router = APIRouter()
ws_router = APIRouter()
log = logging.getLogger("fxcommand.api")


def rt(request: Request):
    return request.app.state.rt


# =================================================================== helpers
def trade_dict(t: TradeRow) -> dict:
    return t.model_dump()


def session_dict(r, s: SessionRow, with_detail: bool = False) -> dict:
    store, mgr = r.store, r.manager
    assigns = store.assignments(s.id)
    profiles = {p.id: p.name for p in store.risk_profiles()}
    states = mgr.assignment_states(s.id)
    snap = next((x for x in mgr.snapshot()["sessions"] if x["id"] == s.id), {})
    trades = store.trades(session_id=s.id, limit=5000)
    d = {
        **s.model_dump(),
        "magics": snap.get("magics", [s.magic]),
        # Weekend Close cuts the tested edge of trades held for days (D6): the editor warns with this
        "weekend_close_warning": bool(s.weekend_close and any(a.timeframe in ("H4", "D1") for a in assigns)),
        "window_text": TradingWindow.from_dict(s.window).describe(),
        "cost_overrides": [{"symbol": o.symbol, "timeframe": o.timeframe, "reason": o.reason, "set_ts": o.set_ts} for o in store.cost_overrides(s.id)],
        "assignments": [
            {**a.model_dump(), "risk_profile_name": profiles.get(a.risk_profile_id), "state": states.get(a.id)} for a in assigns
        ],
        "open_positions": snap.get("open_positions", 0),
        "day_pnl": snap.get("day_pnl", 0.0),
        # the Session's current Execution Mode only: a Session switched between Paper and Broker never mixes them (D16)
        "stats": trade_stats(t for t in trades if t.paper == (s.execution == "paper")),
    }
    if with_detail:
        d["positions"] = [p for p in mgr.positions_view() if p["session_id"] == s.id]
        d["trades"] = [trade_dict(t) for t in trades[:300]]
        d["journal"] = [journal_dict(j) for j in store.journal(session_id=s.id, limit=150)]
        d["equity"] = _pnl_curve(trades)
    return d


def _pnl_curve(trades: list[TradeRow]) -> list[dict]:
    closed = sorted((t for t in trades if t.status == "closed" and t.profit is not None), key=lambda t: t.close_time or 0)
    total, out = 0.0, []
    for t in closed:
        total += t.profit
        out.append({"ts": t.close_time, "value": round(total, 2)})
    return out


# ================================================================= overview
@router.get("/status")
async def status(request: Request):
    r = rt(request)
    return {**r.manager.snapshot(), "version": "0.1.0", "broker_mode": r.config.broker}


@router.get("/overview")
async def overview(request: Request):
    r = rt(request)
    snap = r.manager.snapshot()
    today = r.store.trades(since=snap["day_start"], limit=5000)
    closed_today = [t for t in today if t.status == "closed"]
    all_trades = r.store.trades(limit=100_000)
    epoch = r.store.paper_epoch()
    return {
        **snap,
        "equity_curve": [{"ts": e.ts, "equity": e.equity, "balance": e.balance} for e in r.store.equity_curve(limit=1000)],
        "alerts": [journal_dict(j) for j in r.store.journal(alerts_only=True, limit=20)],
        "recent": [journal_dict(j) for j in r.store.journal(limit=15)],
        # the real Account and the Paper Account are never mixed (D16); Paper = the current epoch
        "today": trade_stats(t for t in closed_today if not t.paper),
        "all_time": trade_stats(t for t in all_trades if not t.paper),
        "today_paper": trade_stats(t for t in closed_today if t.paper and t.paper_epoch == epoch),
        "all_time_paper": trade_stats(t for t in all_trades if t.paper and t.paper_epoch == epoch),
        "paper_account": r.manager.paper_account_view(),
    }


# ================================================================= sessions
@router.get("/sessions")
async def list_sessions(request: Request):
    r = rt(request)
    return [session_dict(r, s) for s in r.store.list_sessions()]


@router.post("/sessions", status_code=201)
async def create_session(request: Request, spec: SessionIn):
    r = rt(request)
    s = await r.manager.create_session(spec)
    return session_dict(r, s, True)


@router.get("/sessions/{session_id}")
async def get_session(request: Request, session_id: int):
    r = rt(request)
    return session_dict(r, r.store.get_session(session_id), True)


@router.put("/sessions/{session_id}")
async def update_session(request: Request, session_id: int, spec: SessionIn):
    r = rt(request)
    return session_dict(r, await r.manager.update_session(session_id, spec), True)


@router.delete("/sessions/{session_id}", status_code=204)
async def delete_session(request: Request, session_id: int):
    await rt(request).manager.delete_session(session_id)


class StopIn(BaseModel):
    close_positions: bool = False


@router.post("/sessions/{session_id}/start")
async def start_session(request: Request, session_id: int):
    r = rt(request)
    return session_dict(r, await r.manager.start(session_id))


@router.post("/sessions/{session_id}/pause")
async def pause_session(request: Request, session_id: int):
    r = rt(request)
    return session_dict(r, await r.manager.pause(session_id))


@router.post("/sessions/{session_id}/resume")
async def resume_session(request: Request, session_id: int):
    r = rt(request)
    return session_dict(r, await r.manager.resume(session_id))


@router.post("/sessions/{session_id}/stop")
async def stop_session(request: Request, session_id: int, body: StopIn | None = None):
    r = rt(request)
    return session_dict(r, await r.manager.stop(session_id, (body or StopIn()).close_positions))


# ================================================================== symbols
def _quote_rows(b, names: list[str]) -> list[dict]:
    out = []
    for name in names:
        try:
            info, tick = b.symbol_info(name), b.tick(name)
        except BrokerError:
            continue
        out.append({**info.to_dict(), "bid": tick.bid, "ask": tick.ask, "spread_points": tick.spread_points, "time": tick.time})
    return out


@router.get("/symbols/names")
async def symbol_names(request: Request):
    """Every symbol the Broker offers (cheap: names only). For pickers."""
    return await rt(request).broker.run(lambda b: b.symbols())


@router.get("/symbols")
async def symbols(request: Request, q: str = "", limit: int = Query(60, ge=1, le=500)):
    """Quotes for symbols matching ``q``. Symbols used by a Session always come first.

    A real Market Watch can hold well over a thousand symbols, so quotes are only
    fetched for at most ``limit`` of them.
    """
    r = rt(request)
    active = {s.id for s in r.store.list_sessions() if s.status in ("running", "paused")}
    sessions = {s.id: s for s in r.store.list_sessions()}
    used: dict[str, list[dict]] = {}
    for a in r.store.all_assignments():
        s = sessions.get(a.session_id)
        if s is None:
            continue
        used.setdefault(a.symbol, []).append(
            {"session_id": s.id, "session_name": s.name, "status": s.status, "timeframe": a.timeframe, "strategy": a.strategy, "active": s.id in active}
        )
    ql = q.strip().lower()

    def pick(b) -> list[dict]:
        names = b.symbols()
        match = [n for n in names if not ql or ql in n.lower()]
        first = [n for n in match if n in used]
        rest = [n for n in match if n not in used]
        return _quote_rows(b, (first + rest)[:limit])

    rows = await r.broker.run(pick)
    open_by_symbol: dict[str, int] = {}
    for p in r.manager.positions_view():
        if p["owned"]:
            open_by_symbol[p["symbol"]] = open_by_symbol.get(p["symbol"], 0) + 1
    return [{**row, "used_by": used.get(row["name"], []), "open_positions": open_by_symbol.get(row["name"], 0)} for row in rows]


@router.get("/symbols/{symbol}/bars")
async def bars(request: Request, symbol: str, timeframe: Timeframe = Timeframe.M15, count: int = Query(300, ge=10, le=5000)):
    r = rt(request)
    df = await r.broker.run(lambda b: b.closed_bars(symbol, timeframe, count))
    since = int(df["time"].iloc[0]) if len(df) else 0
    trades = [t for t in r.store.trades(symbol=symbol, limit=2000) if (t.close_time or t.open_time) >= since]
    return {
        "symbol": symbol,
        "timeframe": timeframe.value,
        "bars": json.loads(df.to_json(orient="records")),
        "trades": [trade_dict(t) for t in trades],
    }


# =============================================================== strategies
@router.get("/strategies")
async def strategies(request: Request):
    r = rt(request)
    trades = r.store.trades(limit=100_000)
    epoch = r.store.paper_epoch()
    assigns = r.store.all_assignments()
    out = []
    for key, s in STRATEGIES.items():
        mine = [t for t in trades if t.strategy == key and not t.paper]
        paper = [t for t in trades if t.strategy == key and t.paper and t.paper_epoch == epoch]
        out.append(
            {
                **s.describe(),
                "stats": trade_stats(mine),  # the real Account only (D16)
                "stats_paper": trade_stats(paper),  # the Paper Account, current epoch
                "assignments": sum(1 for a in assigns if a.strategy == key),
                "by_symbol": {sym: trade_stats([t for t in mine if t.symbol == sym]) for sym in sorted({t.symbol for t in mine})},
                "by_symbol_paper": {sym: trade_stats([t for t in paper if t.symbol == sym]) for sym in sorted({t.symbol for t in paper})},
            }
        )
    return out


# ===================================================================== risk
class LimitsIn(BaseModel):
    max_positions_global: int = Field(ge=1, le=500)
    daily_loss_pct_global: float = Field(gt=0, le=100)


class ProfileIn(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    risk_pct: float = Field(1.0, gt=0, le=10)
    max_spread_points: float = Field(30, gt=0, le=10_000)
    breakeven: bool = False
    breakeven_at_r: float = Field(1.0, gt=0, le=20)
    trailing: bool = False
    trailing_atr: float = Field(2.0, gt=0, le=50)
    trailing_start_r: float = Field(1.0, ge=0, le=20)
    allow_min_lot: bool = False
    min_lot_max_risk_pct: float = Field(2.0, gt=0, le=10)


class LiveCapsIn(BaseModel):
    max_risk_pct: float = Field(gt=0, le=10)
    max_volume: float = Field(gt=0, le=100)
    confirm_login: int | None = None


class FloorResetIn(BaseModel):
    confirm_login: int
    pct: float = Field(80.0, gt=0, lt=100)


@router.get("/risk")
async def risk(request: Request):
    r = rt(request)
    snap = r.manager.snapshot()
    limits = r.store.global_limits()
    owned = [p for p in snap["positions"] if p["owned"] and not p["paper"]]  # the Account's exposure: Paper excluded
    profiles = r.store.risk_profiles()
    assigns = r.store.all_assignments()
    return {
        "limits": {"max_positions_global": limits.max_positions_global, "daily_loss_pct_global": limits.daily_loss_pct_global},
        "profiles": [{**p.model_dump(), "used_by": sum(1 for a in assigns if a.risk_profile_id == p.id)} for p in profiles],
        "exposure": {
            "open_positions": len(owned),
            "day_pnl": snap["day_pnl"],
            "day_start_equity": snap["day_start_equity"],
            "daily_loss_cap": round(-limits.daily_loss_pct_global / 100 * (snap["day_start_equity"] or 0), 2),
            "floating": round(sum(p["profit"] for p in owned), 2),
            "risk_at_stop": round(sum(p["risk_amount"] or 0 for p in owned), 2),
        },
        "kill_switch_at": snap["kill_switch_at"],
        "live_caps": snap["live_caps"],
        "equity_floor": snap["equity_floor"],
        "account_live": bool(snap["account"] and not snap["account"]["is_demo"]),
        "login": snap["account"]["login"] if snap["account"] else None,
        "equity": snap["account"]["equity"] if snap["account"] else None,
    }


@router.put("/risk/live-caps")
async def set_live_caps(request: Request, body: LiveCapsIn):
    """Live Caps bind only live Accounts; changing them while a live Account is connected needs the typed account number."""
    r = rt(request)
    acct = r.manager.snapshot()["account"]
    if acct and not acct["is_demo"] and body.confirm_login != acct["login"]:
        raise DomainError("confirm_required", f"type the account number {acct['login']} to change Live Caps on a LIVE account", 422)
    caps = r.store.set_live_caps({"max_risk_pct": body.max_risk_pct, "max_volume": body.max_volume})
    r.journal.record("alert" if acct and not acct["is_demo"] else "state", f"Live Caps changed: risk ≤ {caps['max_risk_pct']}%, volume ≤ {caps['max_volume']} lots",
                     ts=r.manager.now, level="warn", alert=bool(acct and not acct["is_demo"]))
    return await risk(request)


@router.post("/risk/equity-floor/reset")
async def reset_floor(request: Request, body: FloorResetIn):
    r = rt(request)
    await r.manager.reset_equity_floor(body.confirm_login, body.pct)
    return await risk(request)


@router.put("/risk/limits")
async def set_limits(request: Request, body: LimitsIn):
    r = rt(request)
    r.store.set_global_limits(RiskLimits(max_positions_global=body.max_positions_global, daily_loss_pct_global=body.daily_loss_pct_global))
    r.journal.record("state", f"Global risk limits changed: {body.model_dump()}", ts=r.manager.now)
    return await risk(request)


@router.post("/risk/profiles", status_code=201)
async def create_profile(request: Request, body: ProfileIn):
    r = rt(request)
    if any(p.name == body.name for p in r.store.risk_profiles()):
        raise DomainError("name_taken", f"risk profile {body.name!r} already exists")
    return r.store.save_risk_profile(RiskProfileRow(**body.model_dump())).model_dump()


@router.put("/risk/profiles/{profile_id}")
async def update_profile(request: Request, profile_id: int, body: ProfileIn):
    r = rt(request)
    if any(p.name == body.name and p.id != profile_id for p in r.store.risk_profiles()):
        raise DomainError("name_taken", f"risk profile {body.name!r} already exists")
    return r.store.save_risk_profile(RiskProfileRow(id=profile_id, **body.model_dump())).model_dump()


@router.delete("/risk/profiles/{profile_id}", status_code=204)
async def delete_profile(request: Request, profile_id: int):
    try:
        rt(request).store.delete_risk_profile(profile_id)
    except ValueError as e:
        raise DomainError("in_use", str(e)) from None


@router.post("/kill-switch")
async def kill_switch(request: Request):
    return await rt(request).manager.kill_all()


# ================================================================ positions
@router.get("/positions")
async def positions(request: Request):
    return rt(request).manager.positions_view()


@router.post("/positions/{ticket}/close")
async def close_position(request: Request, ticket: int):
    return await rt(request).manager.close_position(ticket)


# ================================================================== history
@router.get("/trades")
async def trades(
    request: Request,
    session_id: int | None = None,
    symbol: str | None = None,
    strategy: str | None = None,
    status: str | None = None,
    since: int | None = None,
    until: int | None = None,
    account: Literal["real", "paper"] | None = None,
    limit: int = Query(1000, ge=1, le=20_000),
):
    r = rt(request)
    rows = r.store.trades(session_id=session_id, symbol=symbol, status=status, since=since, until=until, limit=limit)
    if strategy:
        rows = [t for t in rows if t.strategy == strategy]
    if account is not None:
        rows = [t for t in rows if t.paper == (account == "paper")]
    names = {s.id: s.name for s in r.store.list_sessions()}
    return {
        "trades": [{**trade_dict(t), "session_name": names.get(t.session_id)} for t in rows],
        "stats": trade_stats(rows),
        "stats_real": trade_stats(t for t in rows if not t.paper),
        "stats_paper": trade_stats(t for t in rows if t.paper),
        "curve": _pnl_curve(rows),
    }


# ================================================================== journal
@router.get("/journal")
async def journal(
    request: Request,
    session_id: int | None = None,
    kind: list[str] | None = Query(None),
    level: str | None = None,
    alerts: bool = False,
    symbol: str | None = None,
    before_id: int | None = None,
    limit: int = Query(200, ge=1, le=2000),
):
    rows = rt(request).store.journal(session_id, kind, level, alerts, symbol, before_id, limit)
    return [journal_dict(j) for j in rows]


@router.get("/logs")
async def logs(request: Request, level: str | None = None, after: int = 0, limit: int = Query(500, ge=1, le=2000)):
    return rt(request).logs.lines(level, after, limit)


# ================================================================== account
class LiveIn(BaseModel):
    enabled: bool
    confirm: str = ""


@router.get("/account")
async def account(request: Request):
    r = rt(request)
    snap = r.manager.snapshot()
    return {
        "mode": r.config.broker,
        "connected": snap["connected"],
        "last_error": snap["last_error"],
        "server_time": snap["server_time"],
        "account": snap["account"],
        "live_enabled": snap["live_enabled"],
        "terminal_path": r.config.mt5_path,
        "db": r.config.db_url,
        "equity_floor": snap["equity_floor"],
        "heartbeat_age": snap["heartbeat_age"],
    }


@router.get("/preflight")
async def preflight(request: Request, session_id: int | None = None):
    """Pre-flight Check. With ``session_id``: that Session's Symbols and Execution Mode."""
    return await rt(request).manager.preflight(session_id)


@router.put("/account/live-enabled")
async def set_live(request: Request, body: LiveIn):
    r = rt(request)
    acct = r.manager.snapshot()["account"]
    if not acct:
        raise DomainError("broker_offline", "no account connected", 503)
    if body.enabled and body.confirm.strip() != str(acct["login"]):
        raise DomainError("confirm_required", f"type the account number {acct['login']} to enable live trading", 422)
    r.store.set_live_enabled(acct["login"], body.enabled)
    if body.enabled and not acct["is_demo"] and r.store.equity_floor(acct["login"]) is None:
        pct = 80.0
        r.store.set_equity_floor(acct["login"], {"floor": round(acct["equity"] * pct / 100, 2), "pct": pct, "set_at": r.manager.now, "breached_at": None})
    r.journal.record(
        "alert" if body.enabled else "state",
        f"Live trading {'ENABLED' if body.enabled else 'disabled'} for account {acct['login']}",
        ts=r.manager.now,
        level="warn" if body.enabled else "info",
        alert=body.enabled,
    )
    return await account(request)


@router.post("/account/reconnect")
async def reconnect(request: Request):
    r = rt(request)
    r.manager.status.connected = False
    await r.manager.tick_once()
    return await account(request)


# ================================================================= settings
class SettingsIn(BaseModel):
    learning_enabled: bool | None = None
    learning_candidates: int | None = Field(None, ge=10, le=2000)
    learning_bars: int | None = Field(None, ge=500, le=50_000)
    poll_interval: float | None = Field(None, ge=0.2, le=60)
    sim_speed: float | None = Field(None, ge=0, le=6000)
    close_on_auto_stop: bool | None = None
    equity_snapshot_seconds: int | None = Field(None, ge=60, le=86400)
    default_window: dict[str, Any] | None = None
    cost_check_max_r: float | None = Field(None, ge=0.01, le=10)


def _notify_view(r) -> dict:
    n = r.store.notify_settings()
    return {
        "enabled": bool(n.get("enabled", True)),
        "telegram_token": mask(n.get("telegram_token") or ""),
        "telegram_chat_id": str(n.get("telegram_chat_id") or ""),
        "configured": telegram_from_settings(n) is not None,
    }


@router.get("/settings")
async def get_settings(request: Request):
    r = rt(request)
    return {"app": r.store.app_settings(), "defaults": DEFAULT_APP_SETTINGS, "mode": r.config.broker, "notify": _notify_view(r)}


class NotifyIn(BaseModel):
    enabled: bool | None = None
    telegram_token: str | None = Field(None, max_length=200)  # omitted = keep; "" = clear
    telegram_chat_id: str | None = Field(None, max_length=64)


@router.put("/settings/notify")
async def put_notify(request: Request, body: NotifyIn):
    r = rt(request)
    cur = r.store.notify_settings()
    patch = {k: (v.strip() if isinstance(v, str) else v) for k, v in body.model_dump().items() if v is not None}
    r.store.set_setting("notify", {**cur, **patch})
    r.journal.record("state", f"Notification settings changed: {', '.join(k for k in patch)}", ts=r.manager.now)
    return _notify_view(r)


@router.post("/settings/notify/test")
async def notify_test(request: Request):
    r = rt(request)
    entry = await r.notifier.deliver(f"✅ FXCommand {r.notifier.label()} test message — alerts will arrive here.", force=True)
    if entry["status"] != "sent":
        # not configured is the operator's to fix (422); a delivery failure is Telegram's side (502)
        raise DomainError("notify_failed", f"test message not delivered: {entry['status']}", 502 if entry["status"].startswith("failed") else 422)
    return entry


@router.get("/notify/outbox")
async def notify_outbox(request: Request):
    return list(rt(request).notifier.outbox)


@router.put("/settings")
async def put_settings(request: Request, body: SettingsIn):
    r = rt(request)
    patch = {k: v for k, v in body.model_dump().items() if v is not None}
    merged = r.store.update_app_settings(patch)
    r.journal.record("state", f"Settings changed: {', '.join(patch)}", ts=r.manager.now)
    return {"app": merged, "defaults": DEFAULT_APP_SETTINGS, "mode": r.config.broker, "notify": _notify_view(r)}


# ================================================================= learning
def _is_live(r) -> bool:
    acct = r.manager.snapshot()["account"]
    return bool(acct and not acct["is_demo"])


class PromoteIn(BaseModel):
    session_id: int
    force: bool = False


class AutoPromoteIn(BaseModel):
    enabled: bool


@router.get("/learning")
async def learning_overview(request: Request):
    r = rt(request)
    return r.learning.overview(_is_live(r))


@router.get("/learning/arenas/{symbol}/{timeframe}")
async def learning_arena(request: Request, symbol: str, timeframe: Timeframe):
    r = rt(request)
    return r.learning.arena(symbol, timeframe.value, _is_live(r))


@router.post("/learning/arenas/{symbol}/{timeframe}/optimize")
async def learning_optimize(request: Request, symbol: str, timeframe: Timeframe):
    run_id = rt(request).learning.request_optimize(symbol, timeframe.value, "manual")
    return {"run_id": run_id}


@router.post("/learning/run")
async def learning_run_all(request: Request):
    """'Learn now': queue an Optimizer Run for every Arena traded by a running or paused Session."""
    r = rt(request)
    runs = []
    for s in r.store.list_sessions():
        if s.status in ("running", "paused"):
            for a in r.store.assignments(s.id):
                runs.append({"symbol": a.symbol, "timeframe": a.timeframe, "run_id": r.learning.request_optimize(a.symbol, a.timeframe, "manual")})
    return {"queued": runs}


@router.get("/learning/runs/{run_id}")
async def learning_run(request: Request, run_id: int):
    row = rt(request).learning.repo.run(run_id)
    if row is None:
        raise DomainError("not_found", f"optimizer run {run_id} not found", 404)
    return row.model_dump()


@router.post("/learning/challengers/{challenger_id}/promote")
async def learning_promote(request: Request, challenger_id: int, body: PromoteIn):
    return rt(request).learning.promote(challenger_id, body.session_id, body.force).model_dump()


@router.post("/learning/slots/{session_id}/{symbol}/rollback")
async def learning_rollback(request: Request, session_id: int, symbol: str, timeframe: str | None = None):
    return rt(request).learning.rollback(session_id, symbol, timeframe).model_dump()


@router.put("/learning/slots/{session_id}/{symbol}/auto-promote")
async def learning_auto_promote(request: Request, session_id: int, symbol: str, body: AutoPromoteIn, timeframe: str | None = None):
    r = rt(request)
    return r.learning.set_auto_promote(session_id, symbol, body.enabled, _is_live(r), timeframe)


@router.delete("/learning/pending/{pending_id}", status_code=204)
async def learning_cancel_pending(request: Request, pending_id: int):
    service = rt(request).learning
    row = next((p for p in service.repo.all_pending() if p.id == pending_id), None)
    if row is None:
        raise DomainError("not_found", f"no pending change {pending_id}", 404)
    service.cancel(row, "operator")


# ============================================================ cost check
@router.get("/sessions/{session_id}/cost-check")
async def session_cost_check(request: Request, session_id: int):
    """Each Assignment's cost (spread + slippage) in R of its typical stop, and whether it may start.
    The override is part of the Session update payload (``cost_overrides``)."""
    r = rt(request)
    s = r.store.get_session(session_id)
    return {"threshold": float(r.store.app_settings()["cost_check_max_r"]), "assignments": await r.manager.cost_check(s)}


class CostPreviewIn(BaseModel):
    session_id: int | None = None
    assignments: list[AssignmentIn] = Field(min_length=1, max_length=50)


@router.post("/cost-check")
async def cost_check_preview(request: Request, body: CostPreviewIn):
    """The Cost Check for an editor's unsaved Assignments (read-only)."""
    r = rt(request)
    return {"threshold": float(r.store.app_settings()["cost_check_max_r"]), "assignments": await r.manager.cost_check_preview(body.session_id, body.assignments)}


# ============================================================== evidence
class EvidenceIn(BaseModel):
    symbol: str = Field(min_length=1, max_length=40)
    timeframe: Timeframe
    strategy: str
    params: dict[str, Any] = Field(default_factory=dict)
    session_id: int | None = None  # its Trading Window and Weekend Close
    risk_profile_id: int | None = None  # its exit rules (default: no breakeven, no trailing stop)
    reverse_on_opposite: bool = True
    window: dict[str, Any] | None = None
    weekend_close: bool = False
    weekend_close_time: str = "22:30"


def _evidence_spec(r, body: EvidenceIn):
    return r.learning.spec_from(
        body.symbol, body.timeframe.value, body.strategy, body.params, body.session_id, body.risk_profile_id,
        body.reverse_on_opposite, body.window, body.weekend_close_time if body.weekend_close else None,
    )


@router.get("/evidence")
async def evidence_list(request: Request, symbol: str | None = None, timeframe: Timeframe | None = None, strategy: str | None = None):
    return rt(request).learning.evidence(symbol, timeframe.value if timeframe else None, strategy)


@router.get("/evidence/{evidence_id}")
async def evidence_get(request: Request, evidence_id: int):
    from ..learning.evidence import summary

    row = rt(request).learning.repo.evidence_row(evidence_id)
    if row is None:
        raise DomainError("not_found", f"evidence {evidence_id} not found", 404)
    return summary(row)


@router.post("/evidence/match")
async def evidence_match(request: Request, body: EvidenceIn):
    """The editor's badge for settings that may not be saved yet (read-only)."""
    r = rt(request)
    spec = _evidence_spec(r, body)
    out = r.learning.evidence_for(spec)
    if spec.weekend_close and body.timeframe.value in ("H4", "D1"):
        out["without_weekend_close"] = r.learning.evidence_for(spec.with_weekend_close(None))
    return out


@router.post("/evidence/run", status_code=202)
async def evidence_run(request: Request, body: EvidenceIn):
    """Queue an Evidence Run (dashboard only: not exposed to MCP)."""
    r = rt(request)
    return r.learning.request_evidence(_evidence_spec(r, body))


@router.get("/sessions/{session_id}/evidence")
async def session_evidence(request: Request, session_id: int):
    r = rt(request)
    s = r.store.get_session(session_id)
    return [
        {"assignment_id": a.id, "symbol": a.symbol, "timeframe": a.timeframe, "strategy": a.strategy, **r.learning.assignment_evidence(s, a)}
        for a in r.store.assignments(session_id)
    ]


@router.get("/scorecard")
async def scorecard(request: Request):
    return await rt(request).learning.scorecard()


# ========================================================== paper account
class PaperAccountIn(BaseModel):
    start_balance: float = Field(gt=0, le=10_000_000)


class PaperResetIn(BaseModel):
    confirm: str
    start_balance: float | None = Field(None, gt=0, le=10_000_000)


@router.get("/paper-account")
async def paper_account(request: Request):
    return rt(request).manager.paper_account_view()


@router.put("/paper-account")
async def put_paper_account(request: Request, body: PaperAccountIn):
    r = rt(request)
    await r.manager.set_paper_start_balance(body.start_balance)
    return r.manager.paper_account_view()


@router.post("/paper-account/reset")
async def reset_paper_account(request: Request, body: PaperResetIn):
    r = rt(request)
    await r.manager.reset_paper_account(body.confirm, body.start_balance)
    return r.manager.paper_account_view()


# ======================================================= strategy review
class TrialIn(BaseModel):
    symbol: str = Field(min_length=1, max_length=40)
    timeframe: Timeframe
    hypothesis: str = Field(min_length=1, max_length=2000)
    strategy: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    data_from: int
    data_to: int
    result: dict[str, Any] = Field(default_factory=dict)
    source: Literal["cli", "reviewer"] = "cli"


class HoldoutIn(BaseModel):
    symbol: str = Field(min_length=1, max_length=40)
    timeframe: Timeframe
    strategy: str
    params: dict[str, Any] = Field(default_factory=dict)
    hypothesis: str = Field(min_length=1, max_length=2000)


class ReviewIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    summary: str = Field(min_length=1, max_length=1000)
    report: str = Field("", max_length=200_000)
    arenas: list[str] = Field(default_factory=list)
    finalists: list[dict[str, Any]] = Field(default_factory=list)
    actions: list[dict[str, Any]] = Field(default_factory=list)
    period_from: int | None = None
    period_to: int | None = None


class ChallengerIn(BaseModel):
    symbol: str = Field(min_length=1, max_length=40)
    timeframe: Timeframe
    strategy: str
    params: dict[str, Any] = Field(default_factory=dict)
    note: str = Field("", max_length=500)


@router.get("/research/snapshot")
async def research_snapshot(
    request: Request, symbol: str, timeframe: Timeframe, tail: int | None = Query(None, ge=0, le=1_000_000), arena_timeframe: Timeframe | None = None
):
    """Closed bars of an Arena before its sealed holdout (read-only), with today's costs. With
    ``arena_timeframe`` the bars are another Arena's context and end at that Arena's holdout."""
    return await rt(request).learning.snapshot(symbol, timeframe.value, tail, arena_timeframe.value if arena_timeframe else None)


@router.get("/research/trials")
async def research_trials(request: Request, symbol: str | None = None, timeframe: Timeframe | None = None, limit: int = Query(100, ge=1, le=5000)):
    service = rt(request).learning
    rows = service.repo.trials(symbol, timeframe.value if timeframe else None, limit)
    return {"arenas": service.trial_summary(), "trials": [t.model_dump() for t in rows]}


@router.post("/research/trials", status_code=201)
async def research_add_trial(request: Request, body: TrialIn):
    return await rt(request).learning.record_trial(
        body.symbol, body.timeframe.value, body.hypothesis, body.data_from, body.data_to, body.result, body.strategy, body.params, body.source
    )


@router.post("/research/holdout")
async def research_holdout(request: Request, body: HoldoutIn):
    """Score a finalist once on the sealed holdout; recorded in the ledger; a failure is final."""
    return await rt(request).learning.evaluate_holdout(body.symbol, body.timeframe.value, body.strategy, body.params, body.hypothesis)


@router.get("/reviews")
async def reviews(request: Request, limit: int = Query(50, ge=1, le=500)):
    return [rv.model_dump(exclude={"report"}) for rv in rt(request).learning.repo.reviews(limit)]


@router.get("/reviews/{review_id}")
async def review(request: Request, review_id: int):
    row = rt(request).learning.repo.review(review_id)
    if row is None:
        raise DomainError("not_found", f"review {review_id} not found", 404)
    return row.model_dump()


@router.post("/reviews", status_code=201)
async def add_review(request: Request, body: ReviewIn):
    return rt(request).learning.add_review(**body.model_dump())


@router.post("/learning/challengers", status_code=202)
async def submit_challenger(request: Request, body: ChallengerIn):
    """A reviewer's Candidate for an Arena: judged by walk-forward, then Shadow Trades under the same
    Guardrails. Never promotes and never touches a Session."""
    return rt(request).learning.submit_challenger(body.symbol, body.timeframe.value, body.strategy, body.params, body.note)


# ======================================================== sim test controls
def _require_sim(r) -> None:
    if r.config.broker != "sim":
        raise DomainError("sim_only", "simulation controls are disabled when BROKER=mt5", 403)


class AdvanceIn(BaseModel):
    bars: int = Field(1, ge=1, le=5000)


class ShockIn(BaseModel):
    symbol: str
    pct: float = Field(ge=-0.5, le=0.5)


class ClockIn(BaseModel):
    speed: float = Field(ge=0, le=6000)


@router.get("/sim/state")
async def sim_state(request: Request):
    r = rt(request)
    _require_sim(r)
    return {**(await r.broker.run(lambda b: b.state())), "speed": r.store.app_settings()["sim_speed"]}


@router.post("/sim/advance")
async def sim_advance(request: Request, body: AdvanceIn):
    """Step the simulated market bar by bar, running one engine pass after each bar (deterministic)."""
    r = rt(request)
    _require_sim(r)
    for _ in range(body.bars):
        await r.broker.run(lambda b: b.step(1))
        await r.manager.tick_once()
    return await sim_state(request)


@router.post("/sim/shock")
async def sim_shock(request: Request, body: ShockIn):
    r = rt(request)
    _require_sim(r)
    await r.broker.run(lambda b: b.shock(body.symbol, body.pct))
    await r.manager.tick_once()
    return await sim_state(request)


class InjectChallengerIn(BaseModel):
    symbol: str
    timeframe: Timeframe
    strategy: str
    params: dict[str, Any] = Field(default_factory=dict)


@router.post("/sim/learning/challenger")
async def sim_inject_challenger(request: Request, body: InjectChallengerIn):
    """Test control (sim only): add a Challenger without evidence, to exercise the Promote/Rollback path.
    On a random-walk market the Optimizer rightly finds none."""
    r = rt(request)
    _require_sim(r)
    from ..learning.candidate import Candidate

    try:
        cand = Candidate.of(body.strategy, body.params)
    except KeyError as e:
        raise DomainError("unknown_strategy", str(e.args[0]), 422) from None
    repo = r.learning.repo
    existing = next((c for c in repo.challengers(body.symbol, body.timeframe.value) if c.candidate_key == cand.key), None)
    if existing:
        return {"id": existing.id, "candidate": cand.to_dict()}
    repo.ensure_candidate(cand)
    ch = repo.add_challenger(
        symbol=body.symbol, timeframe=body.timeframe.value, candidate_key=cand.key, started_ts=r.manager.now, note="injected (sim test control)"
    )
    r.learning._trim_challengers(body.symbol, body.timeframe.value, r.manager.now)
    return {"id": ch.id, "candidate": cand.to_dict()}


class FaultIn(BaseModel):
    kind: str
    count: int = Field(1, ge=1, le=50)


class SimAccountIn(BaseModel):
    login: int | None = None
    is_demo: bool | None = None
    margin_mode: str | None = None
    algo_trading: bool | None = None


class SimSymbolIn(BaseModel):
    symbol: str
    trade_mode: str = Field(pattern="^(full|longonly|shortonly|closeonly|disabled)$")


def _sim(b):
    return getattr(b, "inner", b)  # the SimBroker behind the PaperRouter


@router.post("/sim/fault")
async def sim_fault(request: Request, body: FaultIn):
    """Test control (sim only): queue broker faults — requote, timeout_filled, timeout_none, partial,
    price_zero, hang, close_fail, close_timeout_done."""
    r = rt(request)
    _require_sim(r)
    await r.broker.run(lambda b: _sim(b).inject(body.kind, body.count))
    return await sim_state(request)


@router.post("/sim/account")
async def sim_account(request: Request, body: SimAccountIn):
    """Test control (sim only): pretend the terminal switched account / became live / netting."""
    r = rt(request)
    _require_sim(r)

    def apply(b):
        sim = _sim(b)
        for k, v in body.model_dump().items():
            if v is not None:
                setattr(sim, k, v)

    await r.broker.run(apply)
    await r.manager.tick_once()
    return await sim_state(request)


@router.post("/sim/symbol")
async def sim_symbol(request: Request, body: SimSymbolIn):
    r = rt(request)
    _require_sim(r)
    await r.broker.run(lambda b: _sim(b).trade_modes.__setitem__(body.symbol, body.trade_mode))
    return await sim_state(request)


@router.post("/sim/clock")
async def sim_clock(request: Request, body: ClockIn):
    r = rt(request)
    _require_sim(r)
    r.store.update_app_settings({"sim_speed": body.speed})
    return await sim_state(request)


# ================================================================ websocket
@ws_router.websocket("/ws")
async def ws(websocket: WebSocket):
    await websocket.accept()
    r = websocket.app.state.rt
    q = r.bus.subscribe()
    try:
        await websocket.send_json({"type": "snapshot", "data": r.manager.snapshot()})
        while True:
            event = await q.get()
            # collapse bursts of snapshots: only the newest matters
            if event["type"] == "snapshot":
                while not q.empty():
                    nxt = q.get_nowait()
                    if nxt["type"] != "snapshot":
                        await websocket.send_json(nxt)
                    else:
                        event = nxt
            await websocket.send_json(event)
    except (WebSocketDisconnect, asyncio.CancelledError):
        pass
    except Exception:  # pragma: no cover
        log.exception("websocket error")
    finally:
        r.bus.unsubscribe(q)
