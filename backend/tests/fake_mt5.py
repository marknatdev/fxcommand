"""A scriptable stand-in for the ``MetaTrader5`` package, so the real ``Mt5Broker`` adapter's order
paths can be tested without a terminal and without ever sending a real order.

Only what ``Mt5Broker`` uses is implemented. ``order_send`` answers from ``script`` (a list of
behaviours consumed in order) and otherwise fills normally.
"""

from __future__ import annotations

from types import SimpleNamespace as NS

import numpy as np

# --- constants (values as in the real package)
TIMEFRAME_M1, TIMEFRAME_M5, TIMEFRAME_M15, TIMEFRAME_M30 = 1, 5, 15, 30
TIMEFRAME_H1, TIMEFRAME_H4, TIMEFRAME_D1 = 16385, 16388, 16408
ORDER_TYPE_BUY, ORDER_TYPE_SELL = 0, 1
POSITION_TYPE_BUY, POSITION_TYPE_SELL = 0, 1
TRADE_ACTION_DEAL, TRADE_ACTION_SLTP = 1, 6
ORDER_FILLING_FOK, ORDER_FILLING_IOC, ORDER_FILLING_RETURN = 0, 1, 2
ORDER_TIME_GTC = 0
DEAL_TYPE_BUY, DEAL_TYPE_SELL = 0, 1
DEAL_ENTRY_IN, DEAL_ENTRY_OUT, DEAL_ENTRY_OUT_BY = 0, 1, 3
DEAL_REASON_CLIENT, DEAL_REASON_MOBILE, DEAL_REASON_WEB, DEAL_REASON_EXPERT, DEAL_REASON_SL, DEAL_REASON_TP = 0, 1, 2, 3, 4, 5
TRADE_RETCODE_REQUOTE, TRADE_RETCODE_PLACED, TRADE_RETCODE_DONE, TRADE_RETCODE_DONE_PARTIAL = 10004, 10008, 10009, 10010
TRADE_RETCODE_ERROR, TRADE_RETCODE_TIMEOUT, TRADE_RETCODE_INVALID_STOPS = 10011, 10012, 10016
TRADE_RETCODE_MARKET_CLOSED, TRADE_RETCODE_NO_MONEY, TRADE_RETCODE_PRICE_OFF = 10018, 10019, 10021
TRADE_RETCODE_CLIENT_DISABLES_AT, TRADE_RETCODE_CONNECTION = 10027, 10031


