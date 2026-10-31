"""Tests for AI-authored custom Blocks: loader, safety, lookahead, and approval (ADR 0012)."""

from __future__ import annotations

import hashlib
import sqlite3
import threading
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


class FakeLedger:
    """In-memory SQLite ledger replicating Migration 4 custom block tables."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._db = sqlite3.connect(":memory:", check_same_thread=False)
        with self._lock:
            self._db.executescript("""
                CREATE TABLE custom_blocks (
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    code TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                );

                CREATE TABLE custom_block_approvals (
                    id INTEGER PRIMARY KEY,
                    block_id INTEGER NOT NULL REFERENCES custom_blocks (id),
                    approver TEXT NOT NULL,
                    approved_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                );
            """)


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

    # Stored in custom_blocks table
    cur = ledger._db.cursor()
    cur.execute("SELECT name, sha256 FROM custom_blocks WHERE name = ?", ("safe_filter",))
    row = cur.fetchone()
    assert row is not None
    assert row[0] == "safe_filter"
    assert row[1] == meta.sha256


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

    # Check ledger records
    cur = ledger._db.cursor()
    cur.execute(
        """
        SELECT b.name, a.approver, a.approved_at
        FROM custom_block_approvals a
        JOIN custom_blocks b ON a.block_id = b.id
        WHERE b.name = ?
        """,
        ("safe_entry",),
    )
    row = cur.fetchone()
    assert row is not None
    assert row[0] == "safe_entry"
    assert row[1] == "owner"


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
    meta_v2 = reg.update_code("dyn_filter", code_v2)

    # Approval MUST be invalidated because hash differs (ADR 0012)
    assert meta_v2.approved is False
    assert meta_v2.approved_at is None
    assert reg.is_approved("dyn_filter") is False

    # Re-approving v2 succeeds
    meta_reapproved = reg.approve("dyn_filter", approver="owner")
    assert meta_reapproved.approved is True
    assert reg.is_approved("dyn_filter") is True


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
    threshold: float = Field(100.0, ge=0.0)

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
    mult: float = 1.0

def persisted_block(bars, p):
    return bars.close > 0
"""
    file_path = custom_dir / "persisted_block.py"
    file_path.write_text(code, encoding="utf-8")
    sha256 = hashlib.sha256(code.encode("utf-8")).hexdigest()

    # Seed Ledger approval for this sha256
    with ledger._lock:
        cur = ledger._db.cursor()
        cur.execute(
            "INSERT INTO custom_blocks (name, kind, code, sha256) VALUES (?, ?, ?, ?)",
            ("persisted_block", "filter", code, sha256),
        )
        block_id = cur.lastrowid
        cur.execute(
            "INSERT INTO custom_block_approvals (block_id, approver) VALUES (?, ?)",
            (block_id, "owner"),
        )
        ledger._db.commit()

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

    # Unapproved custom block is not active in LIBRARY -> rejected by Spec validate
    with pytest.raises(SpecInvalid, match="unknown block"):
        validate(spec_dict)

    # Approve the custom block
    reg.approve("my_custom_entry", approver="owner")

    # Now active in LIBRARY -> Spec validate passes!
    try:
        spec = validate(spec_dict)
        assert spec.entry.block == "my_custom_entry"

        # Grid expands cleanly
        combos = expand_grid(spec)
        assert len(combos) == 1
        assert combos[0].entry.block == "my_custom_entry"

        from vibedesk.blocks.custom.lookahead import base_bars

        bars = base_bars(0)
        signals = compile_signals(combos[0], bars)
        assert len(signals.long_entry) == len(bars)

    finally:
        # Cleanup LIBRARY def
        LIBRARY._defs.pop("my_custom_entry", None)


def test_revoked_or_changed_custom_block_rejected_in_spec():
    reg = CustomBlockRegistry()
    bdef = BlockDef("revokable_entry", Kind.ENTRY, ValidEntryParams, valid_entry_fn, "doc")
    code_v1 = "def revokable_entry(bars, p): pass"
    reg.register("revokable_entry", "entry", code_v1, block_def=bdef)

    # Approve v1
    reg.approve("revokable_entry", approver="owner")

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

    assert validate(spec_dict).entry.block == "revokable_entry"

    # Revoke approval
    reg.revoke("revokable_entry")

    # Must be rejected now
    with pytest.raises(SpecInvalid, match="unknown block"):
        validate(spec_dict)


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
