"""Tests for AI-authored custom Blocks: loader, safety, lookahead, and approval (ADR 0012)."""

from __future__ import annotations

import hashlib
from pathlib import Path

import numpy as np
import pytest
from pydantic import BaseModel, ConfigDict, Field

from vibedesk.blocks.compile import compile_signals
from vibedesk.blocks.custom import (
    CustomBlockLoader,
    CustomBlockRegistry,
    LookaheadError,
    custom_block,
    load_custom_blocks,
    test_custom_block_lookahead,
    validate_custom_block_code,
)
from vibedesk.blocks.indicators import rolling_max_prev, rolling_min_prev
from vibedesk.blocks.registry import LIBRARY, BlockDef, Kind
from vibedesk.blocks.spec import SpecInvalid, expand_grid, validate
from vibedesk.ledger import Ledger
from vibedesk.market.bars import Bars

# --- Safety Validator Tests ---


def test_validator_accepts_clean_code():
    code = """
import numpy as np
from pydantic import BaseModel, ConfigDict, Field
from vibedesk.market.bars import Bars

class Params(BaseModel):
    period: int = Field(14, ge=2, le=100)

def custom_rsi(bars: Bars, p: Params) -> np.ndarray:
    return bars.close > bars.open
"""
    ok, err = validate_custom_block_code(code)
    assert ok is True
    assert err is None


def test_validator_rejects_syntax_errors():
    code = "def bad_syntax(bars: return"
    ok, err = validate_custom_block_code(code)
    assert ok is False
    assert "Syntax error" in str(err)


@pytest.mark.parametrize(
    "bad_import,expected_name",
    [
        ("import os", "os"),
        ("import sys", "sys"),
        ("import subprocess", "subprocess"),
        ("import socket", "socket"),
        ("import urllib.request", "urllib.request"),
        ("import requests", "requests"),
        ("import httpx", "httpx"),
        ("import aiohttp", "aiohttp"),
        ("import MetaTrader5", "MetaTrader5"),
        ("import metatrader5", "metatrader5"),
        ("import shutil", "shutil"),
        ("from os import path", "os"),
        ("from subprocess import Popen", "subprocess"),
    ],
)
def test_validator_rejects_dangerous_imports(bad_import: str, expected_name: str):
    code = f"{bad_import}\ndef my_block(bars, p):\n    return bars.close\n"
    ok, err = validate_custom_block_code(code)
    assert ok is False
    assert f"Forbidden import: {expected_name}" in str(err)


@pytest.mark.parametrize(
    "bad_call,expected_name",
    [
        ("eval('2 + 2')", "eval"),
        ("exec('a = 1')", "exec"),
        ("open('data.txt', 'r')", "open"),
        ("compile('pass', '', 'exec')", "compile"),
        ("__import__('os')", "__import__"),
    ],
)
def test_validator_rejects_dangerous_calls(bad_call: str, expected_name: str):
    code = f"def my_block(bars, p):\n    {bad_call}\n    return bars.close\n"
    ok, err = validate_custom_block_code(code)
    assert ok is False
    assert f"Forbidden call: {expected_name}" in str(err)


def test_validator_rejects_attribute_calls():
    code = "def my_block(bars, p):\n    f = path.open('w')\n    return bars.close\n"
    ok, err = validate_custom_block_code(code)
    assert ok is False
    assert "Forbidden call: open" in str(err)


# --- Look-Ahead Property Testing ---


class ValidEntryParams(BaseModel):
    model_config = ConfigDict(frozen=True)
    period: int = Field(10, ge=2, le=50)


def valid_entry_fn(bars: Bars, p: ValidEntryParams) -> tuple[np.ndarray, np.ndarray]:
    """Long on break above previous highest high; short on break below lowest low."""
    upper = rolling_max_prev(bars.high, p.period)
    lower = rolling_min_prev(bars.low, p.period)
    return bars.close > upper, bars.close < lower


class PeekingEntryParams(BaseModel):
    model_config = ConfigDict(frozen=True)
    look_forward: int = Field(1, ge=1, le=5)


def peeking_entry_fn(bars: Bars, p: PeekingEntryParams) -> tuple[np.ndarray, np.ndarray]:
    """Flawed block that looks ahead at future bars!"""
    # Peeks at the NEXT bar's close using roll
    future_close = np.roll(bars.close, -p.look_forward)
    return future_close > bars.close, future_close < bars.close