class FakeMt5:
    def __init__(self, margin_mode: int = 2, trade_mode: int = 0):
        self.now = 1_704_700_800
        self.bid, self.ask = 1.08500, 1.08512
        self.account = NS(login=12345678, name="Fake", server="Fake-Server", company="Fake", currency="USD", balance=1000.0,
                          equity=1000.0, margin=0.0, margin_free=1000.0, leverage=500, trade_mode=trade_mode, trade_allowed=True,
                          trade_expert=True, margin_mode=margin_mode)
        self.term = NS(connected=True, trade_allowed=True, tradeapi_disabled=False, ping_last=42_000, build=6182, name="Fake Terminal")
        self.positions: dict[int, NS] = {}
        self.deals: list[NS] = []
        self.requests: list[dict] = []
        self.script: list[str] = []
        self.next_ticket = 5_000_001
        self.symbol_trade_mode = 4
        self.filling = 2  # IOC only, as on XM

    # -- lifecycle
    def initialize(self, path=None):
        return True

    def shutdown(self):
        return None

    def last_error(self):
        return (1, "fake")

    def version(self):
        return (500, 6182, "fake")

    def terminal_info(self):
        return self.term

    def account_info(self):
        return self.account

    # -- symbols
    def symbols_get(self):
        return [NS(name="EURUSD", visible=True)]

    def symbol_select(self, sym, enable=True):
        return sym == "EURUSD"

    def symbol_info(self, sym):
        if sym != "EURUSD":
            return None
        return NS(name="EURUSD", description="Euro", digits=5, point=1e-5, trade_tick_size=1e-5, trade_tick_value=1.0,
                  trade_contract_size=100_000, volume_min=0.01, volume_max=50.0, volume_step=0.01, trade_stops_level=0,
                  trade_freeze_level=3, filling_mode=self.filling, trade_mode=self.symbol_trade_mode, spread=12)

    def symbol_info_tick(self, sym):
        return NS(time=self.now, bid=self.bid, ask=self.ask) if sym == "EURUSD" else None

    def copy_rates_from_pos(self, sym, tf, start, count):
        n = count
        t = self.now - 60 * (np.arange(n)[::-1] + 1)
        a = np.zeros(n, dtype=[("time", "i8"), ("open", "f8"), ("high", "f8"), ("low", "f8"), ("close", "f8"), ("tick_volume", "i8")])
        a["time"], a["open"], a["high"], a["low"], a["close"], a["tick_volume"] = t, self.bid, self.bid + 1e-4, self.bid - 1e-4, self.bid, 10
        return a

    # -- positions / history
    def positions_get(self, ticket=None):
        if ticket is not None:
            p = self.positions.get(ticket)
            return (p,) if p else ()
        return tuple(self.positions.values())

    def history_deals_get(self, *args, ticket=None, position=None):
        if ticket is not None:
            return tuple(d for d in self.deals if d.ticket == ticket)
        if position is not None:
            return tuple(d for d in self.deals if d.position_id == position)
        return tuple(self.deals)

    def order_calc_margin(self, type_, sym, volume, price):
        return round(volume * 100_000 * price / self.account.leverage, 2)

    def order_calc_profit(self, type_, sym, volume, open_, close):
        return round((close - open_) * (1 if type_ == ORDER_TYPE_BUY else -1) * volume * 100_000, 2)

    # -- trading
    def _result(self, retcode, order=0, deal=0, price=0.0, volume=0.0, comment=""):
        return NS(retcode=retcode, order=order, deal=deal, price=price, volume=volume, comment=comment)

    def _open(self, req, volume):
        ticket = self.next_ticket
        self.next_ticket += 2
        buy = req["type"] == ORDER_TYPE_BUY
        price = self.ask if buy else self.bid
        self.positions[ticket] = NS(ticket=ticket, symbol=req["symbol"], type=POSITION_TYPE_BUY if buy else POSITION_TYPE_SELL, volume=volume,
                                    price_open=price, price_current=price, sl=req["sl"], tp=req["tp"], profit=0.0, swap=0.0,
                                    magic=req["magic"], time=self.now, comment=req["comment"])
        deal = ticket + 1
        self.deals.append(NS(ticket=deal, position_id=ticket, entry=DEAL_ENTRY_IN, type=DEAL_TYPE_BUY if buy else DEAL_TYPE_SELL,
                             price=price, volume=volume, time=self.now, magic=req["magic"], symbol=req["symbol"], profit=0.0, swap=0.0,
                             commission=-0.07, reason=DEAL_REASON_EXPERT))
        return ticket, deal, price

    def order_send(self, req):
        self.requests.append(dict(req))
        what = self.script.pop(0) if self.script else "fill"
        if what == "none":
            return None
        codes = {"requote": TRADE_RETCODE_REQUOTE, "timeout": TRADE_RETCODE_TIMEOUT, "connection": TRADE_RETCODE_CONNECTION,
                 "error": TRADE_RETCODE_ERROR, "no_money": TRADE_RETCODE_NO_MONEY, "market_closed": TRADE_RETCODE_MARKET_CLOSED,
                 "algo_off": TRADE_RETCODE_CLIENT_DISABLES_AT, "invalid_stops": TRADE_RETCODE_INVALID_STOPS}
        if what in codes:
            return self._result(codes[what], comment=what)
        if req["action"] == TRADE_ACTION_SLTP:
            p = self.positions[req["position"]]
            p.sl, p.tp = req["sl"], req["tp"]
            return self._result(TRADE_RETCODE_DONE)
        if "position" in req:  # close
            p = self.positions.pop(req["position"])
            self.deals.append(NS(ticket=self.next_ticket, position_id=p.ticket, entry=DEAL_ENTRY_OUT, type=1 - p.type, price=req["price"],
                                 volume=p.volume, time=self.now + 60, magic=p.magic, symbol=p.symbol, profit=1.5, swap=0.0,
                                 commission=-0.07, reason=DEAL_REASON_EXPERT))
            self.next_ticket += 1
            return self._result(TRADE_RETCODE_DONE, order=self.next_ticket, price=req["price"], volume=p.volume)
        volume = req["volume"]
        if what == "partial":
            volume = round(volume / 2, 2)
        ticket, deal, price = self._open(req, volume)
        if what == "price_zero":
            return self._result(TRADE_RETCODE_DONE, order=ticket, deal=deal, price=0.0, volume=0.0)
        if what == "order_differs":  # order ticket != position ticket (netting-style numbering)
            return self._result(TRADE_RETCODE_DONE, order=ticket + 999, deal=deal, price=price, volume=volume)
        if what == "timeout_filled":
            return self._result(TRADE_RETCODE_TIMEOUT, comment="timeout")
        code = TRADE_RETCODE_DONE_PARTIAL if what == "partial" else TRADE_RETCODE_DONE
        return self._result(code, order=ticket, deal=deal, price=price, volume=volume)
