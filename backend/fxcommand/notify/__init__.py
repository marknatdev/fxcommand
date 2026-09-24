"""Notifier: delivers Alerts outside the application (Telegram), so the operator hears about them
when the dashboard is closed.

``AlertDispatcher`` listens on the EventBus for ``alert`` events (Journal entries marked as alerts,
unless their data says ``notify: False``) and ``notify`` events (e.g. the daily summary), and sends
them through a ``Notifier`` built from the current settings. Delivery runs off the event loop and
never raises into the engine; every attempt lands in a small outbox the dashboard shows.
"""

from __future__ import annotations

import asyncio
import collections
import json
import logging
import time
import urllib.error
import urllib.request
from typing import Callable, Protocol

log = logging.getLogger("fxcommand.notify")

RATE_LIMIT = 20  # messages per RATE_WINDOW seconds
RATE_WINDOW = 60.0
DEDUPE_SECONDS = 60.0


class Notifier(Protocol):
    def send(self, text: str) -> None:
        """Deliver ``text``; raise on failure."""


class TelegramNotifier:
    def __init__(self, token: str, chat_id: str, timeout: float = 10.0):
        self.token = token
        self.chat_id = chat_id
        self.timeout = timeout

    def send(self, text: str) -> None:
        body = json.dumps({"chat_id": self.chat_id, "text": text[:4000], "disable_web_page_preview": True}).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{self.token}/sendMessage", data=body, headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                ok = json.loads(r.read().decode() or "{}").get("ok", False)
        except urllib.error.HTTPError as e:  # the token must never appear in errors
            raise RuntimeError(f"Telegram answered HTTP {e.code}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise RuntimeError(f"Telegram unreachable: {getattr(e, 'reason', e)}") from None
        if not ok:
            raise RuntimeError("Telegram refused the message")


class MemoryNotifier:
    """Test adapter: keeps what it was asked to send."""

    def __init__(self):
        self.sent: list[str] = []

    def send(self, text: str) -> None:
        self.sent.append(text)


def mask(secret: str) -> str:
    return "" if not secret else ("•" * 6 + secret[-4:] if len(secret) > 8 else "•" * 6)


def telegram_from_settings(settings: dict) -> Notifier | None:
    token, chat = (settings.get("telegram_token") or "").strip(), str(settings.get("telegram_chat_id") or "").strip()
    if not settings.get("enabled", True) or not token or not chat:
        return None
    return TelegramNotifier(token, chat)


class AlertDispatcher:
    def __init__(
        self,
        store,
        bus,
        factory: Callable[[dict], Notifier | None] = telegram_from_settings,
        label: Callable[[], str] = lambda: "",
    ):
        self.store = store
        self.bus = bus
        self.factory = factory
        self.label = label
        self.outbox: collections.deque[dict] = collections.deque(maxlen=50)
        self._sent_times: collections.deque[float] = collections.deque()
        self._recent: dict[str, float] = {}
        self._suppressed = 0
        self._task: asyncio.Task | None = None
        self._queue: asyncio.Queue | None = None

    # ----------------------------------------------------------- lifecycle
    def start(self) -> None:
        if self._task is None:
            self._queue = self.bus.subscribe()
            self._task = asyncio.create_task(self._run(), name="notifier")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        if self._queue is not None:
            self.bus.unsubscribe(self._queue)

    async def _run(self) -> None:
        assert self._queue is not None
        while True:
            ev = await self._queue.get()
            try:
                text = self.text_for(ev)
                if text:
                    await self.deliver(text)
            except Exception:  # noqa: BLE001 — the notifier must never die
                log.exception("notification failed")

    # ------------------------------------------------------------- content
    def text_for(self, ev: dict) -> str | None:
        kind, d = ev.get("type"), ev.get("data") or {}
        if kind == "notify":
            return str(d.get("text") or "") or None
        if kind != "alert" or (d.get("data") or {}).get("notify") is False:
            return None
        icon = {"error": "🔴", "warn": "🟠"}.get(d.get("level"), "🔵")
        head = f"{icon} FXCommand {self.label()}".rstrip()
        sym = f" {d['symbol']}" if d.get("symbol") else ""
        return f"{head}\n[{d.get('kind')}]{sym} {d.get('message')}"

    # ------------------------------------------------------------ delivery
    def _record(self, text: str, status: str) -> dict:
        entry = {"wall": time.time(), "text": text, "status": status}
        self.outbox.appendleft(entry)
        return entry

    async def deliver(self, text: str, force: bool = False) -> dict:
        now = time.time()
        if not force:
            if now - self._recent.get(text, 0.0) < DEDUPE_SECONDS:
                return self._record(text, "skipped: duplicate")
            while self._sent_times and now - self._sent_times[0] > RATE_WINDOW:
                self._sent_times.popleft()
            if len(self._sent_times) >= RATE_LIMIT:
                self._suppressed += 1
                return self._record(text, "dropped: rate limit")
        notifier = self.factory(self.store.notify_settings())
        if notifier is None:
            return self._record(text, "skipped: not configured")
        if self._suppressed and not force:
            text = f"{text}\n(+{self._suppressed} alerts suppressed by the rate limit — see the Journal)"
            self._suppressed = 0
        self._recent[text] = now
        self._sent_times.append(now)
        try:
            await asyncio.get_running_loop().run_in_executor(None, notifier.send, text)
        except Exception as e:  # noqa: BLE001
            log.warning("notification not delivered: %s", e)
            return self._record(text, f"failed: {e}")
        return self._record(text, "sent")
