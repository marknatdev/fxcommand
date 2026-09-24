"""Pre-flight Check: the conditions checked before a Session starts (pure: facts in, checks out).

Blocking checks must pass before a Broker-mode Session may start on a live Account. On demo
Accounts and for Paper Sessions every check is advisory. ``hedging`` blocks everywhere (ADR 0006).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from ..broker.types import AccountInfo, SymbolInfo, TerminalStatus, Tick
from ..risk.gate import MAX_QUOTE_AGE, RiskProfile, loss_at_stop

MAX_PING_MS = 500.0
MAX_HEARTBEAT_AGE = 5.0


@dataclass(frozen=True)
class Check:
    id: str
    label: str
    status: str  # pass | warn | fail
    detail: str
    blocking: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class SymbolFacts:
    name: str
    info: SymbolInfo | None = None
    tick: Tick | None = None
    error: str = ""
    profile: RiskProfile | None = None
    typical_sl: float = 0.0  # price distance of a typical stop-loss (strategy ATR multiple), 0 = unknown
    margin_min_lot: float | None = None


@dataclass
class Facts:
    connected: bool
    terminal: TerminalStatus | None
    account: AccountInfo | None
    now: int
    live_enabled: bool = False
    floor: dict | None = None
    notifier_configured: bool = False
    db_ok: bool = True
    heartbeat_age: float | None = None
    paper: bool = False
    symbols: list[SymbolFacts] = field(default_factory=list)


def evaluate(f: Facts) -> dict:
    acct = f.account
    live = bool(acct and not acct.is_demo)
    enforce = live and not f.paper  # blocking checks bind only here
    checks: list[Check] = []

    def add(id_: str, label: str, ok: bool | None, detail: str, blocking: bool = True, warn_only: bool = False) -> None:
        status = "pass" if ok else ("warn" if warn_only or ok is None else "fail")
        checks.append(Check(id_, label, status, detail, blocking=blocking and enforce and not warn_only))

    t = f.terminal
    add("terminal", "Terminal connected to the trade server", bool(f.connected and t and t.connected),
        "connected" if f.connected and t and t.connected else "not connected — open MetaTrader 5 and log in")
    if f.paper:
        add("algo_trading", "Algo Trading enabled in the terminal", True, "not needed: Paper Sessions never send orders", blocking=False)
    else:
        add("algo_trading", "Algo Trading enabled in the terminal", bool(t and t.algo_trading),
            "on" if t and t.algo_trading else "press the Algo Trading button in the MT5 toolbar")
        add("account_trading", "Account allows trading by Expert Advisors", bool(acct and acct.trade_allowed),
            "allowed" if acct and acct.trade_allowed else "the account or terminal refuses automated trading")
    hedging = bool(acct and acct.margin_mode == "hedging")
    checks.append(Check("hedging", "Hedging account (one position per order)", "pass" if hedging else "fail",
                        "hedging" if hedging else f"margin mode is {acct.margin_mode if acct else 'unknown'}; FXCommand trades hedging accounts only",
                        blocking=True))
    if t is not None:
        add("ping", "Ping to the trade server", t.ping_ms < MAX_PING_MS, f"{t.ping_ms:.0f} ms (limit {MAX_PING_MS:.0f} ms)")
    if live and not f.paper:
        add("live_enabled", "Live trading enabled for this account", f.live_enabled,
            "live-enabled" if f.live_enabled else "enable live trading on the Account page (typed account number)")
        breached = bool(f.floor and f.floor.get("breached_at"))
        add("equity_floor", "Equity above the Equity Floor", not breached,
            f"floor {f.floor['floor']:.2f}" + (" — BREACHED; reset it on the Risk page" if breached else "") if f.floor else "floor is set when live trading is enabled")

    for s in f.symbols:
        if s.info is None or s.tick is None:
            add(f"symbol:{s.name}", f"{s.name} available", False, s.error or "not offered by the broker")
            continue
        info, tick = s.info, s.tick
        tradable = info.trade_mode == "full"
        add(f"trade_mode:{s.name}", f"{s.name} tradable", tradable if info.trade_mode != "disabled" else False,
            f"trade mode {info.trade_mode}", warn_only=info.trade_mode in ("longonly", "shortonly"))
        age = f.now - tick.time
        add(f"quote:{s.name}", f"{s.name} quote is fresh", age <= MAX_QUOTE_AGE,
            f"last quote {age}s old (limit {MAX_QUOTE_AGE}s)" + ("" if age <= MAX_QUOTE_AGE else " — market closed or feed stalled"))
        if s.profile is not None:
            add(f"spread:{s.name}", f"{s.name} spread within the Risk Profile", tick.spread_points <= s.profile.max_spread_points,
                f"{tick.spread_points} pts (max {s.profile.max_spread_points} pts, profile {s.profile.name})")
        if acct is not None and s.margin_min_lot is not None and not f.paper:
            add(f"margin:{s.name}", f"{s.name} minimum lot fits free margin", s.margin_min_lot <= acct.margin_free,
                f"{info.volume_min} lots need {s.margin_min_lot:.2f} {acct.currency}; free {acct.margin_free:.2f}")
        if acct is not None and s.profile is not None and s.typical_sl > 0 and acct.equity > 0:
            need = loss_at_stop(info.volume_min, s.typical_sl, info)
            pct = need / acct.equity * 100
            ok = pct <= s.profile.risk_pct
            detail = f"minimum lot at a typical stop risks {need:.2f} {acct.currency} = {pct:.2f}% (profile {s.profile.risk_pct}%)"
            if not ok:
                detail += "; most signals will be rejected for size" + (
                    " unless within the minimum-lot allowance" if s.profile.allow_min_lot else ""
                )
            add(f"min_lot:{s.name}", f"{s.name} risk % can buy the minimum lot", ok, detail, warn_only=True)

    add("notifier", "Telegram alerts configured", f.notifier_configured,
        "configured" if f.notifier_configured else "set a bot token and chat id on the Settings page", warn_only=True)
    add("database", "Database writable", f.db_ok, "ok" if f.db_ok else "cannot write the database")
    hb = f.heartbeat_age
    add("heartbeat", "Engine loop running", hb is not None and hb <= MAX_HEARTBEAT_AGE,
        "no engine pass yet" if hb is None else f"last pass {hb:.1f}s ago (limit {MAX_HEARTBEAT_AGE:.0f}s)")

    failed = [c for c in checks if c.blocking and c.status == "fail"]
    return {"ok": not failed, "live": live, "paper": f.paper, "enforced": enforce, "checks": [c.to_dict() for c in checks],
            "failed": [c.label for c in failed]}
