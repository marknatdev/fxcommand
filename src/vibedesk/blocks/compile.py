"""Compile one Combination of a Strategy Spec into the per-bar Signals the engine trades."""

import numpy as np

from vibedesk.blocks.registry import LIBRARY, BlockRegistry
from vibedesk.blocks.spec import Combination
from vibedesk.engine.simulate import Signals
from vibedesk.market.bars import Bars


def compile_signals(
    combination: Combination, bars: Bars, registry: BlockRegistry = LIBRARY
) -> Signals:
    def run(bound, *args):
        return registry.get(bound.block).fn(bars, bound.params, *args)

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
        target_dist = np.asarray(run(combination.target, stop_dist), dtype=np.float64)
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
    return Signals(
        long & tradable,
        short & tradable,
        stop_dist,
        target_dist,
        trail_dist=trail_dist,
        time_bars=time_bars,
        exit_long=exit_long,
        exit_short=exit_short,
    )
