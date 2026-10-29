"""Serve the app over the bar cache with a canned AI and a throwaway Ledger and settings, for
a click-through without an OmniRoute key: `uv run python tests/demo_server.py [port]`."""

from __future__ import annotations

import json
import sys
import tempfile
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI

from vibedesk.app.llm import Turn
from vibedesk.app.research import EXAMPLE
from vibedesk.app.server import create_app
from vibedesk.app.settings import DATA
from vibedesk.market.bars import Timeframe
from vibedesk.market.cache import record_import, write_chunk
from vibedesk.market.costs import CostMeasurement, record_cost
from vibedesk.market.synthetic import SyntheticBarSource
from vibedesk.notify import RecordingNotifier

RSI = {
    "name": "EURUSD H4 RSI bounce in quiet trends",
    "symbol": "EURUSD",
    "timeframe": "H4",
    "direction": "long",
    "entry": {"block": "rsi_threshold", "params": {"level": [25, 30]}},
    "filters": [{"block": "adx", "params": {"max": 25}}],
    "exit": {
        "stop": {"block": "atr_stop", "params": {"mult": 2.0}},
        "target": {"block": "r_target", "params": {"r": [1.5, 2.0]}},
    },
    "sizing": {"block": "fixed_risk", "params": {}},
}  # fmt: skip


class CannedLlm:
    def chat(self, messages: list[dict]) -> str:
        content = messages[-1].get("content", "") if messages else ""
        spec = RSI if "eurusd" in str(content).lower() else EXAMPLE
        return f"(Canned AI for testing.) {spec['name']}.\n```json\n{json.dumps(spec)}\n```"

    def complete(self, messages: list[dict[str, Any]], schemas: Any = None) -> Turn:
        return Turn(content=self.chat(messages))


def _write_symbol_bars(root: Path, source: SyntheticBarSource, symbol: str) -> None:
    for tf in (Timeframe.M15, Timeframe.H1, Timeframe.H4):
        bars = source.fetch(symbol, tf)
        for year in range(2013, 2025):
            start = datetime(year, 1, 1, tzinfo=UTC).timestamp()
            end = datetime(year + 1, 1, 1, tzinfo=UTC).timestamp()
            write_chunk(root, bars.between(int(start), int(end)), year)


def _record_demo_metadata(root: Path, symbol: str) -> None:
    record_import(root, symbol, datetime(2025, 1, 1, tzinfo=UTC))
    record_cost(
        root,
        CostMeasurement(
            symbol, "2026-10-23T00:00:00+00:00", 0.01, 30.0, 35.0, 1000, -50.0, 10.0, 3
        ),
    )


def populate_demo_cache(root: Path) -> None:
    """Populate synthetic GOLD and EURUSD bars if bar cache is empty."""
    bars_dir = root / "bars"
    if (bars_dir / "GOLD").exists() and (bars_dir / "EURUSD").exists():
        return
    source = SyntheticBarSource(1, date(2013, 12, 1), date(2025, 1, 1))
    for symbol in ("GOLD", "EURUSD"):
        _write_symbol_bars(root, source, symbol)
        _record_demo_metadata(root, symbol)


def create_demo_app(
    data_dir: Path | None = None,
    scratch: Path | None = None,
    llm: Any = None,
    notifier: Any = None,
) -> tuple[FastAPI, Path, Path]:
    """Create a configured demo app with canned LLM and throwaway ledger/settings."""
    if scratch is None:
        scratch = Path(tempfile.mkdtemp(prefix="vibedesk-demo-"))
    else:
        scratch.mkdir(parents=True, exist_ok=True)

    if data_dir is None:
        # Use DATA if it already contains bars, otherwise create synthetic cache in scratch / data
        if (DATA / "bars" / "GOLD").exists():
            data_dir = DATA
        else:
            data_dir = scratch / "data"
            data_dir.mkdir(parents=True, exist_ok=True)
            populate_demo_cache(data_dir)
    else:
        data_dir.mkdir(parents=True, exist_ok=True)
        populate_demo_cache(data_dir)

    app = create_app(
        root=data_dir,
        llm=llm or CannedLlm(),
        db=scratch / "desk.db",
        settings_root=scratch,
        enable_scheduler=False,
        notifier=notifier if notifier is not None else RecordingNotifier(),
    )

    return app, data_dir, scratch


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8766
    app, data_dir, scratch = create_demo_app()
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
