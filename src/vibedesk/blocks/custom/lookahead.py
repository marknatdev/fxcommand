"""Look-ahead verification for custom Blocks (spec §8.2, ADR 0012).

Spec §9.5: no Block may look ahead. Its output at bar t must not change when
bars after t do.
"""

from __future__ import annotations

from functools import cache
from typing import Any

import numpy as np
from pydantic import BaseModel

from vibedesk.blocks.exits import AtrStopParams, atr_stop
from vibedesk.blocks.registry import BlockDef, Kind
from vibedesk.market.bars import Bars, Timeframe


class LookaheadError(AssertionError):
    """Raised when a Block uses future data to calculate past or current signals."""


@cache
def base_bars(seed: int) -> Bars:
    """Generate deterministic synthetic bars for property testing without BarSource."""
    rng = np.random.default_rng(seed)
    n_bars = 400
    time = 1700000000 + np.arange(n_bars, dtype=np.int64) * 900
    step = 0.5
    close = 2000.0 + np.cumsum(rng.normal(0, step, n_bars))
    open_ = np.r_[2000.0, close[:-1]]
    high = np.maximum(open_, close) + rng.uniform(0.1, step, n_bars)
    low = np.minimum(open_, close) - rng.uniform(0.1, step, n_bars)
    spread = np.full(n_bars, 35, dtype=np.int32)
    return Bars(
        symbol="TEST",
        timeframe=Timeframe.M15,
        point=0.01,
        digits=2,
        time=time,
        open=open_,
        high=high,
        low=low,
        close=close,
        spread=spread,
    )


def rewrite_after(bars: Bars, cut: int, rng: np.random.Generator) -> Bars:
    """Return the same bars up to cut, then a different random future."""
    n = len(bars)
    future = n - cut - 1
    step = bars.close[cut] * 0.002
    close = bars.close[cut] + np.cumsum(rng.normal(0, step, future))
    open_ = np.r_[bars.close[cut], close[:-1]]
    high = np.maximum(open_, close) + rng.uniform(0, step, future)
    low = np.minimum(open_, close) - rng.uniform(0, step, future)

    def splice(head, tail):
        return np.r_[head[: cut + 1], tail]

    return Bars(
        bars.symbol,
        bars.timeframe,
        bars.point,
        bars.digits,
        bars.time,
        splice(bars.open, open_),
        splice(bars.high, high),
        splice(bars.low, low),
        splice(bars.close, close),
        splice(bars.spread, rng.integers(0, 200, future)),
    )


def outputs(block: BlockDef, params: Any, bars: Bars) -> list[np.ndarray]:
    """Execute block function and return list of output arrays."""
    if block.kind is Kind.TARGET:
        stop_dist = atr_stop(bars, AtrStopParams())
        result = block.fn(bars, params, stop_dist)
    else:
        result = block.fn(bars, params)
    return [np.asarray(x) for x in (result if isinstance(result, tuple) else (result,))]


def test_custom_block_lookahead(
    block: BlockDef,
    sample_params: dict[str, Any] | BaseModel | None = None,
    n_cases: int = 50,
) -> bool:
    """Run property test ensuring block outputs at bar t never depend on bars > t.

    Raises LookaheadError if look-ahead bias is detected. Returns True on success.
    """
    if sample_params is None:
        params = block.params()
    elif isinstance(sample_params, BaseModel):
        params = sample_params
    elif isinstance(sample_params, dict):
        params = block.params(**sample_params)
    else:
        raise TypeError(f"Invalid sample_params type: {type(sample_params)}")

    rng = np.random.default_rng(abs(hash(block.name)) % 2**32)
    signalled = False

    for case in range(n_cases):
        bars = base_bars(case % 5)
        cut = int(rng.integers(50, len(bars) - 2))
        original = outputs(block, params, bars)
        changed = outputs(block, params, rewrite_after(bars, cut, rng))

        for before, after in zip(original, changed, strict=True):
            if not np.array_equal(before[: cut + 1], after[: cut + 1], equal_nan=True):
                raise LookaheadError(
                    f"Custom block {block.name!r} failed look-ahead test: "
                    f"output at or before bar {cut} changed when future bars were altered"
                )
        signalled = signalled or any(np.any(x[: cut + 1] != 0) for x in original if x.dtype == bool)
        signalled = signalled or any(np.isfinite(x).any() for x in original if x.dtype != bool)

    if not signalled:
        raise LookaheadError(
            f"Custom block {block.name!r} never produced signal/output across {n_cases} test cases"
        )

    return True


test_custom_block_lookahead.__test__ = False  # Ignore as pytest test
verify_custom_block_lookahead = test_custom_block_lookahead
