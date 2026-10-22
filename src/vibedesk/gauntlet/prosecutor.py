"""The Prosecutor: fixed checks that a good result is luck (spec §6.3).

Each check answers one question about the stitched blind record. A red flag is a failed check.
A split with fewer than MIN_SPLIT_TRADES trades is too thin to judge and is reported as n/a,
never as passed. The Research Desk's LLM only narrates these results; it never judges.

- Concentration: do the 5 best trades make more than half of net R?
- Neighbours: do the grid Combinations one step away from the Holdout's choice (one parameter
  moved to the next value on its own axis) hold up, by their median blind profit factor?
- Sides: for a Spec that trades both ways, does each side pay on its own?
- Regimes: does the record pay in quiet, normal and busy markets? Entries are split into
  terciles of ATR(14) at the signal bar, as a share of the close, so the drift of the price
  level over the years doesn't pass for a change in volatility.
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np

from vibedesk.blocks.indicators import atr
from vibedesk.blocks.spec import Combination
from vibedesk.engine.metrics import profit_factor
from vibedesk.gauntlet.verdict import Check, judged, not_applicable
from vibedesk.market.bars import Bars

MAX_TOP5_SHARE = 0.5
MIN_NEIGHBOUR_PF = 1.0
MIN_SIDE_PF = 1.0
MIN_REGIME_PF = 0.9
MIN_SPLIT_TRADES = 30
ATR_PERIOD = 14
REGIMES = ("quiet", "normal", "busy")


def concentration(top5_share: float) -> Check:
    return judged(
        "top5_share",
        top5_share,
        MAX_TOP5_SHARE,
        at_most=True,
        na_note="net R is not positive, so there is no profit to concentrate",
    )


def neighbours(
    combinations: Sequence[Combination], blind_pf: np.ndarray, chosen: int | None
) -> Check:
    name = "neighbour_profit_factor"
    if chosen is None:
        return not_applicable(name, MIN_NEIGHBOUR_PF, "no Combination was chosen for the Holdout")
    close = neighbour_indexes(combinations, chosen)
    values = blind_pf[close]
    values = values[~np.isnan(values)]
    if not len(values):
        return not_applicable(name, MIN_NEIGHBOUR_PF, "no traded neighbours one step away")
    return judged(
        name, float(np.median(values)), MIN_NEIGHBOUR_PF, note=f"{len(values)} neighbours"
    )


def neighbour_indexes(combinations: Sequence[Combination], chosen: int) -> list[int]:
    """Combinations that differ from `chosen` in exactly one parameter, by one step."""
    flat = [_parameters(c) for c in combinations]
    values: dict[tuple, set] = {}
    for params in flat:
        for key, value in params.items():
            values.setdefault(key, set()).add(value)
    axes = {key: sorted(found) for key, found in values.items()}
    centre = flat[chosen]
    found = []
    for i, params in enumerate(flat):
        changed = [key for key in centre if params[key] != centre[key]]
        if len(changed) != 1:
            continue
        axis = axes[changed[0]]
        if abs(axis.index(params[changed[0]]) - axis.index(centre[changed[0]])) == 1:
            found.append(i)
    return found


def sides(records: np.ndarray, direction: str) -> list[Check]:
    if direction != "both":
        return []
    checks = []
    for side, name in ((1, "long_profit_factor"), (-1, "short_profit_factor")):
        r = records["r"][records["side"] == side]
        if len(r) < MIN_SPLIT_TRADES:
            checks.append(not_applicable(name, MIN_SIDE_PF, f"{len(r)} trades"))
        else:
            checks.append(judged(name, profit_factor(r), MIN_SIDE_PF, note=f"{len(r)} trades"))
    return checks


def regimes(records: np.ndarray, bars: Bars) -> list[Check]:
    signal = records["entry_idx"] - 1
    level = (atr(bars.high, bars.low, bars.close, ATR_PERIOD) / bars.close)[signal]
    # Terciles of the record's own entries, so each split holds a third of the trades.
    cuts = np.nanquantile(level, [1 / 3, 2 / 3]) if len(level) else np.array([np.nan, np.nan])
    tercile = np.searchsorted(cuts, level, side="right")
    checks = []
    for k, regime in enumerate(REGIMES):
        r = records["r"][tercile == k]
        name = f"{regime}_regime_profit_factor"
        if len(r) < MIN_SPLIT_TRADES:
            checks.append(not_applicable(name, MIN_REGIME_PF, f"{len(r)} trades"))
        else:
            checks.append(judged(name, profit_factor(r), MIN_REGIME_PF, note=f"{len(r)} trades"))
    return checks


def prosecute(wf) -> list[Check]:
    """Every Prosecutor check on a WalkForward."""
    records = wf.trades.records
    return [
        concentration(wf.metrics.top5_share),
        neighbours(wf.combinations, wf.blind_pf, wf.holdout_choice),
        *sides(records, wf.combinations[0].direction),
        *regimes(records, wf.market.bars),
    ]


def _parameters(combination: Combination) -> dict[tuple, object]:
    """Every parameter of a Combination, keyed by (slot, Block, parameter)."""
    slots = {
        "entry": combination.entry,
        "stop": combination.stop,
        "target": combination.target,
        "trailing": combination.trailing,
        "time": combination.time,
        "opposite": combination.opposite,
        "sizing": combination.sizing,
    }
    slots |= {f"filters[{i}]": f for i, f in enumerate(combination.filters)}
    out = {}
    for slot, bound in slots.items():
        if bound is not None:
            for name, value in bound.params.model_dump().items():
                out[(slot, bound.block, name)] = value
    return out
