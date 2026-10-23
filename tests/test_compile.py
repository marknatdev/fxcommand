"""Compiling a Combination into the per-bar Signals the engine trades."""

import numpy as np
from pydantic import BaseModel, ConfigDict

from vibedesk.blocks.compile import compile_signals
from vibedesk.blocks.registry import BlockRegistry, Kind
from vibedesk.blocks.spec import expand_grid, validate
from vibedesk.market.bars import Bars, Timeframe

# What a test-only entry Block fires on six bars: bars 1 and 3 fire both ways.
LONG = np.array([True, True, False, True, False, False])
SHORT = np.array([False, True, True, True, False, True])


class NoParams(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)  # frozen: BlockCache hashes params


def registry() -> BlockRegistry:
    reg = BlockRegistry()
    reg.register("both_ways", Kind.ENTRY, NoParams)(lambda bars, params: (LONG, SHORT))
    reg.register("fixed_stop", Kind.STOP, NoParams)(lambda bars, params: np.full(len(bars), 1.0))
    reg.register("no_sizing", Kind.SIZING, NoParams)(lambda bars, params: None)
    return reg


def test_an_entry_that_fires_both_ways_on_a_bar_enters_neither_way_there():
    # Long-only bar 0 and short-only bars 2 and 5 stay; bars 1 and 3 say nothing and go.
    # The stop is 1.0 everywhere, so no bar is dropped for want of a stop.
    n = len(LONG)
    flat = np.full(n, 100.0)
    bars = Bars(symbol="GOLD", timeframe=Timeframe.M15, point=0.01, digits=2,
                time=900 * np.arange(n), open=flat, high=flat + 1, low=flat - 1, close=flat,
                spread=np.zeros(n))  # fmt: skip
    reg = registry()
    spec = {
        "name": "both-ways",
        "symbol": "GOLD",
        "timeframe": "M15",
        "direction": "both",
        "entry": {"block": "both_ways", "params": {}},
        "exit": {"stop": {"block": "fixed_stop", "params": {}}},
        "sizing": {"block": "no_sizing", "params": {}},
    }
    [combination] = expand_grid(validate(spec, registry=reg), registry=reg)

    signals = compile_signals(combination, bars, registry=reg)

    assert not (signals.long_entry & signals.short_entry).any()
    assert signals.long_entry.tolist() == [True, False, False, False, False, False]
    assert signals.short_entry.tolist() == [False, False, True, False, False, True]
