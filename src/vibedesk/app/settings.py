"""App settings: the data folder and the OmniRoute connection.

Values come from `data/settings.json` (written by the app's settings form), then from the
environment or a `.env` file at the repo root. The key never leaves this computer except in the
Authorization header sent to OmniRoute.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
DATA = REPO / "data"
DEFAULT_LLM_URL = "http://127.0.0.1:20128/v1"  # OmniRoute, through the SSH tunnel


@dataclass
class Settings:
    llm_url: str = DEFAULT_LLM_URL
    llm_key: str = ""
    llm_model: str = ""

    def public(self) -> dict:
        """What the page may see: never the key itself."""
        return {"llm_url": self.llm_url, "llm_model": self.llm_model,
                "llm_key_set": bool(self.llm_key)}  # fmt: skip


def load(root: Path = DATA) -> Settings:
    env = {**_dotenv(REPO / ".env"), **os.environ}
    settings = Settings(
        llm_url=env.get("VIBEDESK_LLM_URL", DEFAULT_LLM_URL),
        llm_key=env.get("VIBEDESK_LLM_KEY", ""),
        llm_model=env.get("VIBEDESK_LLM_MODEL", ""),
    )
    saved = root / "settings.json"
    if saved.exists():
        for name, value in json.loads(saved.read_text(encoding="utf-8")).items():
            if hasattr(settings, name) and value:
                setattr(settings, name, value)
    return settings


def save(settings: Settings, root: Path = DATA) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "settings.json").write_text(json.dumps(asdict(settings), indent=2), encoding="utf-8")


def _dotenv(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            name, value = line.split("=", 1)
            values[name.strip()] = value.strip().strip('"').strip("'")
    return values
