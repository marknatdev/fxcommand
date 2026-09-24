"""Mt5Broker: adapter onto the real MetaTrader 5 terminal via the ``MetaTrader5`` package.

The package keeps process-global, blocking, non-thread-safe state, so this
adapter must only ever be driven through ``BrokerThread`` (one thread).

It attaches to the terminal that is already running and logged in; it never
stores or sends credentials. See docs/adr/0001 and 0002.
"""

from __future__ import annotations

import time

import pandas as pd

from .base import BrokerError
from .types import (
    AccountInfo,
    ClosedTrade,
    OrderResult,
    Position,
    Side,
    SymbolInfo,
    TerminalStatus,
    Tick,
    Timeframe,
    empty_bars,
)

try:  # the package only exists on Windows
    import MetaTrader5 as mt5
except ImportError:  # pragma: no cover
    mt5 = None

ACCOUNT_TRADE_MODE_REAL = 2
SYMBOL_FILLING_FOK = 1
SYMBOL_FILLING_IOC = 2
MARGIN_MODES = {0: "netting", 1: "exchange", 2: "hedging"}
SYMBOL_TRADE_MODES = {0: "disabled", 1: "longonly", 2: "shortonly", 3: "closeonly", 4: "full"}

# Order Outcomes (ADR 0007), by MT5 retcode
RET_FILLED = {10008, 10009, 10010}  # PLACED, DONE, DONE_PARTIAL
RET_UNCERTAIN = {10011, 10012, 10031}  # ERROR (request processing), TIMEOUT, CONNECTION


def _tf(timeframe: Timeframe) -> int:
    return {
        Timeframe.M1: mt5.TIMEFRAME_M1,
        Timeframe.M5: mt5.TIMEFRAME_M5,
        Timeframe.M15: mt5.TIMEFRAME_M15,
        Timeframe.M30: mt5.TIMEFRAME_M30,
        Timeframe.H1: mt5.TIMEFRAME_H1,
        Timeframe.H4: mt5.TIMEFRAME_H4,
        Timeframe.D1: mt5.TIMEFRAME_D1,
    }[Timeframe(timeframe)]


