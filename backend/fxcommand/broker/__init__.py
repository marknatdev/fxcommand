from .base import Broker, BrokerError, BrokerThread
from .types import AccountInfo, ClosedTrade, OrderResult, Position, Side, SymbolInfo, Tick, Timeframe


def make_broker(mode: str, **kwargs) -> Broker:
    """Build the adapter for ``BROKER=sim|mt5``."""
    if mode == "sim":
        from .sim import SimBroker

        return SimBroker(**kwargs)
    if mode == "mt5":
        from .mt5 import Mt5Broker

        return Mt5Broker(**kwargs)
    raise ValueError(f"unknown broker mode {mode!r} (expected 'sim' or 'mt5')")


__all__ = [
    "AccountInfo",
    "Broker",
    "BrokerError",
    "BrokerThread",
    "ClosedTrade",
    "OrderResult",
    "Position",
    "Side",
    "SymbolInfo",
    "Tick",
    "Timeframe",
    "make_broker",
]
