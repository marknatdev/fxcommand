"""App settings: the data folder, the OmniRoute connection, token caps, and notifications.

Values come from `data/settings.json` (written by the app's settings form), then from the
environment or a `.env` file at the repo root. Secrets (API keys, Telegram bot token) are
stored in `data/secrets.json` or `.env` and never logged or committed.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
DATA = REPO / "data"
DEFAULT_LLM_URL = "http://127.0.0.1:20128/v1"  # OmniRoute, through the SSH tunnel
DEFAULT_DAILY_TOKEN_CAP = 100_000


@dataclass
class Settings:
    llm_url: str = DEFAULT_LLM_URL
    llm_key: str = ""
    llm_model: str = ""
    daily_token_cap: int = DEFAULT_DAILY_TOKEN_CAP
    telegram_enabled: bool = False
    telegram_chat_id: str = ""

    def public(self) -> dict:
        """What the page may see: never the key or secrets themselves."""
        return {
            "llm_url": self.llm_url,
            "llm_model": self.llm_model,
            "llm_key_set": bool(self.llm_key),
            "daily_token_cap": self.daily_token_cap,
            "telegram_enabled": self.telegram_enabled,
            "telegram_chat_id_set": bool(self.telegram_chat_id),
        }


def load(root: Path = DATA) -> Settings:
    env = {**_dotenv(REPO / ".env"), **os.environ}
    token_cap_str = env.get("VIBEDESK_DAILY_TOKEN_CAP", "")
    daily_token_cap = int(token_cap_str) if token_cap_str.isdigit() else DEFAULT_DAILY_TOKEN_CAP
    telegram_enabled = env.get("VIBEDESK_TELEGRAM_ENABLED", "").lower() in ("1", "true", "yes")

    settings = Settings(
        llm_url=env.get("VIBEDESK_LLM_URL", DEFAULT_LLM_URL),
        llm_key=env.get("VIBEDESK_LLM_KEY", ""),
        llm_model=env.get("VIBEDESK_LLM_MODEL", ""),
        daily_token_cap=daily_token_cap,
        telegram_enabled=telegram_enabled,
        telegram_chat_id=env.get("VIBEDESK_TELEGRAM_CHAT_ID", ""),
    )
    saved = root / "settings.json"
    if saved.exists():
        for name, value in json.loads(saved.read_text(encoding="utf-8")).items():
            if hasattr(settings, name) and value is not None:
                setattr(settings, name, value)
    return settings


def save(settings: Settings, root: Path = DATA) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "settings.json").write_text(json.dumps(asdict(settings), indent=2), encoding="utf-8")


def load_secrets(root: Path = DATA) -> dict[str, str]:
    """Load secrets from data/secrets.json (git-ignored) with environment fallback.

    Secrets (like telegram_bot_token) are never logged or committed.
    """
    secrets: dict[str, str] = {}
    env_token = os.environ.get("VIBEDESK_TELEGRAM_BOT_TOKEN")
    if env_token:
        secrets["telegram_bot_token"] = env_token

    path = root / "secrets.json"
    if path.exists():
        try:
            content = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(content, dict):
                secrets.update({str(k): str(v) for k, v in content.items()})
        except Exception:
            pass
    return secrets


def save_secrets(secrets: dict[str, str], root: Path = DATA) -> None:
    """Save secrets to data/secrets.json (git-ignored). Never commit or log."""
    root.mkdir(parents=True, exist_ok=True)
    existing = load_secrets(root)
    existing.update(secrets)
    (root / "secrets.json").write_text(json.dumps(existing, indent=2), encoding="utf-8")


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
