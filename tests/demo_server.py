"""Serve the app over the real bar cache with a canned AI and a throwaway Ledger and settings, for
a click-through without an OmniRoute key: `uv run python tests/demo_server.py [port]`."""

import json
import sys
import tempfile
from pathlib import Path

import uvicorn

from vibedesk.app.research import EXAMPLE
from vibedesk.app.server import create_app
from vibedesk.app.settings import DATA

RSI = {
    "name": "EURUSD H4 RSI bounce in quiet trends",
    "symbol": "EURUSD",
    "timeframe": "H4",
    "direction": "long",
    "entry": {"block": "rsi_threshold", "params": {"level": [25, 30]}},
    "filters": [{"block": "adx", "params": {"max": 25}}],
    "exit": {"stop": {"block": "atr_stop", "params": {"mult": 2.0}},
             "target": {"block": "r_target", "params": {"r": [1.5, 2.0]}}},
    "sizing": {"block": "fixed_risk", "params": {}},
}  # fmt: skip


class CannedLlm:
    def chat(self, messages: list[dict]) -> str:
        spec = RSI if "eurusd" in messages[-1]["content"].lower() else EXAMPLE
        return f"(Canned AI for testing.) {spec['name']}.\n```json\n{json.dumps(spec)}\n```"


if __name__ == "__main__":
    scratch = Path(tempfile.mkdtemp(prefix="vibedesk-demo-"))
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8766
    app = create_app(DATA, llm=CannedLlm(), db=scratch / "desk.db", settings_root=scratch)
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
