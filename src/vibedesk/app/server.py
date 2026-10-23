"""The Vibe Desk web app: `uv run vibedesk-web` serves it on http://127.0.0.1:8765."""

from __future__ import annotations

import argparse
import threading
import webbrowser
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from vibedesk.app import settings as config
from vibedesk.app.desk import Desk
from vibedesk.app.llm import Llm, LlmError, OmniRoute
from vibedesk.app.research import DraftFailed, draft_spec
from vibedesk.blocks.spec import SpecInvalid
from vibedesk.market.costs import CostError

STATIC = Path(__file__).parent / "static"
PORT = 8765


class DraftIn(BaseModel):
    message: str
    history: list[dict] = []


class TrialIn(BaseModel):
    idea: str = ""
    spec: dict


class SettingsIn(BaseModel):
    llm_url: str | None = None
    llm_key: str | None = None
    llm_model: str | None = None


def create_app(root: Path = config.DATA, llm: Llm | None = None, db: Path | None = None,
               settings_root: Path | None = None) -> FastAPI:  # fmt: skip
    """The app over the bar cache at `root`. Tests pass a fake `llm`, and may keep the Ledger
    (`db`) and settings (`settings_root`) apart from the real ones."""
    app = FastAPI(title="Vibe Desk")
    desk = Desk(root, db)
    settings_root = settings_root or root
    state = {"settings": config.load(settings_root)}

    def current_llm() -> Llm:
        if llm is not None:
            return llm
        s = state["settings"]
        return OmniRoute(s.llm_url, s.llm_key, s.llm_model)

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/api/status")
    def status():
        return {"data": desk.data_status(), "update": desk.update.state(),
                "settings": state["settings"].public()}  # fmt: skip

    @app.get("/api/models")
    def models():
        client = current_llm()
        if not isinstance(client, OmniRoute):
            return {"models": [], "error": None}
        try:
            return {"models": client.models(), "error": None}
        except LlmError as error:
            return {"models": [], "error": str(error)}

    @app.post("/api/settings")
    def save_settings(body: SettingsIn):
        s = state["settings"]
        for name, value in body.model_dump().items():
            if value is not None:
                setattr(s, name, value.strip())
        config.save(s, settings_root)
        return s.public()

    @app.post("/api/draft")
    def draft(body: DraftIn):
        if not body.message.strip():
            raise HTTPException(400, "say what the idea is")
        try:
            result = draft_spec(current_llm(), body.message, body.history)
        except DraftFailed as error:
            raise HTTPException(502, str(error)) from error
        return {"spec": result.spec, "explanation": result.explanation,
                "combinations": result.combinations, "attempts": result.attempts}  # fmt: skip

    @app.post("/api/trial")
    def trial(body: TrialIn):
        try:
            return desk.run(body.idea, body.spec)
        except SpecInvalid as error:
            raise HTTPException(400, f"the Spec is not valid: {error}") from error
        except CostError as error:
            raise HTTPException(409, f"{error}. Press Update data first.") from error
        except ValueError as error:
            raise HTTPException(409, str(error)) from error

    @app.get("/api/trials")
    def trials():
        return desk.history()

    @app.get("/api/trials/{trial_id}")
    def one_trial(trial_id: int):
        try:
            return desk.trial(trial_id)
        except Exception as error:
            raise HTTPException(404, str(error)) from error

    @app.post("/api/data/update")
    def update_data():
        return {"started": desk.update.start(), **desk.update.state()}

    return app


def main() -> None:
    import uvicorn

    parser = argparse.ArgumentParser(prog="vibedesk-web")
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--open", action="store_true", help="open the browser once it's up")
    args = parser.parse_args()
    if args.open:
        threading.Timer(2.0, webbrowser.open, (f"http://127.0.0.1:{args.port}",)).start()
    uvicorn.run(create_app(), host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
