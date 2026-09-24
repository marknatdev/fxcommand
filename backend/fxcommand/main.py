"""``uv run fxcommand`` — start the engine + API + dashboard in one process."""

from __future__ import annotations

import logging

import uvicorn

from .app import create_app
from .config import Config


def run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    config = Config.from_env()
    if config.broker not in ("sim", "mt5"):
        raise SystemExit(f"BROKER must be 'sim' or 'mt5', got {config.broker!r}")
    # exactly one worker, no reload: a second process would start a second engine (ADR 0001)
    uvicorn.run(create_app(config), host=config.host, port=config.port, workers=1, reload=False, log_level="info")


if __name__ == "__main__":
    run()
