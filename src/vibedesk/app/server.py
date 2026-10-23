"""The Vibe Desk web app: `uv run vibedesk-web` serves it on http://127.0.0.1:8765."""

from __future__ import annotations

import argparse
import threading
import webbrowser
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from vibedesk.app import settings as config
from vibedesk.app.chats import ChatNotFound, Chats, ai_history, result_message
from vibedesk.app.desk import Desk
from vibedesk.app.llm import Llm, LlmError, OmniRoute
from vibedesk.app.research import DraftFailed, draft_spec
from vibedesk.blocks.spec import SpecInvalid
from vibedesk.market.costs import CostError

STATIC = Path(__file__).parent / "static"
PORT = 8765


class DraftIn(BaseModel):
    message: str
    chat_id: int | None = None  # None starts a new chat


class TrialIn(BaseModel):
    spec: dict
    chat_id: int | None = None
    idea: str = ""


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
    chats = Chats((Path(db).parent if db else Path(root)) / "chats.db")
    restore_old_trials(desk, chats)
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

    @app.get("/api/chats")
    def list_chats():
        return chats.list()

    @app.get("/api/chats/{chat_id}")
    def get_chat(chat_id: int):
        try:
            return chats.get(chat_id)
        except ChatNotFound as error:
            raise HTTPException(404, str(error)) from error

    @app.delete("/api/chats/{chat_id}")
    def delete_chat(chat_id: int):
        chats.delete(chat_id)
        return {"deleted": chat_id}

    @app.post("/api/draft")
    def draft(body: DraftIn):
        if not body.message.strip():
            raise HTTPException(400, "say what the idea is")
        chat_id = body.chat_id or chats.create(body.message)
        try:
            history = ai_history(chats.get(chat_id)["messages"])
        except ChatNotFound as error:
            raise HTTPException(404, str(error)) from error
        user = {"role": "user", "text": body.message}
        try:
            result = draft_spec(current_llm(), body.message, history)
        except DraftFailed as error:
            chats.append(chat_id, user, {"role": "error", "text": str(error)})
            return JSONResponse({"detail": str(error), "chat_id": chat_id}, status_code=502)
        answer = {"role": "assistant", "text": result.explanation, "spec": result.spec,
                  "combinations": result.combinations}  # fmt: skip
        chats.append(chat_id, user, answer)
        return {"chat_id": chat_id, "spec": result.spec, "explanation": result.explanation,
                "combinations": result.combinations, "attempts": result.attempts}  # fmt: skip

    @app.post("/api/trial")
    def trial(body: TrialIn):
        idea = body.idea
        if body.chat_id and not idea:
            idea = chats.get(body.chat_id)["title"]
        try:
            summary = desk.run(idea, body.spec)
        except (SpecInvalid, CostError, ValueError) as error:
            status, text = {
                SpecInvalid: (400, f"the Spec is not valid: {error}"),
                CostError: (409, f"{error}. Press Update data first."),
            }.get(type(error), (409, str(error)))
            if body.chat_id:
                chats.append(body.chat_id, {"role": "error", "text": text})
            raise HTTPException(status, text) from error
        if body.chat_id:
            chats.append(body.chat_id, result_message(summary))
        return summary

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


def restore_old_trials(desk: Desk, chats: Chats) -> None:
    """Give every test run before chats were saved a chat of its own, so it can be opened and
    carried on like any other."""
    known = chats.trial_ids()
    for row in reversed(desk.history(limit=1000)):  # oldest first
        if row["id"] in known:
            continue
        summary = desk.trial(row["id"])
        chat_id = chats.create(row["idea"] or row["name"])
        chats.append(
            chat_id,
            {"role": "user", "text": row["idea"] or row["name"]},
            {"role": "assistant", "text": "Restored from a test run before chats were saved.",
             "spec": summary["spec"], "combinations": summary["combinations"]},
            result_message(summary),
        )  # fmt: skip


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
