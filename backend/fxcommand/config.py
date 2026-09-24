"""Process configuration from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
ROOT_DIR = BACKEND_DIR.parent


@dataclass(frozen=True)
class Config:
    broker: str = "sim"  # BROKER=sim|mt5
    db_url: str = ""
    host: str = "127.0.0.1"
    port: int = 8000
    sim_seed: int = 42
    sim_speed: float | None = None  # overrides the persisted setting at startup when set
    sim_start: int | None = None
    mt5_path: str | None = None
    static_dir: Path | None = None
    run_engine: bool = True
    keep_awake: bool = False  # keep Windows awake while a Session is active (default on for BROKER=mt5)

    @classmethod
    def from_env(cls) -> "Config":
        broker = os.environ.get("BROKER", "sim").lower()
        db = os.environ.get("FXC_DB") or str(BACKEND_DIR / "data" / f"fxcommand-{broker}.db")
        static = os.environ.get("FXC_STATIC") or str(ROOT_DIR / "frontend" / "dist")
        speed = os.environ.get("FXC_SIM_SPEED")
        start = os.environ.get("FXC_SIM_START")
        return cls(
            broker=broker,
            db_url=db if db.startswith("sqlite:") else f"sqlite:///{db}",
            host=os.environ.get("FXC_HOST", "127.0.0.1"),
            port=int(os.environ.get("FXC_PORT", "8000")),
            sim_seed=int(os.environ.get("FXC_SIM_SEED", "42")),
            sim_speed=float(speed) if speed not in (None, "") else None,
            sim_start=int(start) if start else None,
            mt5_path=os.environ.get("MT5_PATH") or None,
            static_dir=Path(static) if Path(static).is_dir() else None,
            run_engine=os.environ.get("FXC_ENGINE", "1") != "0",
            keep_awake=os.environ.get("FXC_KEEP_AWAKE", "1" if broker == "mt5" else "0") == "1",
        )
