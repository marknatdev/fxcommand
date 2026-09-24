"""MCP server (stdio) exposing FXCommand engine control to AI agents.

It is a thin client of the running app's HTTP API (``FXC_URL``, default
http://127.0.0.1:8000), so it obeys exactly the same rules as the dashboard.
Deliberately absent: any tool that enables live trading, edits risk limits, or promotes/rolls back a Champion.
"""

from __future__ import annotations

import os
from typing import Any

import httpx
from mcp.server.mcpserver import MCPServer

BASE = os.environ.get("FXC_URL", "http://127.0.0.1:8000").rstrip("/")
mcp = MCPServer("fxcommand")


def _call(method: str, path: str, body: dict | None = None, params: dict | None = None) -> Any:
    try:
        r = httpx.request(method, f"{BASE}/api{path}", json=body, params=params, timeout=30)
    except httpx.HTTPError as e:
        return {"error": f"FXCommand is not reachable at {BASE}: {e}"}
    if r.status_code >= 400:
        try:
            return {"error": r.json()}
        except ValueError:
            return {"error": r.text}
    return r.json() if r.content else {"ok": True}


def _slim_session(s: dict) -> dict:
    return {
        "id": s["id"],
        "name": s["name"],
        "status": s["status"],
        "magic": s["magic"],
        "stop_reason": s.get("stop_reason"),
        "day_pnl": s.get("day_pnl"),
        "open_positions": s.get("open_positions"),
        "assignments": [f"{a['symbol']} {a['timeframe']} {a['strategy']}" for a in s.get("assignments", [])],
        "stats": s.get("stats"),
    }


@mcp.tool()
def get_overview() -> dict:
    """Account, broker mode, connection, today's P&L, sessions summary, open positions and recent alerts."""
    o = _call("GET", "/overview")
    if "error" in o:
        return o
    return {
        k: o[k]
        for k in ("mode", "connected", "server_time", "account", "day_pnl", "day_start_equity", "sessions", "positions", "alerts", "today", "all_time")
    }


@mcp.tool()
def list_sessions() -> list[dict] | dict:
    """All sessions with status, assignments (symbol/timeframe/strategy) and performance stats."""
    r = _call("GET", "/sessions")
    return r if isinstance(r, dict) else [_slim_session(s) for s in r]


@mcp.tool()
def start_session(session_id: int) -> dict:
    """Start (or restart an interrupted) session. Refused on a live account unless the operator enabled live trading."""
    r = _call("POST", f"/sessions/{session_id}/start")
    return r if "error" in r else _slim_session(r)


@mcp.tool()
def pause_session(session_id: int) -> dict:
    """Pause a running session: no new entries, existing positions are still managed."""
    r = _call("POST", f"/sessions/{session_id}/pause")
    return r if "error" in r else _slim_session(r)


@mcp.tool()
def resume_session(session_id: int) -> dict:
    """Resume a paused session."""
    r = _call("POST", f"/sessions/{session_id}/resume")
    return r if "error" in r else _slim_session(r)


@mcp.tool()
def stop_session(session_id: int, close_positions: bool = False) -> dict:
    """Stop a session. close_positions=False leaves its positions open under their server-side SL/TP."""
    r = _call("POST", f"/sessions/{session_id}/stop", {"close_positions": close_positions})
    return r if "error" in r else _slim_session(r)


@mcp.tool()
def kill_switch() -> dict:
    """EMERGENCY: stop every session and close every position opened by FXCommand (foreign positions untouched)."""
    return _call("POST", "/kill-switch")


@mcp.tool()
def list_positions(owned_only: bool = True) -> list[dict] | dict:
    """Open positions on the account; owned_only limits to positions opened by FXCommand sessions."""
    r = _call("GET", "/positions")
    if isinstance(r, dict):
        return r
    return [p for p in r if p["owned"]] if owned_only else r


@mcp.tool()
def get_journal(session_id: int | None = None, alerts_only: bool = False, limit: int = 50) -> list[dict] | dict:
    """Journal entries (signals, risk decisions, orders, closes, alerts), newest first."""
    params: dict[str, Any] = {"limit": max(1, min(limit, 500))}
    if session_id is not None:
        params["session_id"] = session_id
    if alerts_only:
        params["alerts"] = "true"
    r = _call("GET", "/journal", params=params)
    if isinstance(r, dict):
        return r
    return [{k: j[k] for k in ("id", "ts", "session_id", "symbol", "kind", "level", "message")} for j in r]


@mcp.tool()
def get_learning_status() -> dict:
    """Self-improvement status per Arena (symbol + timeframe): Champion, Challengers with Shadow Trade
    results and Guardrail checks, Signal Filter mode, last Optimizer Run and pending Promotions."""
    o = _call("GET", "/learning")
    if "error" in o:
        return o
    arenas = []
    for a in o["arenas"]:
        arenas.append(
            {
                "arena": f"{a['symbol']} {a['timeframe']}",
                "session": a["session"],
                "champion": {"label": a["champion"]["label"], "shadow": a["champion"]["shadow"]},
                "challengers": [
                    {
                        "id": c["id"],
                        "label": c["candidate"]["label"],
                        "shadow_trades": c["shadow"]["n"],
                        "shadow_mean_r": round(c["shadow"]["mean"], 3),
                        "promotable": c["promotable"],
                        "failed_checks": [k["detail"] for k in c["checks"] if not k["ok"]],
                    }
                    for c in a["challengers"]
                ],
                "filter": {"mode": a["filter"]["mode"], "note": a["filter"]["note"]},
                "last_run": a["last_run"],
                "pending": a["pending"],
                "auto_promote": a["auto_promote"],
            }
        )
    return {"enabled": o["enabled"], "arenas": arenas, "queue": o["queue"]}


@mcp.tool()
def run_learning() -> dict:
    """Queue an Optimizer Run for every Arena of every running/paused Session. Promotions still need
    the dashboard (or all Guardrails passing with auto-promotion on a demo account)."""
    return _call("POST", "/learning/run")


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
