import copy
import json
from typing import Literal, get_args

import pytest
from pydantic import BaseModel, ConfigDict, Field

from vibedesk.blocks.registry import BlockRegistry, Kind
from vibedesk.blocks.spec import MAX_COMBINATIONS, SpecInvalid, Symbol, expand_grid, validate
from vibedesk.market.importer import UNIVERSE
from vibedesk.market.synthetic import PROFILES
from vibedesk.market.universe import SYMBOLS


class _Params(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RangeBreakout(_Params):
    session: Literal["asia", "london_open", "ny_open"]
    range_bars: int = Field(ge=1, le=48)


class MaSlope(_Params):
    period: int = Field(ge=2, le=500)
    min_slope_atr: float = Field(0.0, ge=0)


class AtrStop(_Params):
    period: int = Field(14, ge=2, le=200)
    mult: float = Field(gt=0, le=20)


class RTarget(_Params):
    r: float = Field(gt=0, le=20)


class TimeExit(_Params):
    bars: int = Field(ge=1, le=1000)


class FixedRisk(_Params):
    risk_pct: float = Field(1.0, gt=0, le=1.0)


@pytest.fixture
def registry():
    """Stub Blocks with the §5.2 names and parameters; their implementations don't matter here."""
    reg = BlockRegistry()
    for name, kind, params in [
        ("range_breakout", Kind.ENTRY, RangeBreakout),
        ("ma_slope", Kind.FILTER, MaSlope),
        ("atr_stop", Kind.STOP, AtrStop),
        ("r_target", Kind.TARGET, RTarget),
        ("time_exit", Kind.TIME, TimeExit),
        ("fixed_risk", Kind.SIZING, FixedRisk),
    ]:
        reg.register(name, kind, params)(lambda *args: None)
    return reg


EXAMPLE = {  # spec §5.1
    "name": "london-breakout-trend",
    "symbol": "GOLD",
    "timeframe": "M15",
    "direction": "both",
    "entry": {
        "block": "range_breakout",
        "params": {"session": "london_open", "range_bars": [4, 8]},
    },
    "filters": [{"block": "ma_slope", "params": {"period": [50, 100, 200], "min_slope_atr": 0.0}}],
    "exit": {
        "stop": {"block": "atr_stop", "params": {"mult": [1.5, 2.0, 2.5]}},
        "target": {"block": "r_target", "params": {"r": [2.0, 3.0]}},
        "time": {"block": "time_exit", "params": {"bars": 32}},
    },
    "sizing": {"block": "fixed_risk", "params": {"risk_pct": 1.0}},
}


def variant(**changes):
    spec = copy.deepcopy(EXAMPLE)
    for path, value in changes.items():
        node = spec
        *parents, leaf = path.split("__")
        for key in parents:
            node = node[int(key)] if key.isdigit() else node[key]
        node[leaf] = value
    return spec


def errors_of(spec, registry):
    with pytest.raises(SpecInvalid) as caught:
        validate(spec, registry)
    return {(e.path, e.reason) for e in caught.value.errors}


def paths_of(spec, registry):
    return {path for path, _ in errors_of(spec, registry)}


def test_example_spec_validates_and_expands_to_36_combinations(registry):
    spec = validate(EXAMPLE, registry)
    combos = expand_grid(spec, registry)

    assert len(combos) == 2 * 3 * 3 * 2 == 36
    first = combos[0]
    assert first.direction == "both"
    assert first.entry.params == RangeBreakout(session="london_open", range_bars=4)
    assert first.stop.params == AtrStop(mult=1.5)  # default period filled in
    assert first.time.params == TimeExit(bars=32)
    assert first.trailing is None and first.opposite is None


def test_spec_validates_from_a_json_string(registry):
    assert validate(json.dumps(EXAMPLE), registry).name == "london-breakout-trend"


def test_grid_over_the_limit_is_rejected(registry):
    spec = variant(exit__target__params={"r": [1.5, 2.0, 3.0]})  # 54 combinations

    [(path, reason)] = errors_of(spec, registry)

    assert MAX_COMBINATIONS == 50
    assert path == "grid"
    assert "54" in reason and "50" in reason


@pytest.mark.parametrize(
    "change, path",
    [
        (dict(symbol="DOGEUSD"), "symbol"),
        (dict(timeframe="M1"), "timeframe"),
        (dict(direction="sideways"), "direction"),
        (dict(entry__block="no_such_block"), "entry.block"),
        (dict(entry__block="ma_slope"), "entry.block"),  # a filter is not an entry
        (dict(filters__0__block="atr_stop"), "filters[0].block"),
        (dict(exit__stop__params={"mult": 2.0, "colour": "red"}), "exit.stop.params.colour"),
        (dict(exit__stop__params={"mult": [2.0, 25.0]}), "exit.stop.params.mult"),
        (dict(exit__stop__params={"mult": []}), "exit.stop.params.mult"),
        (dict(exit__stop__params={"mult": [2.0, 2.0]}), "exit.stop.params.mult"),
        (dict(exit__stop__params={"mult": [2.0, "wide"]}), "exit.stop.params.mult"),
        (dict(sizing__params={"risk_pct": 2.0}), "sizing.params.risk_pct"),  # never size up
    ],
)
def test_invalid_specs_report_where_they_fail(registry, change, path):
    assert path in paths_of(variant(**change), registry)


@pytest.mark.parametrize("risk_pct", [[0.5, 1.0], [1.0], [], [0.5, 2.0]])
def test_sizing_takes_one_value_not_a_list(registry, risk_pct):
    [(path, reason)] = errors_of(variant(sizing__params={"risk_pct": risk_pct}), registry)

    assert path == "sizing.params.risk_pct"
    assert "doesn't change a Trial's R" in reason and "one value" in reason


def test_a_sizing_list_is_reported_with_the_other_errors(registry):
    spec = variant(entry__block="no_such_block", sizing__params={"risk_pct": [0.5, 1.0]})

    assert {"entry.block", "sizing.params.risk_pct"} <= paths_of(spec, registry)


def test_an_unknown_symbol_is_told_the_symbols_it_could_be(registry):
    [(path, reason)] = errors_of(variant(symbol="DOGEUSD"), registry)

    assert path == "symbol"
    assert all(f"'{symbol}'" in reason for symbol in SYMBOLS)


def test_the_spec_the_importer_and_the_synthetic_bars_share_one_universe():
    assert len(set(SYMBOLS)) == len(SYMBOLS)
    assert get_args(Symbol) == SYMBOLS
    assert UNIVERSE == SYMBOLS
    assert set(PROFILES) == set(SYMBOLS)


def test_missing_stop_is_rejected(registry):
    spec = copy.deepcopy(EXAMPLE)
    del spec["exit"]["stop"]

    assert "exit.stop" in paths_of(spec, registry)


def test_missing_required_parameter_is_rejected(registry):
    assert "exit.stop.params.mult" in paths_of(variant(exit__stop__params={}), registry)


def test_errors_are_collected_not_stopped_at_the_first(registry):
    spec = variant(entry__block="no_such_block", exit__stop__params={"mult": 99.0})

    assert {"entry.block", "exit.stop.params.mult"} <= paths_of(spec, registry)


def test_grid_order_does_not_depend_on_how_the_spec_was_written(registry):
    shuffled = variant(
        entry__params={"range_bars": [8, 4], "session": "london_open"},
        filters__0__params={"min_slope_atr": 0.0, "period": [200, 50, 100]},
        exit__stop__params={"mult": [2.5, 1.5, 2.0]},
    )

    original = [c.label() for c in expand_grid(validate(EXAMPLE, registry), registry)]
    reordered = [c.label() for c in expand_grid(validate(shuffled, registry), registry)]

    assert original == reordered
    assert len(set(original)) == 36