class ZeroEntryParams(BaseModel):
    model_config = ConfigDict(frozen=True)


def zero_entry_fn(bars: Bars, p: ZeroEntryParams) -> tuple[np.ndarray, np.ndarray]:
    """Inert block that never signals."""
    n = len(bars)
    return np.zeros(n, dtype=bool), np.zeros(n, dtype=bool)


def test_property_test_accepts_valid_block():
    bdef = BlockDef(
        name="test_valid_entry",
        kind=Kind.ENTRY,
        params=ValidEntryParams,
        fn=valid_entry_fn,
        doc="Valid entry",
    )
    assert test_custom_block_lookahead(bdef, n_cases=30) is True


def test_property_test_rejects_lookahead_block():
    bdef = BlockDef(
        name="test_peeking_entry",
        kind=Kind.ENTRY,
        params=PeekingEntryParams,
        fn=peeking_entry_fn,
        doc="Peeking entry",
    )
    with pytest.raises(LookaheadError, match="failed look-ahead test"):
        test_custom_block_lookahead(bdef, n_cases=30)


def test_property_test_rejects_inert_block():
    bdef = BlockDef(
        name="test_zero_entry",
        kind=Kind.ENTRY,
        params=ZeroEntryParams,
        fn=zero_entry_fn,
        doc="Zero entry",
    )
    with pytest.raises(LookaheadError, match="never produced signal/output"):
        test_custom_block_lookahead(bdef, n_cases=10)


# --- Custom Block Registry & Ledger Integration ---


class FakeLedger(Ledger):
    """Ephemeral SQLite adapter sharing the production Ledger contract."""

    def __init__(self):
        super().__init__(":memory:")


def test_registry_registration_and_unapproved_by_default():
    ledger = FakeLedger()
    reg = CustomBlockRegistry(ledger=ledger)

    code = "def custom_filter(bars, p):\n    return bars.close > 0\n"
    meta = reg.register("safe_filter", "filter", code)

    assert meta.name == "safe_filter"
    assert meta.kind == "filter"
    assert meta.approved is False
    assert meta.approved_at is None
    assert reg.is_approved("safe_filter") is False

    # Stored via Ledger register_custom_block
    rec = ledger.get_custom_block("safe_filter")
    assert rec is not None
    assert rec.name == "safe_filter"
    assert rec.sha256 == meta.sha256


def test_owner_approval_action_and_ledger_record():
    ledger = FakeLedger()
    reg = CustomBlockRegistry(ledger=ledger)

    code = "def custom_entry(bars, p):\n    return bars.close > 100, bars.close < 100\n"
    bdef = BlockDef("safe_entry", Kind.ENTRY, ValidEntryParams, valid_entry_fn, "doc")
    reg.register("safe_entry", "entry", code, block_def=bdef)

    # Approve block
    approved_meta = reg.approve("safe_entry", approver="owner")
    assert approved_meta.approved is True
    assert approved_meta.approved_at is not None
    assert reg.is_approved("safe_entry") is True

    # Check ledger records via read_custom_block_approval_state
    state = ledger.read_custom_block_approval_state("safe_entry")
    assert state["approved"] is True
    assert state["approver"] == "owner"
    assert state["name"] == "safe_entry"


def test_changed_file_invalidates_approval():
    ledger = FakeLedger()
    reg = CustomBlockRegistry(ledger=ledger)

    code_v1 = "def custom_filter(bars, p):\n    return bars.close > 10\n"
    bdef = BlockDef("dyn_filter", Kind.FILTER, ValidEntryParams, valid_entry_fn, "doc")
    reg.register("dyn_filter", "filter", code_v1, block_def=bdef)

    # Owner approves v1
    reg.approve("dyn_filter", approver="owner")
    assert reg.is_approved("dyn_filter") is True

    # Code is modified (v2)
    code_v2 = "def custom_filter(bars, p):\n    return bars.close > 20\n"
    meta_v2 = reg.update_code("dyn_filter", code_v2, block_def=bdef)

    # Approval MUST be invalidated because hash differs (ADR 0012)
    assert meta_v2.approved is False
    assert meta_v2.approved_at is None
    assert reg.is_approved("dyn_filter") is False

    # Re-approving v2 succeeds
    meta_reapproved = reg.approve("dyn_filter", approver="owner")
    assert meta_reapproved.approved is True
    assert reg.is_approved("dyn_filter") is True


