"""Notifier: Alerts reach the operator outside the app; delivery never disturbs trading."""

import asyncio
import sqlite3

from fxcommand.notify import AlertDispatcher, MemoryNotifier, mask, telegram_from_settings
from fxcommand.store import Store



class Boom:
    def send(self, text):
        raise RuntimeError("network down")


async def dispatcher(h, notifier):
    d = AlertDispatcher(h.store, h.bus, lambda settings: notifier, lambda: "[SIM]")
    d.start()
    await asyncio.sleep(0)
    return d


async def drain():
    for _ in range(20):
        await asyncio.sleep(0.01)


async def test_alerts_are_delivered_and_quiet_ones_skipped(h):
    mem = MemoryNotifier()
    d = await dispatcher(h, mem)
    h.journal.record("alert", "Session 'X' stopped: AUTO-STOP", ts=1, level="warn", alert=True)
    h.journal.record("alert", "Broker connection lost", ts=1, level="error", alert=True, data={"notify": False})
    h.journal.record("info", "just info", ts=1)
    await drain()
    assert len(mem.sent) == 1 and "AUTO-STOP" in mem.sent[0] and "[SIM]" in mem.sent[0]
    await d.stop()


async def test_kill_switch_reaches_the_notifier(h):
    mem = MemoryNotifier()
    d = await dispatcher(h, mem)
    await h.mgr.kill_all()
    await drain()
    assert any("KILL SWITCH" in m for m in mem.sent)
    await d.stop()


async def test_duplicates_and_rate_limit(h):
    mem = MemoryNotifier()
    d = await dispatcher(h, mem)
    for _ in range(3):
        await d.deliver("same")
    assert mem.sent == ["same"]
    for i in range(40):
        await d.deliver(f"m{i}")
    assert len(mem.sent) == 20  # RATE_LIMIT per minute
    assert any(e["status"] == "dropped: rate limit" for e in d.outbox)
    await d.stop()


async def test_failures_are_recorded_not_raised(h):
    d = await dispatcher(h, Boom())
    e = await d.deliver("hello")
    assert e["status"].startswith("failed: network down")
    await d.stop()


async def test_not_configured_is_visible(h):
    d = AlertDispatcher(h.store, h.bus)  # real Telegram factory, nothing configured
    e = await d.deliver("x")
    assert e["status"] == "skipped: not configured"


def test_masking_and_factory():
    assert mask("123456:ABCDEFGHIJ") == "••••••GHIJ" and mask("") == ""
    assert telegram_from_settings({"telegram_token": "t", "telegram_chat_id": ""}) is None
    assert telegram_from_settings({"telegram_token": "t", "telegram_chat_id": "1", "enabled": False}) is None
    assert telegram_from_settings({"telegram_token": "t", "telegram_chat_id": "1"}) is not None


async def test_daily_summary_on_day_roll(h):
    mem = MemoryNotifier()
    d = await dispatcher(h, mem)
    s = await h.session(symbols=("EURUSD", "GBPUSD"))
    await h.mgr.start(s.id)
    await h.bars(100)
    day = h.sim.now // 86400
    h.store.set_setting("day_start", {"day": day - 1, "equity": 10_000.0, "balance": 10_000.0})
    await h.mgr.tick_once()
    await drain()
    summary = [m for m in mem.sent if "daily summary" in m]
    assert summary and "Risk Gate rejections" in summary[0]
    await d.stop()


def test_old_database_gains_new_columns(tmp_path):
    db = tmp_path / "old.db"
    Store(f"sqlite:///{db}")
    con = sqlite3.connect(db)
    for table, col in (("sessions", "execution"), ("sessions", "login"), ("trades", "paper"), ("risk_profiles", "allow_min_lot")):
        con.execute(f'ALTER TABLE "{table}" DROP COLUMN "{col}"')
    con.commit()
    con.close()
    st = Store(f"sqlite:///{db}")  # an upgrade opens an older database
    cols = {r[1] for r in sqlite3.connect(db).execute("PRAGMA table_info(sessions)")}
    assert {"execution", "login"} <= cols
    assert st.to_profile(st.risk_profiles()[0]).allow_min_lot is False

