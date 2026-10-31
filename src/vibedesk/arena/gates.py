"""Demo-ready badge evaluation, live gate calculation, and owner overrides."""

from __future__ import annotations

from typing import Any

# Live gate trade requirements per timeframe (spec §3.4, ADR 0009)
LIVE_GATE_MIN_TRADES: dict[str, int] = {
    "M15": 100,
    "H1": 60,
    "H4": 40,
    "D1": 30,
}


def check_demo_ready(metrics: dict[str, Any]) -> tuple[bool, str | None]:
    """Evaluate automated criteria for the Demo-Ready badge (spec §3.2, ADR 0010).

    Requires:
    1. Active duration >= 4 weeks (28 days).
    2. Trade count >= 30.
    3. Profit Factor >= 1.10.
    4. Maximum Peak-to-Trough Drawdown < 6.0%.
    5. No decay flag.

    Returns (is_ready, failure_reason).
    """
    days_active = metrics.get("days_active", 0)
    trades_count = metrics.get("trades_count", 0)
    pf = metrics.get("profit_factor", 0.0)
    max_dd = metrics.get("max_drawdown", 1.0)
    decay_flag = metrics.get("decay_flag", False)

    if days_active < 28:
        return False, f"Duration {days_active} days < 28 days (4 weeks)"
    if trades_count < 30:
        return False, f"Trade count {trades_count} < 30"
    if pf < 1.10:
        return False, f"Profit Factor {pf:.2f} < 1.10"
    if max_dd >= 0.06:
        return False, f"Drawdown {max_dd * 100:.1f}% >= 6.0%"
    if decay_flag:
        return False, "Performance decay detected against Gauntlet distribution"

    return True, None


def evaluate_live_gate(
    metrics: dict[str, Any],
    timeframe: str,
) -> tuple[bool, dict[str, Any]]:
    """Compute Live Gate readiness (spec §3.4, ADR 0009).

    Requires:
    1. Trade count meets timeframe threshold (M15: 100, H1: 60, H4: 40, D1: 30).
    2. Bootstrap 95% 1-sided lower confidence bound of Profit Factor > 1.00.
    3. Maximum Drawdown < 6.0%.

    Computed and displayed for observation only; no orders are placed.
    """
    min_trades = LIVE_GATE_MIN_TRADES.get(timeframe.upper(), 50)
    trades_count = metrics.get("trades_count", 0)
    pf_lower_bound = metrics.get("pf_lower_bound", 0.0)
    max_dd = metrics.get("max_drawdown", 1.0)

    checks = {
        "trades_count": {
            "required": min_trades,
            "actual": trades_count,
            "passed": trades_count >= min_trades,
        },
        "pf_lower_bound": {
            "required": 1.00,
            "actual": pf_lower_bound,
            "passed": pf_lower_bound > 1.00,
        },
        "max_drawdown": {
            "required": 0.06,
            "actual": max_dd,
            "passed": max_dd < 0.06,
        },
    }

    all_passed = all(c["passed"] for c in checks.values())
    return all_passed, checks


def record_override(
    ledger: Any,
    strategy_id: int,
    gate_type: str,
    reason: str,
) -> dict[str, Any]:
    """Record an owner override for Demo-Ready or Live Gate in the Ledger (ADR 0010)."""
    clean_reason = reason.strip()
    if not clean_reason:
        raise ValueError("A non-empty reason is required for owner overrides")
    if gate_type not in ("demo_ready", "live_gate"):
        raise ValueError(f"Unknown gate_type: {gate_type}")

    record = {"strategy_id": strategy_id, "gate_type": gate_type, "reason": clean_reason}
    if ledger is not None and hasattr(ledger, "record_arena_override"):
        ledger.record_arena_override(strategy_id, gate_type, clean_reason)
    elif ledger is not None and hasattr(ledger, "_db"):
        try:
            with ledger._lock:
                ledger._db.execute(
                    "INSERT INTO arena_overrides (strategy_id, gate_type, reason) VALUES (?, ?, ?)",
                    (strategy_id, gate_type, clean_reason),
                )
                ledger._db.commit()
        except Exception:
            pass
    return record