def test_custom_block_registry_with_real_ledger(tmp_path: Path):
    db_path = tmp_path / "desk.db"
    ledger = Ledger(db_path)
    reg = CustomBlockRegistry(ledger=ledger)

    code = "def real_custom_entry(bars, p):\n    return bars.close > 50, bars.close < 50\n"
    bdef = BlockDef("real_custom_entry", Kind.ENTRY, ValidEntryParams, valid_entry_fn, "doc")
    meta = reg.register("real_custom_entry", "entry", code, block_def=bdef)
    assert meta.approved is False

    # Ledger record created and unapproved
    rec = ledger.get_custom_block("real_custom_entry")
    assert rec.name == "real_custom_entry"
    assert rec.approved is False

    # Owner approves block
    approved_meta = reg.approve("real_custom_entry", approver="owner")
    assert approved_meta.approved is True
    assert reg.is_approved("real_custom_entry") is True

    # Ledger record verified
    rec2 = ledger.get_custom_block("real_custom_entry")
    assert rec2.approved is True
    assert rec2.approver == "owner"
    assert ledger.is_custom_block_approved("real_custom_entry") is True


# --- Dynamic Loader Tests ---


def test_custom_block_loader_discovery_and_safety(tmp_path: Path):
    custom_dir = tmp_path / "custom"
    custom_dir.mkdir()

    # Valid block file
    valid_file = custom_dir / "valid_block.py"
    valid_file.write_text(
        """
from pydantic import BaseModel, ConfigDict, Field
from vibedesk.blocks.registry import Kind
from vibedesk.market.bars import Bars
import numpy as np

BLOCK_NAME = "valid_block"
BLOCK_KIND = Kind.FILTER

class Params(BaseModel):
    threshold: float = Field(100.0, ge=0.0, le=5000.0)

def valid_block(bars: Bars, p: Params) -> np.ndarray:
    return bars.close > p.threshold
""",
        encoding="utf-8",
    )

    # Dangerous block file (os import)
    bad_file = custom_dir / "evil_block.py"
    bad_file.write_text(
        """
import os
def evil_block(bars, p):
    return bars.close
""",
        encoding="utf-8",
    )

    loader = CustomBlockLoader(directory=custom_dir)
    loaded = loader.load_all()
    assert len(load_custom_blocks(directory=custom_dir)) == 2

    assert len(loaded) == 2
    loaded_map = {m.name: m for m in loaded}

    # Valid block is loaded as unapproved
    assert "valid_block" in loaded_map
    assert loaded_map["valid_block"].approved is False
    assert loaded_map["valid_block"].error is None
    assert loaded_map["valid_block"].block_def is not None

    # Evil block is rejected by safety validator
    assert "evil_block" in loaded_map
    assert loaded_map["evil_block"].approved is False
    assert "Forbidden import: os" in str(loaded_map["evil_block"].error)
    assert loaded_map["evil_block"].block_def is None


def test_loader_restores_ledger_approvals(tmp_path: Path):
    ledger = FakeLedger()
    custom_dir = tmp_path / "custom"
    custom_dir.mkdir()

    code = """
from pydantic import BaseModel, ConfigDict, Field
from vibedesk.blocks.registry import Kind
import numpy as np

BLOCK_NAME = "persisted_block"
BLOCK_KIND = Kind.FILTER

class Params(BaseModel):
    mult: float = Field(1.0, ge=0.1, le=10.0)

def persisted_block(bars, p):
    return bars.close > 0
"""
    file_path = custom_dir / "persisted_block.py"
    file_path.write_text(code, encoding="utf-8")
    sha256 = hashlib.sha256(code.encode("utf-8")).hexdigest()

    # Seed Ledger approval via public methods
    ledger.register_custom_block("persisted_block", "filter", code, sha256=sha256)
    ledger.record_custom_block_approval("persisted_block", "owner")

    # Load with registry connected to ledger
    reg = CustomBlockRegistry(ledger=ledger)
    loader = CustomBlockLoader(directory=custom_dir, registry=reg)
    loaded = loader.load_all()

    assert len(loaded) == 1
    meta = loaded[0]
    assert meta.name == "persisted_block"
    assert meta.approved is True
    assert meta.approved_at is not None
    assert reg.is_approved("persisted_block") is True