class Mt5Broker:
    mode = "mt5"

    def __init__(self, terminal_path: str | None = None, deviation_points: int = 20):
        if mt5 is None:
            raise BrokerError("MetaTrader5 package is not installed (Windows only)")
        self._path = terminal_path
        self._deviation = deviation_points
        self._connected = False
        self._last_server_time = 0

    # --------------------------------------------------------------- lifecycle
    def connect(self) -> None:
        ok = mt5.initialize(path=self._path) if self._path else mt5.initialize()
        if not ok:
            raise BrokerError(f"MT5 initialize failed: {mt5.last_error()}")
        if mt5.account_info() is None:
            raise BrokerError("MT5 terminal is running but not logged into an account")
        self._connected = True

    def disconnect(self) -> None:
        if self._connected:
            mt5.shutdown()
        self._connected = False

    def is_connected(self) -> bool:
        if not self._connected:
            return False
        info = mt5.terminal_info()
        return bool(info and info.connected)

    def _need(self, value, what: str):
        if value is None:
            raise BrokerError(f"{what} failed: {mt5.last_error()}")
        return value

    def server_time(self) -> int:
        """Latest tick time across Market Watch symbols (MT5 has no direct server clock)."""
        best = self._last_server_time
        for sym in self._market_watch()[:10]:
            t = mt5.symbol_info_tick(sym)
            if t is not None and t.time > best:
                best = int(t.time)
        if best == 0:
            best = int(time.time())
        self._last_server_time = best
        return best

    def _market_watch(self) -> list[str]:
        return [s.name for s in (mt5.symbols_get() or []) if s.visible]

    # ----------------------------------------------------------------- queries
    def account(self) -> AccountInfo:
        a = self._need(mt5.account_info(), "account_info")
        term = mt5.terminal_info()
        algo_on = bool(term and term.trade_allowed)  # the terminal's "Algo Trading" button
        return AccountInfo(
            login=int(a.login),
            name=a.name,
            server=a.server,
            company=a.company,
            currency=a.currency,
            balance=float(a.balance),
            equity=float(a.equity),
            margin=float(a.margin),
            margin_free=float(a.margin_free),
            leverage=int(a.leverage),
            is_demo=a.trade_mode != ACCOUNT_TRADE_MODE_REAL,
            trade_allowed=bool(a.trade_allowed and a.trade_expert and algo_on),
            margin_mode=MARGIN_MODES.get(int(a.margin_mode), "unknown"),
        )

    def terminal(self) -> TerminalStatus:
        t = self._need(mt5.terminal_info(), "terminal_info")
        return TerminalStatus(
            connected=bool(t.connected),
            algo_trading=bool(t.trade_allowed) and not bool(getattr(t, "tradeapi_disabled", False)),
            ping_ms=round(float(t.ping_last) / 1000.0, 1),
            build=int(t.build),
            name=str(t.name),
        )

    def symbols(self) -> list[str]:
        return self._market_watch()

    def symbol_info(self, symbol: str) -> SymbolInfo:
        if not mt5.symbol_select(symbol, True):
            raise BrokerError(f"symbol {symbol!r} not available: {mt5.last_error()}")
        s = self._need(mt5.symbol_info(symbol), f"symbol_info({symbol})")
        return SymbolInfo(
            name=s.name,
            description=s.description,
            digits=int(s.digits),
            point=float(s.point),
            trade_tick_size=float(s.trade_tick_size),
            trade_tick_value=float(s.trade_tick_value),
            contract_size=float(s.trade_contract_size),
            volume_min=float(s.volume_min),
            volume_max=float(s.volume_max),
            volume_step=float(s.volume_step),
            stops_level=int(s.trade_stops_level),
            filling_mode=int(s.filling_mode),
            trade_mode=SYMBOL_TRADE_MODES.get(int(s.trade_mode), "disabled"),
            freeze_level=int(s.trade_freeze_level),
        )

    def tick(self, symbol: str) -> Tick:
        mt5.symbol_select(symbol, True)
        t = self._need(mt5.symbol_info_tick(symbol), f"symbol_info_tick({symbol})")
        info = self._need(mt5.symbol_info(symbol), f"symbol_info({symbol})")
        return Tick(symbol=symbol, time=int(t.time), bid=float(t.bid), ask=float(t.ask), point=float(info.point))

    def closed_bars(self, symbol: str, timeframe: Timeframe, count: int) -> pd.DataFrame:
        # start_pos=1 skips index 0, the bar that is still forming
        rates = mt5.copy_rates_from_pos(symbol, _tf(timeframe), 1, count)
        if rates is None or len(rates) == 0:
            return empty_bars()
        df = pd.DataFrame(rates)
        return pd.DataFrame(
            {
                "time": df["time"].astype("int64"),
                "open": df["open"],
                "high": df["high"],
                "low": df["low"],
                "close": df["close"],
                "volume": df["tick_volume"].astype(float),
            }
        )

    def positions(self, magic: int | None = None) -> list[Position]:
        out = []
        for p in mt5.positions_get() or []:
            if magic is not None and p.magic != magic:
                continue
            out.append(
                Position(
                    ticket=int(p.ticket),
                    symbol=p.symbol,
                    side="long" if p.type == mt5.POSITION_TYPE_BUY else "short",
                    volume=float(p.volume),
                    price_open=float(p.price_open),
                    price_current=float(p.price_current),
                    sl=float(p.sl),
                    tp=float(p.tp),
                    profit=float(p.profit + p.swap),
                    magic=int(p.magic),
                    time=int(p.time),
                    comment=p.comment,
                )
            )
        return out

    def history(self, since: int) -> list[ClosedTrade]:
        # deal times are broker server time (XM: UTC+3); query with a day of margin either side,
        # then filter precisely on the deal times themselves
        deals = mt5.history_deals_get(since - 86400, int(time.time()) + 7 * 86400)
        if deals is None:
            return []
        opens: dict[int, object] = {}
        closes: dict[int, list] = {}
        for d in deals:
            if d.entry == mt5.DEAL_ENTRY_IN:
                opens[d.position_id] = d
            elif d.entry in (mt5.DEAL_ENTRY_OUT, mt5.DEAL_ENTRY_OUT_BY):
                closes.setdefault(d.position_id, []).append(d)
        out = []
        for pid, outs in closes.items():
            if mt5.positions_get(ticket=pid):
                continue  # partially closed, still open
            last = max(outs, key=lambda d: d.time)
            if last.time < since:
                continue
            first = opens.get(pid)
            if first is None:
                hist = mt5.history_deals_get(position=pid) or []
                first = next((d for d in hist if d.entry == mt5.DEAL_ENTRY_IN), None)
            profit = sum(d.profit + d.swap + d.commission for d in outs)
            if first is not None:
                profit += first.commission
            reason = {mt5.DEAL_REASON_SL: "sl", mt5.DEAL_REASON_TP: "tp", mt5.DEAL_REASON_EXPERT: "expert"}.get(
                last.reason, "manual" if last.reason in (mt5.DEAL_REASON_CLIENT, mt5.DEAL_REASON_MOBILE, mt5.DEAL_REASON_WEB) else "other"
            )
            out.append(
                ClosedTrade(
                    ticket=int(pid),
                    symbol=last.symbol,
                    side="long" if (first.type if first else 1 - last.type) == mt5.DEAL_TYPE_BUY else "short",
                    volume=float(sum(d.volume for d in outs)),
                    price_open=float(first.price) if first else 0.0,
                    price_close=float(last.price),
                    profit=round(float(profit), 2),
                    magic=int(last.magic),
                    time_open=int(first.time) if first else 0,
                    time_close=int(last.time),
                    reason=reason,
                )
            )
        return out

    # ----------------------------------------------------------------- orders
    def _filling(self, symbol: str) -> int:
        info = mt5.symbol_info(symbol)
        mode = int(info.filling_mode) if info else 0
        if mode & SYMBOL_FILLING_FOK:
            return mt5.ORDER_FILLING_FOK
        if mode & SYMBOL_FILLING_IOC:
            return mt5.ORDER_FILLING_IOC
        return mt5.ORDER_FILLING_RETURN

    def _send(self, request: dict) -> OrderResult:
        """order_send + Order Outcome classification. Never retries (the engine decides)."""
        r = mt5.order_send(request)
        if r is None:  # no reply at all: the request may or may not have reached the server
            return OrderResult(False, -1, f"order_send returned nothing: {mt5.last_error()}", uncertain=True)
        code = int(r.retcode)
        if code in RET_FILLED:
            return OrderResult(
                True,
                code,
                r.comment or "done",
                ticket=int(r.order) or None,
                price=float(r.price) or None,
                volume=float(r.volume) or None,
                extra={"deal": int(r.deal), "order": int(r.order)},
            )
        return OrderResult(False, code, r.comment or "rejected", uncertain=code in RET_UNCERTAIN)

    def _position_of(self, res: OrderResult):
        """The position an entry opened. On hedging accounts its ticket is the order ticket; otherwise
        follow the deal to its position id."""
        order, deal = res.extra.get("order"), res.extra.get("deal")
        if order:
            found = mt5.positions_get(ticket=order)
            if found:
                return found[0]
        if deal:
            deals = mt5.history_deals_get(ticket=deal) or []
            for d in deals:
                found = mt5.positions_get(ticket=d.position_id)
                if found:
                    return found[0]
        return None

    def market_order(
        self, symbol: str, side: Side, volume: float, sl: float, tp: float, magic: int, comment: str = ""
    ) -> OrderResult:
        if not sl:
            return OrderResult(False, -1, "refused: every order must carry a stop-loss")
        mt5.symbol_select(symbol, True)
        t = self._need(mt5.symbol_info_tick(symbol), "symbol_info_tick")
        buy = side == "long"
        price = t.ask if buy else t.bid
        res = self._send(
            {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": symbol,
                "volume": float(volume),
                "type": mt5.ORDER_TYPE_BUY if buy else mt5.ORDER_TYPE_SELL,
                "price": price,
                "sl": float(sl),
                "tp": float(tp or 0.0),
                "deviation": self._deviation,
                "magic": int(magic),
                "comment": comment[:31],
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": self._filling(symbol),
            }
        )
        if not res.ok:
            return res
        # the reply's price/ticket are not trustworthy on every server (0 on market execution):
        # report what the position itself says
        pos = self._position_of(res)
        if pos is not None:
            return OrderResult(
                True, res.retcode, res.message, ticket=int(pos.ticket), price=float(pos.price_open), volume=float(pos.volume), extra=res.extra
            )
        # filled but not visible yet (or already stopped out): best available facts
        return OrderResult(
            True, res.retcode, res.message, ticket=res.ticket, price=res.price or float(price), volume=res.volume or float(volume), extra=res.extra
        )

    def margin_required(self, symbol: str, side: Side, volume: float) -> float:
        mt5.symbol_select(symbol, True)
        t = self._need(mt5.symbol_info_tick(symbol), "symbol_info_tick")
        buy = side == "long"
        m = mt5.order_calc_margin(mt5.ORDER_TYPE_BUY if buy else mt5.ORDER_TYPE_SELL, symbol, float(volume), t.ask if buy else t.bid)
        return float(self._need(m, f"order_calc_margin({symbol})"))

    def modify(self, ticket: int, sl: float, tp: float) -> OrderResult:
        pos = mt5.positions_get(ticket=ticket)
        if not pos:
            return OrderResult(False, -1, f"position {ticket} not found")
        return self._send(
            {"action": mt5.TRADE_ACTION_SLTP, "position": int(ticket), "symbol": pos[0].symbol, "sl": float(sl), "tp": float(tp or 0.0)}
        )

    def close(self, ticket: int, comment: str = "") -> OrderResult:
        pos = mt5.positions_get(ticket=ticket)
        if not pos:
            return OrderResult(False, -1, f"position {ticket} not found")
        p = pos[0]
        t = self._need(mt5.symbol_info_tick(p.symbol), "symbol_info_tick")
        buy = p.type == mt5.POSITION_TYPE_BUY
        return self._send(
            {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": p.symbol,
                "volume": float(p.volume),
                "type": mt5.ORDER_TYPE_SELL if buy else mt5.ORDER_TYPE_BUY,
                "position": int(ticket),
                "price": t.bid if buy else t.ask,
                "deviation": self._deviation,
                "magic": int(p.magic),
                "comment": (comment or "fxcommand close")[:31],
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": self._filling(p.symbol),
            }
        )
