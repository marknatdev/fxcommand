"""The Vibe Desk web app: `uv run vibedesk-web` serves it on http://127.0.0.1:8765."""

from __future__ import annotations

import argparse
import threading
import webbrowser
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

from vibedesk.app import settings as config
from vibedesk.app.api_spec import (
    BlockLibraryOut,
    BlockOut,
    BudgetGrantIn,
    BudgetGrantOut,
    ChatIn,
    CreateFamilyIn,
    FamilyDetailOut,
    FamilySummaryOut,
    HealthOut,
    HoldoutApproveOut,
    HoldoutRequestIn,
    HoldoutRequestOut,
    HypothesisDetailOut,
    LedgerStatsOut,
    PublicSettingsOut,
    RunTrialIn,
    TrialSummaryOut,
    UpdateSettingsIn,
)
from vibedesk.app.auth import require_owner, set_owner_cookie
from vibedesk.app.chats import ChatNotFound, Chats, ai_history, result_message
from vibedesk.app.desk import Desk
from vibedesk.app.llm import Llm, LlmError, OmniRoute
from vibedesk.app.research import DraftFailed, draft_spec
from vibedesk.app.scheduler import Scheduler
from vibedesk.blocks.registry import LIBRARY
from vibedesk.blocks.spec import SpecInvalid
from vibedesk.desk.budget import TokenBudget
from vibedesk.desk.loop import ToolRegistry, desk_loop, sse_stream, starter_tools
from vibedesk.desk.tools import DeskContext, build_desk_tools
from vibedesk.gauntlet.trial import HoldoutRefused
from vibedesk.ledger.store import BudgetExhausted, HoldoutSpent, LedgerError, NotFound
from vibedesk.market.costs import CostError
from vibedesk.notify import Notice, NoticeKind, Notifier, get_notifier

STATIC = Path(__file__).parent / "static"
PORT = 8765


class DraftIn(BaseModel):
    message: str
    chat_id: int | None = None  # None starts a new chat
    family_id: int | None = None


class TrialIn(BaseModel):
    spec: dict
    chat_id: int | None = None
    idea: str = ""


class SettingsIn(BaseModel):
    llm_url: str | None = None
    llm_key: str | None = None
    llm_model: str | None = None


