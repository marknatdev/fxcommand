"""Compile one Combination of a Strategy Spec into the per-bar Signals the engine trades."""

from collections.abc import Callable, Hashable
from typing import Any

import numpy as np

from vibedesk.blocks.registry import LIBRARY, BlockRegistry
from vibedesk.blocks.spec import Combination
from vibedesk.engine.simulate import Signals
from vibedesk.market.bars import Bars


class BlockCache:
    """Block outputs for one set of bars, so Combinations that share a Block compute it once.

    A grid of 48 Combinations may share one slow filter; with a cache it runs once. Outputs are
    keyed by the Block and its params, and a target also by the stop it is measured from. They
    are stored read-only because every Combination that uses them gets the same arrays.
    """

    def __init__(self, bars: Bars) -> None:
        self.bars = bars
        self._outputs: dict[Hashable, Any] = {}

    def get(self, key: Hashable, compute: Callable[[], Any]) -> Any:
        if key not in self._outputs:
            self._outputs[key] = _read_only(compute())
        return self._outputs[key]


def compile_signals(
    combination: Combination,
    bars: Bars,
    registry: BlockRegistry = LIBRARY,
    cache: BlockCache | None = None,
) -> Signals:
    if cache is None:
        cache = BlockCache(bars)
    elif cache.bars is not bars:
        raise ValueError("the BlockCache was made for other bars")

    def run(bound, *args, key=()):
        def compute():
            return registry.get(bound.block).fn(bars, bound.params, *args)

        return cache.get((bound, *key), compute)

    raw_long, raw_short = (np.array(side, dtype=np.bool_) for side in run(combination.entry))
    long, short = raw_long.copy(), raw_short.copy()
    for bound in combination.filters:
        allow_long, allow_short = run(bound)
        long &= allow_long
        short &= allow_short
    if combination.direction == "long":
        short[:] = False
    elif combination.direction == "short":
        long[:] = False
    both = long & short  # an entry that points both ways says nothing; skip it
    long &= ~both
    short &= ~both

    stop_dist = np.asarray(run(combination.stop), dtype=np.float64)
    if combination.target is None:
        target_dist = np.full(len(bars), np.nan)
    else:
        # A target is measured in units of the stop, so the same target differs per stop.
        target_dist = np.asarray(
            run(combination.target, stop_dist, key=(combination.stop,)), dtype=np.float64
        )
    trail_dist = None
    if combination.trailing is not None:
        trail_dist = np.asarray(run(combination.trailing), dtype=np.float64)
    time_bars = 0 if combination.time is None else int(run(combination.time))
    # The opposite-signal exit listens to the entry Block alone: filters and direction decide
    # when to enter, not when an existing trade has been contradicted.
    exit_long = exit_short = None
    if combination.opposite is not None:
        exit_long, exit_short = raw_short & ~raw_long, raw_long & ~raw_short

    tradable = stop_dist > 0  # False where the stop is still NaN during warm-up
    # All read-only, like the cached arrays among them, so the engine sees one array type.
    long, short, target_dist, exit_long, exit_short = _read_only(
        (long & tradable, short & tradable, target_dist, exit_long, exit_short)
    )
    return Signals(
        long,
        short,
        stop_dist,
        target_dist,
        trail_dist=trail_dist,
        time_bars=time_bars,
        exit_long=exit_long,
        exit_short=exit_short,
    )


def _read_only(output):
    if isinstance(output, np.ndarray):
        output.setflags(write=False)
    elif isinstance(output, tuple):
        for item in output:
            _read_only(item)
    return output
