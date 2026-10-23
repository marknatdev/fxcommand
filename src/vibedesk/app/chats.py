"""Conversations: what the owner said, the AI's Specs and the test results, kept so an old chat
can be opened again and carried on. Stored in their own SQLite file, apart from the Ledger."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path

TITLE_LENGTH = 70


class ChatNotFound(LookupError):
    pass


class Chats:
    """Each chat holds a list of messages:
    - {"role": "user", "text"}
    - {"role": "assistant", "text", "spec", "combinations"}: the AI's explanation and its Spec
    - {"role": "result", "trial_id", "verdict", "text", "note"}: a test of a Spec; `note` is
      what the AI is told about it
    - {"role": "error", "text"}: shown on the page, never sent to the AI
    """

    def __init__(self, path: Path) -> None:
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS chats (id INTEGER PRIMARY KEY, title TEXT NOT NULL,"
            " created_at TEXT NOT NULL, updated_at TEXT NOT NULL, verdict TEXT,"
            " messages TEXT NOT NULL DEFAULT '[]')"
        )

    def create(self, title: str) -> int:
        now = _now()
        title = " ".join(title.split())[:TITLE_LENGTH] or "New idea"
        with self._lock:
            return self._db.execute(
                "INSERT INTO chats (title, created_at, updated_at) VALUES (?, ?, ?)",
                (title, now, now),
            ).lastrowid

    def list(self) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT id, title, updated_at, verdict FROM chats ORDER BY updated_at DESC, id DESC"
            ).fetchall()
        return [dict(row) for row in rows]

    def get(self, chat_id: int) -> dict:
        with self._lock:
            row = self._db.execute("SELECT * FROM chats WHERE id = ?", (chat_id,)).fetchone()
        if row is None:
            raise ChatNotFound(f"no chat {chat_id}")
        return {**dict(row), "messages": json.loads(row["messages"])}

    def append(self, chat_id: int, *messages: dict) -> None:
        with self._lock:
            row = self._db.execute(
                "SELECT messages, verdict FROM chats WHERE id = ?", (chat_id,)
            ).fetchone()
            if row is None:
                raise ChatNotFound(f"no chat {chat_id}")
            stored = json.loads(row["messages"]) + list(messages)
            verdicts = [m["verdict"] for m in messages if m.get("role") == "result"]
            self._db.execute(
                "UPDATE chats SET messages = ?, updated_at = ?, verdict = ? WHERE id = ?",
                (json.dumps(stored), _now(), verdicts[-1] if verdicts else row["verdict"],
                 chat_id),
            )  # fmt: skip

    def delete(self, chat_id: int) -> None:
        with self._lock:
            self._db.execute("DELETE FROM chats WHERE id = ?", (chat_id,))

    def trial_ids(self) -> set[int]:
        with self._lock:
            rows = self._db.execute("SELECT messages FROM chats").fetchall()
        return {m["trial_id"] for row in rows for m in json.loads(row["messages"])
                if m.get("role") == "result"}  # fmt: skip


def ai_history(messages: list[dict]) -> list[dict]:
    """The chat as the AI sees it: errors left out, a test result told as the owner's words."""
    history = []
    for m in messages:
        if m["role"] == "user":
            history.append({"role": "user", "content": m["text"]})
        elif m["role"] == "assistant":
            spec = json.dumps(m["spec"])
            history.append({"role": "assistant", "content": f"{m['text']}\n```json\n{spec}\n```"})
        elif m["role"] == "result":
            history.append({"role": "user", "content": m["note"]})
    return history


def result_message(summary: dict) -> dict:
    """The chat message for a test, from the page's summary of it."""
    m = summary["metrics"]
    text = (
        f"{summary['label']}. {m['trades']} trades on the blind years, net {_f(m['net_r'], 1)} R, "
        f"profit factor {_f(m['profit_factor'])}. Tell me what to change, or describe a new idea."
    )
    return {
        "role": "result",
        "trial_id": summary["id"],
        "verdict": summary["verdict"],
        "text": text,
        "note": result_note(summary),
    }


def result_note(summary: dict) -> str:
    m, years = summary["metrics"], summary["years"]
    win = None if m["win_rate"] is None else m["win_rate"] * 100
    failed = [
        f"{c['name']} {_f(c['value'])} (needs {'<=' if c['at_most'] else '>='} "
        f"{_f(c['threshold'])})"
        for c in summary["checks"]
        if str(c["status"]).lower() == "fail"
    ]
    yearly = zip(years, summary["year_net_r"], strict=True)
    per_year = ", ".join(f"{year} {_f(net, 1)}" for year, net in yearly)
    return (
        f'Test result of "{summary["spec"]["name"]}": {summary["verdict"]}. Blind years '
        f"{years[0]}-{years[-1]}: {m['trades']} trades, net {_f(m['net_r'], 1)} R, profit factor "
        f"{_f(m['profit_factor'])}, win rate {_f(win, 0)}%, max drawdown "
        f"{_f(m['max_drawdown_r'], 1)} R. Net R per year: {per_year}. Failed checks: "
        f"{'; '.join(failed) or 'none'}. If I ask for changes, build on this Spec."
    )


def _f(value, digits: int = 2) -> str:
    return "n/a" if value is None else f"{value:.{digits}f}"


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