# --- Strategy Spec Usage and Rejection Tests ---


def test_strategy_spec_rejects_unapproved_custom_blocks():
    reg = CustomBlockRegistry()
    bdef = BlockDef("my_custom_entry", Kind.ENTRY, ValidEntryParams, valid_entry_fn, "doc")
    code = "def my_custom_entry(bars, p): pass"
    reg.register("my_custom_entry", "entry", code, block_def=bdef)

    spec_dict = {
        "name": "Custom Spec",
        "symbol": "GOLD",
        "timeframe": "H1",
        "direction": "long",
        "entry": {"block": "my_custom_entry", "params": {"period": 20}},
        "filters": [],
        "exit": {"stop": {"block": "atr_stop", "params": {"mult": 2.0}}},
        "sizing": {"block": "fixed_risk", "params": {"risk_pct": 1.0}},
    }

    try:
        # Unapproved custom block in Spec -> SpecError naming block
        with pytest.raises(
            SpecInvalid,
            match=r"custom block 'my_custom_entry' is not approved by the owner \(ADR 0012\)",
        ):
            validate(spec_dict, registry=reg.to_block_registry(include_unapproved=True))

        # Approve the custom block -> accepted
        reg.approve("my_custom_entry", approver="owner")

        spec = validate(spec_dict, registry=reg.to_block_registry(include_unapproved=True))
        assert spec.entry.block == "my_custom_entry"

        # Grid expands cleanly
        combos = expand_grid(spec, registry=reg.to_block_registry())
        assert len(combos) == 1
        assert combos[0].entry.block == "my_custom_entry"

        from vibedesk.blocks.custom.lookahead import base_bars

        bars = base_bars(0)
        signals = compile_signals(combos[0], bars, registry=reg.to_block_registry())
        assert len(signals.long_entry) == len(bars)
    finally:
        # Cleanup LIBRARY def
        assert LIBRARY.get("my_custom_entry") is None


def test_changed_custom_block_rejected_in_spec():
    reg = CustomBlockRegistry()
    bdef = BlockDef("revokable_entry", Kind.ENTRY, ValidEntryParams, valid_entry_fn, "doc")
    code_v1 = "def revokable_entry(bars, p): pass"
    reg.register("revokable_entry", "entry", code_v1, block_def=bdef)

    spec_dict = {
        "name": "Custom Spec",
        "symbol": "GOLD",
        "timeframe": "H1",
        "direction": "long",
        "entry": {"block": "revokable_entry", "params": {"period": 15}},
        "filters": [],
        "exit": {"stop": {"block": "atr_stop", "params": {"mult": 2.0}}},
        "sizing": {"block": "fixed_risk", "params": {"risk_pct": 1.0}},
    }

    try:
        # 1. Unapproved -> rejected
        with pytest.raises(
            SpecInvalid,
            match=r"custom block 'revokable_entry' is not approved by the owner \(ADR 0012\)",
        ):
            validate(spec_dict, registry=reg.to_block_registry(include_unapproved=True))

        # 2. Approved -> accepted
        reg.approve("revokable_entry", approver="owner")
        assert (
            validate(spec_dict, registry=reg.to_block_registry(include_unapproved=True)).entry.block
            == "revokable_entry"
        )

        # Changed source without a reloaded callable is absent from the library.
        reg.update_code("revokable_entry", "x = 2")
        with pytest.raises(SpecInvalid, match="unknown block"):
            validate(spec_dict, registry=reg.to_block_registry(include_unapproved=True))
    finally:
        assert LIBRARY.get("revokable_entry") is None


def test_custom_block_decorator():
    class Params(BaseModel):
        val: int = 5

    @custom_block(name="decorated_filter", kind="filter", params=Params, doc="Decorated doc")
    def decorated_filter(bars: Bars, p: Params) -> np.ndarray:
        return bars.close > p.val

    assert hasattr(decorated_filter, "_block_def")
    bdef = decorated_filter._block_def
    assert bdef.name == "decorated_filter"
    assert bdef.kind == Kind.FILTER
    assert bdef.doc == "Decorated doc"