def create_app(
    root: Path = config.DATA,
    llm: Llm | None = None,
    db: Path | None = None,
    settings_root: Path | None = None,
    tools: ToolRegistry | None = None,
    budget: TokenBudget | None = None,
    notifier: Notifier | None = None,
    scheduler: Scheduler | None = None,
    mt5_api: Any = None,
    enable_scheduler: bool = True,
) -> FastAPI:
    """The app over the bar cache at `root`. Tests pass a fake `llm`, and may keep the Ledger
    (`db`) and settings (`settings_root`) apart from the real ones."""
    settings_root = settings_root or root
    desk = Desk(root, db)
    chats = Chats((Path(db).parent if db else Path(root)) / "chats.db")
    restore_old_trials(desk, chats)
    state = {"settings": config.load(settings_root)}

    # Budget & Notifier initialization
    budget = budget or TokenBudget(daily_cap=state["settings"].daily_token_cap)
    notifier = notifier or get_notifier(root=settings_root, settings=state["settings"])
    if scheduler is None and enable_scheduler:
        scheduler = Scheduler(root=root, desk=desk, notifier=notifier, budget=budget, api=mt5_api)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if scheduler is not None:
            scheduler.start()
        try:
            yield
        finally:
            if scheduler is not None:
                scheduler.stop()

    app = FastAPI(title="Vibe Desk", lifespan=lifespan)
    app.state.desk = desk
    app.state.budget = budget
    app.state.notifier = notifier
    app.state.scheduler = scheduler

    def current_llm() -> Llm:
        if llm is not None:
            return llm
        s = state["settings"]
        return OmniRoute(s.llm_url, s.llm_key, s.llm_model)

    def _run_desk_loop(llm_client: Llm, message: str, chat_tools: Any) -> Any:
        return desk_loop(
            llm_client,
            message,
            tools=chat_tools,
            budget=budget,
            notifier=notifier,
        )

    # --- UI Page (Sets owner cookie on 127.0.0.1:8765 per spec §8.1) ---

    @app.get("/")
    def index():
        res = FileResponse(STATIC / "index.html")
        set_owner_cookie(res)
        return res

    # --- Status & OmniRoute Models (Simple App) ---

    @app.get("/api/status")
    def status():
        return {
            "data": desk.data_status(),
            "update": desk.update.state(),
            "settings": state["settings"].public(),
        }

    @app.get("/api/models")
    def models():
        client = current_llm()
        if not isinstance(client, OmniRoute):
            return {"models": [], "error": None}
        try:
            return {"models": client.models(), "error": None}
        except LlmError as error:
            return {"models": [], "error": str(error)}

    # --- Families & Hypotheses (spec §8.1, CCC-50) ---

    @app.get("/api/families", response_model=list[FamilySummaryOut])
    def list_families():
        return [FamilySummaryOut(**f) for f in desk.list_families()]

    @app.post("/api/families", response_model=FamilyDetailOut)
    def create_family(body: CreateFamilyIn):
        if not body.idea.strip():
            raise HTTPException(400, "an Idea needs some text")
        try:
            family_id = desk.ledger.create_family(body.idea.strip())
            return FamilyDetailOut(**desk.get_family(family_id))
        except (LedgerError, ValueError) as err:
            raise HTTPException(400, str(err)) from err

    @app.get("/api/families/{family_id}", response_model=FamilyDetailOut)
    def get_family(family_id: int):
        try:
            return FamilyDetailOut(**desk.get_family(family_id))
        except NotFound as err:
            raise HTTPException(404, f"family {family_id} not found") from err

    @app.get("/api/hypotheses/{hypothesis_id}", response_model=HypothesisDetailOut)
    def get_hypothesis(hypothesis_id: int):
        try:
            return HypothesisDetailOut(**desk.get_hypothesis(hypothesis_id))
        except NotFound as err:
            raise HTTPException(404, f"hypothesis {hypothesis_id} not found") from err

    # --- Chat: Desk Loop SSE Event Stream (spec §8.1, CCC-50) ---

    @app.post("/api/families/{family_id}/chat")
    def family_chat(family_id: int, body: ChatIn):
        if not body.message.strip():
            raise HTTPException(400, "message cannot be empty")
        try:
            desk.ledger.family(family_id)
        except NotFound as err:
            raise HTTPException(404, f"family {family_id} not found") from err

        chat_tools = tools
        if chat_tools is None:
            ctx = DeskContext(
                ledger=desk.ledger,
                market=desk.market,
                family_id=family_id,
                budget=budget,
                notifier=notifier,
                root=root,
            )
            chat_tools = build_desk_tools(ctx)

        events = _run_desk_loop(current_llm(), body.message, chat_tools)
        return StreamingResponse(sse_stream(events), media_type="text/event-stream")

    # --- Trials (spec §8.1, CCC-50, CCC-55) ---

    @app.post("/api/hypotheses/{hypothesis_id}/trials", response_model=TrialSummaryOut)
    def run_hypothesis_trial(hypothesis_id: int, body: RunTrialIn):
        try:
            desk.ledger.hypothesis(hypothesis_id)
        except NotFound as err:
            raise HTTPException(404, f"hypothesis {hypothesis_id} not found") from err

        try:
            res = desk.run_hypothesis(hypothesis_id, idea=body.idea)
            if res.get("verdict") == "CANDIDATE" and notifier:
                notifier.send(
                    Notice(
                        kind=NoticeKind.CANDIDATE,
                        text=f"Trial #{res['trial_id']} reached CANDIDATE verdict",
                        data={"trial_id": res["trial_id"], "hypothesis_id": hypothesis_id},
                    )
                )
            return TrialSummaryOut(**res)
        except BudgetExhausted as err:
            raise HTTPException(409, str(err)) from err
        except (CostError, ValueError) as err:
            raise HTTPException(400, str(err)) from err
        except Exception as err:
            raise HTTPException(500, str(err)) from err

    @app.get("/api/trials/{trial_id}", response_model=TrialSummaryOut)
    def one_trial(trial_id: int):
        try:
            return TrialSummaryOut(**desk.get_trial_detail(trial_id))
        except NotFound as error:
            raise HTTPException(404, str(error)) from error

    # --- Blocks & Ledger (spec §8.1, CCC-50) ---

    @app.get("/api/blocks", response_model=BlockLibraryOut)
    def get_blocks():
        blocks = [
            BlockOut(
                name=b.name,
                kind=b.kind.value,
                description=b.__doc__ or "",
                parameters=getattr(b, "parameters", {}),
            )
            for b in LIBRARY.all()
        ]
        return BlockLibraryOut(blocks=blocks)

    @app.get("/api/ledger", response_model=LedgerStatsOut)
    def get_ledger():
        return LedgerStatsOut(**desk.ledger_stats())

    # --- Holdout (spec §8.1, CCC-50, CCC-55) ---

    @app.post(
        "/api/hypotheses/{hypothesis_id}/holdout-requests",
        response_model=HoldoutRequestOut,
    )
    def request_holdout_endpoint(hypothesis_id: int, body: HoldoutRequestIn):
        try:
            hyp = desk.ledger.hypothesis(hypothesis_id)
        except NotFound as err:
            raise HTTPException(404, str(err)) from err

        try:
            req_id = desk.request_holdout(hypothesis_id)
            req = desk.ledger.holdout_request(req_id)
            if notifier:
                notifier.send(
                    Notice(
                        kind=NoticeKind.HOLDOUT_REQUEST,
                        text=(
                            f"Holdout requested for Hypothesis #{hypothesis_id} "
                            "awaiting owner approval"
                        ),
                        data={
                            "hypothesis_id": hypothesis_id,
                            "request_id": req_id,
                            "family_id": hyp.family_id,
                        },
                    )
                )
            return HoldoutRequestOut(
                request_id=req_id,
                hypothesis_id=hypothesis_id,
                family_id=hyp.family_id,
                status="pending",
                refusal_reason=None,
                created_at=req.created_at,
            )
        except HoldoutRefused as err:
            return HoldoutRequestOut(
                request_id=0,
                hypothesis_id=hypothesis_id,
                family_id=hyp.family_id,
                status="refused",
                refusal_reason=str(err),
                created_at=datetime.now(UTC).isoformat(),
            )
        except HoldoutSpent as err:
            return HoldoutRequestOut(
                request_id=0,
                hypothesis_id=hypothesis_id,
                family_id=hyp.family_id,
                status="spent",
                refusal_reason=str(err),
                created_at=datetime.now(UTC).isoformat(),
            )

    @app.post(
        "/api/holdout-requests/{request_id}/approve",
        dependencies=[Depends(require_owner)],
        response_model=HoldoutApproveOut,
    )
    def approve_holdout_endpoint(request_id: int):
        try:
            desk.ledger.holdout_request(request_id)
        except NotFound as err:
            raise HTTPException(404, f"Holdout request {request_id} not found") from err
        try:
            grant = desk.approve_holdout(request_id)
            return HoldoutApproveOut(
                request_id=grant.request_id,
                approved=True,
                grant_token=grant.token,
                family_id=grant.family_id,
            )
        except HoldoutSpent as err:
            raise HTTPException(409, str(err)) from err
        except (HoldoutRefused, LedgerError, ValueError) as err:
            raise HTTPException(400, str(err)) from err

    # --- Budget (spec §8.1, CCC-50: Owner-only) ---

    @app.post(
        "/api/families/{family_id}/budget",
        dependencies=[Depends(require_owner)],
        response_model=BudgetGrantOut,
    )
    def extend_budget_endpoint(family_id: int, body: BudgetGrantIn):
        try:
            desk.ledger.family(family_id)
        except NotFound as err:
            raise HTTPException(404, f"Family {family_id} not found") from err
        try:
            new_budget = desk.extend_budget(family_id, amount=body.amount)
            return BudgetGrantOut(
                family_id=family_id,
                new_budget=new_budget,
                added=body.amount,
            )
        except (LedgerError, ValueError) as err:
            raise HTTPException(400, str(err)) from err

    # --- Settings: GET & PUT (spec §8.1, CCC-50: PUT is owner-only) ---

    @app.get("/api/settings", response_model=PublicSettingsOut)
    def get_public_settings():
        return PublicSettingsOut(**state["settings"].public())

    @app.put(
        "/api/settings",
        dependencies=[Depends(require_owner)],
        response_model=PublicSettingsOut,
    )
    def update_settings(body: UpdateSettingsIn):
        nonlocal notifier
        s = state["settings"]
        for name, value in body.model_dump(exclude_unset=True).items():
            if value is not None:
                if isinstance(value, str):
                    if value.strip():
                        setattr(s, name, value.strip())
                elif isinstance(value, bool | int):
                    setattr(s, name, value)
        config.save(s, settings_root)
        reloaded = config.load(settings_root)
        state["settings"] = reloaded
        if budget is not None:
            budget.daily_cap = reloaded.daily_token_cap
        notifier = get_notifier(root=settings_root, settings=reloaded)
        app.state.budget = budget
        app.state.notifier = notifier
        if scheduler is not None:
            scheduler.notifier = notifier
        return PublicSettingsOut(**reloaded.public())

    @app.post("/api/settings")
    def save_settings(body: SettingsIn):
        """Legacy simple-app settings update."""
        nonlocal notifier
        s = state["settings"]
        for name, value in body.model_dump().items():
            if value is not None:
                setattr(s, name, value.strip())
        config.save(s, settings_root)
        reloaded = config.load(settings_root)
        state["settings"] = reloaded
        if budget is not None:
            budget.daily_cap = reloaded.daily_token_cap
        notifier = get_notifier(root=settings_root, settings=reloaded)
        app.state.budget = budget
        app.state.notifier = notifier
        if scheduler is not None:
            scheduler.notifier = notifier
        return s.public()

    # --- Health (spec §8.1, CCC-50) ---

    @app.get("/api/health", response_model=HealthOut)
    def health():
        last_refresh = None
        statuses = desk.data_status()
        import_dates = [s["last_import"] for s in statuses if s.get("last_import")]
        if import_dates:
            last_refresh = max(import_dates)

        # Cached last refresh / import result without probing initialize/shutdown
        nightly_job = scheduler.jobs().get("nightly_refresh") if scheduler else None
        if desk.update.running:
            refresh_ok = True
        elif desk.update.last_ok is not None:
            refresh_ok = desk.update.last_ok
        elif nightly_job and nightly_job.last_run:
            refresh_ok = nightly_job.status != "error"
        else:
            refresh_ok = bool(mt5_api is not None or last_refresh)

        # Cached last-known OmniRoute status without probing network
        client = current_llm()
        omni_ok = True if not isinstance(client, OmniRoute) else bool(client.url and client.key)

        b_state = budget.state().value if budget else "ok"
        tokens_used = budget.used if budget else 0
        active_jobs = sum(1 for j in scheduler.jobs() if j.status == "running") if scheduler else 0

        return HealthOut(
            mt5_connected=refresh_ok,
            omniroute_connected=omni_ok,
            last_refresh=last_refresh,
            token_budget_state=b_state,
            tokens_used_today=tokens_used,
            active_jobs=active_jobs,
        )

    # --- Simple App Endpoints (Kept working per spec and instructions) ---

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

    @app.post("/api/chat")
    def chat_stream(body: DraftIn):
        if not body.message.strip():
            raise HTTPException(400, "say what the idea is")
        chat_tools = tools
        if chat_tools is None:
            family_id = body.family_id
            if family_id is None and body.chat_id is not None:
                try:
                    chat_data = chats.get(body.chat_id)
                    for msg in chat_data.get("messages", []):
                        if "trial_id" in msg:
                            try:
                                trial_row = desk.ledger.trial(msg["trial_id"])
                                hyp = desk.ledger.hypothesis(trial_row.hypothesis_id)
                                family_id = hyp.family_id
                                break
                            except Exception:
                                pass
                except Exception:
                    pass

            if family_id is not None:
                try:
                    desk.ledger.family(family_id)
                except NotFound as err:
                    raise HTTPException(404, f"family {family_id} not found") from err
                ctx = DeskContext(
                    ledger=desk.ledger,
                    market=desk.market,
                    family_id=family_id,
                    budget=budget,
                    notifier=notifier,
                    root=root,
                )
                chat_tools = build_desk_tools(ctx)
            else:
                chat_tools = starter_tools()

        events = _run_desk_loop(current_llm(), body.message, chat_tools)
        return StreamingResponse(sse_stream(events), media_type="text/event-stream")

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
        answer = {
            "role": "assistant",
            "text": result.explanation,
            "spec": result.spec,
            "combinations": result.combinations,
        }
        chats.append(chat_id, user, answer)
        return {
            "chat_id": chat_id,
            "spec": result.spec,
            "explanation": result.explanation,
            "combinations": result.combinations,
            "attempts": result.attempts,
        }

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
            {
                "role": "assistant",
                "text": "Restored from a test run before chats were saved.",
                "spec": summary["spec"],
                "combinations": summary["combinations"],
            },
            result_message(summary),
        )


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
