"""Application wiring: one process = one Broker, one engine, one API (see ADR 0001)."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .broker import BrokerError, BrokerThread, make_broker
from .config import Config
from .engine import DomainError, SessionManager
from .journal import EventBus, Journal, LogBuffer
from .learning.service import LearningService
from .store import NotFound, Store

log = logging.getLogger("fxcommand")


@dataclass
class Runtime:
    config: Config
    broker: BrokerThread
    store: Store
    bus: EventBus
    journal: Journal
    logs: LogBuffer
    manager: SessionManager
    learning: "LearningService"
    clock_task: asyncio.Task | None = None


async def sim_clock(rt: Runtime) -> None:
    """Drive SimBroker time: ``sim_speed`` simulated minutes per real minute (0 = stopped)."""
    while True:
        speed = float(rt.store.app_settings().get("sim_speed") or 0)
        if speed <= 0:
            await asyncio.sleep(0.5)
            continue
        await asyncio.sleep(60.0 / speed)
        try:
            await rt.broker.run(lambda b: b.step(1))
        except Exception:
            log.exception("sim clock step failed")


def sim_resume_start(last_ts: int) -> int | None:
    """The simulated market is regenerated on every start. Continue its clock on the next weekday
    at 08:00 after anything already in the database, so a new run never replays a server day that
    already holds trades (which would count against today's daily-loss limit)."""
    if last_ts <= 0:
        return None
    day = last_ts // 86400 + 1
    while (day + 3) % 7 >= 5:  # skip Saturday/Sunday
        day += 1
    return day * 86400 + 8 * 3600


def build_runtime(config: Config) -> Runtime:
    bus = EventBus()
    logs = LogBuffer(bus)
    root = logging.getLogger("fxcommand")
    root.setLevel(logging.INFO)
    if not any(isinstance(h, LogBuffer) for h in root.handlers):
        root.addHandler(logs)
    store = Store(config.db_url)
    if config.broker == "sim":
        broker = make_broker("sim", seed=config.sim_seed, start=config.sim_start or sim_resume_start(store.last_server_ts()))
        if config.sim_speed is not None:
            store.update_app_settings({"sim_speed": config.sim_speed})
    else:
        broker = make_broker("mt5", terminal_path=config.mt5_path)
    thread = BrokerThread(broker)
    journal = Journal(store, bus)
    learning = LearningService(store, thread, journal, config.broker, processes=True)
    manager = SessionManager(thread, store, journal, bus, learning)
    return Runtime(config, thread, store, bus, journal, logs, manager, learning)


def create_app(config: Config | None = None) -> FastAPI:
    config = config or Config.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        rt = build_runtime(config)
        app.state.rt = rt
        loop = asyncio.get_running_loop()
        rt.bus.attach(loop)

        def _quiet_resets(lp, ctx):
            # Windows' Proactor loop reports every browser that drops a WebSocket as an error
            if isinstance(ctx.get("exception"), ConnectionResetError):
                return
            lp.default_exception_handler(ctx)

        loop.set_exception_handler(_quiet_resets)
        log.info("FXCommand starting — broker=%s db=%s", config.broker, config.db_url)
        rt.learning.start()
        await rt.manager.boot()
        if config.run_engine:
            rt.manager.start_loop(lambda: rt.store.app_settings()["poll_interval"])
        if config.broker == "sim":
            rt.clock_task = asyncio.create_task(sim_clock(rt), name="sim-clock")
        try:
            yield
        finally:
            if rt.clock_task:
                rt.clock_task.cancel()
            await rt.manager.stop_loop()
            await rt.learning.stop()
            await asyncio.get_running_loop().run_in_executor(None, rt.broker.shutdown)
            log.info("FXCommand stopped")

    app = FastAPI(title="FXCommand", version="0.1.0", lifespan=lifespan)

    @app.exception_handler(DomainError)
    async def _domain(_: Request, e: DomainError):
        return JSONResponse({"code": e.code, "message": e.message}, status_code=e.status)

    @app.exception_handler(NotFound)
    async def _nf(_: Request, e: NotFound):
        return JSONResponse({"code": "not_found", "message": str(e)}, status_code=404)

    @app.exception_handler(BrokerError)
    async def _broker(_: Request, e: BrokerError):
        return JSONResponse({"code": "broker_error", "message": str(e)}, status_code=503)

    @app.exception_handler(ValueError)
    async def _value(_: Request, e: ValueError):
        return JSONResponse({"code": "invalid", "message": str(e)}, status_code=422)

    from .api.routes import router, ws_router

    app.include_router(router, prefix="/api")
    app.include_router(ws_router)

    if config.static_dir:
        _mount_spa(app, config.static_dir)
    return app


def _mount_spa(app: FastAPI, dist: Path) -> None:
    index = dist / "index.html"
    if (dist / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    async def spa(path: str):
        if path.startswith(("api/", "ws")):
            return JSONResponse({"code": "not_found", "message": f"/{path} not found"}, status_code=404)
        f = (dist / path).resolve()
        if path and f.is_file() and dist.resolve() in f.parents:
            return FileResponse(f)
        # the page shell must never be cached: it names the current hashed asset files
        return FileResponse(index, headers={"Cache-Control": "no-cache"})