@pytest.mark.parametrize(
    "code",
    [
        "getattr(__builtins__, 'open')",
        "().__class__.__base__.__subclasses__()",
        "import pathlib",
        "import ctypes",
        "import pickle",
        "import marshal",
        "import io",
        "import tempfile",
        "import multiprocessing",
        "import vibedesk.market.mt5",
        "setattr(x, 'a', 1)",
        "globals()",
        "vars()",
        "from . import other",
        "import math as __hidden",
    ],
)
def test_review_validator_bypasses(code):
    assert validate_custom_block_code(code)[0] is False


@pytest.mark.parametrize(
    "fn,code",
    [
        (peeking_entry_fn, "def peek(bars, p): return bars.close"),
        (valid_entry_fn, "import pathlib"),
    ],
)
def test_failed_verification_cannot_be_approved(fn, code):
    ledger = FakeLedger()
    reg = CustomBlockRegistry(ledger=ledger)
    params = PeekingEntryParams if fn is peeking_entry_fn else ValidEntryParams
    bdef = BlockDef("bad", Kind.ENTRY, params, fn, "")
    meta = reg.register("bad", "entry", code, block_def=bdef)
    assert meta.error and not meta.verified
    with pytest.raises(ValueError, match="failed verification"):
        reg.approve("bad")
    assert not ledger.is_custom_block_approved("bad")


def test_missing_definition_cannot_be_approved():
    reg = CustomBlockRegistry()
    reg.register("missing", "entry", "x = 1")
    with pytest.raises(ValueError, match="failed verification"):
        reg.approve("missing")


def test_builtin_names_and_global_library_are_protected():
    reg = CustomBlockRegistry()
    with pytest.raises(ValueError, match="clashes"):
        reg.register("atr_stop", "stop", "x = 1")
    with pytest.raises(ValueError, match="isolated"):
        CustomBlockRegistry(library=LIBRARY)
    assert not hasattr(reg, "revoke")


def test_real_ledger_edit_reapprove_and_restart(tmp_path):
    path = tmp_path / "versions.db"
    bdef = BlockDef("versioned", Kind.ENTRY, ValidEntryParams, valid_entry_fn, "")
    with Ledger(path) as ledger:
        reg = CustomBlockRegistry(ledger=ledger)
        first = reg.register("versioned", "entry", "x = 1", block_def=bdef)
        reg.approve("versioned")
        second = reg.update_code("versioned", "x = 2", block_def=bdef)
        assert not second.approved
        assert first.block_id != second.block_id
        assert ledger.get_custom_block(first.block_id).approved
        assert not ledger.get_custom_block("versioned").approved
        reg.approve("versioned")
    with Ledger(path) as ledger:
        reg = CustomBlockRegistry(ledger=ledger)
        assert reg.register("versioned", "entry", "x = 2", block_def=bdef).approved
        assert len(ledger.list_custom_blocks()) == 2
        reg.update_code("versioned", "x = 3")
        with pytest.raises(ValueError, match="verification"):
            reg.approve("versioned")


def test_invalid_reload_removes_previously_approved_callable(tmp_path):
    path = tmp_path / "example.py"
    path.write_text(
        """
from pydantic import BaseModel
from vibedesk.blocks.registry import Kind
BLOCK_NAME = "different_name"
BLOCK_KIND = Kind.FILTER
class Params(BaseModel):
    pass
def run(bars, p):
    return bars.close > bars.open
""",
        encoding="utf-8",
    )
    loader = CustomBlockLoader()
    assert loader.load_file(path).verified
    loader.registry.approve("different_name")
    path.write_text("import pathlib", encoding="utf-8")
    assert loader.load_file(path).error
    assert not loader.registry.is_approved("different_name")
    assert loader.registry.to_block_registry().get("different_name") is None


def test_lookahead_seed_is_stable(monkeypatch):
    import zlib

    import vibedesk.blocks.custom.lookahead as module

    seeds = []
    original = np.random.default_rng

    def capture(seed):
        seeds.append(seed)
        return original(seed)

    monkeypatch.setattr(module.np.random, "default_rng", capture)
    bdef = BlockDef("stable", Kind.ENTRY, ValidEntryParams, valid_entry_fn, "")
    assert test_custom_block_lookahead(bdef, n_cases=2)
    assert seeds[0] == zlib.crc32(b"stable")
