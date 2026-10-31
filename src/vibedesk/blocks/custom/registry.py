"""Custom Block registry and approval management."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC
from typing import Any


@dataclass(frozen=True)
class CustomBlockMeta:
    """Metadata and code record for an AI-authored block."""

    name: str
    kind: str
    code: str
    approved: bool
    approved_at: str | None
    sha256: str
    author: str = "ai"


class CustomBlockRegistry:
    """Registry managing AI-authored custom blocks in blocks/custom/."""

    def __init__(self, ledger: Any | None = None) -> None:
        self.ledger = ledger
        self._blocks: dict[str, CustomBlockMeta] = {}

    def register(
        self,
        name: str,
        kind: str,
        code: str,
        author: str = "ai",
    ) -> CustomBlockMeta:
        """Register a new unapproved custom block."""
        if name in self._blocks:
            raise ValueError(f"Custom block {name!r} is already registered")

        sha256 = hashlib.sha256(code.encode("utf-8")).hexdigest()
        meta = CustomBlockMeta(
            name=name,
            kind=kind,
            code=code,
            approved=False,
            approved_at=None,
            sha256=sha256,
            author=author,
        )
        self._blocks[name] = meta

        if self.ledger is not None and hasattr(self.ledger, "_db"):
            try:
                with self.ledger._lock:
                    self.ledger._db.execute(
                        "INSERT INTO custom_blocks (name, kind, code, sha256) VALUES (?, ?, ?, ?)",
                        (name, kind, code, sha256),
                    )
                    self.ledger._db.commit()
            except Exception:
                pass

        return meta

    def approve(self, name: str, approver: str = "owner") -> CustomBlockMeta:
        """Approve a custom block for use in Trials (owner action only, ADR 0012)."""
        if name not in self._blocks:
            raise KeyError(f"Custom block {name!r} not found")

        old = self._blocks[name]
        from datetime import datetime

        now_iso = datetime.now(UTC).isoformat()
        approved_meta = CustomBlockMeta(
            name=old.name,
            kind=old.kind,
            code=old.code,
            approved=True,
            approved_at=now_iso,
            sha256=old.sha256,
            author=old.author,
        )
        self._blocks[name] = approved_meta

        if self.ledger is not None and hasattr(self.ledger, "_db"):
            try:
                with self.ledger._lock:
                    cur = self.ledger._db.cursor()
                    cur.execute("SELECT id FROM custom_blocks WHERE name = ?", (name,))
                    row = cur.fetchone()
                    if row:
                        block_id = row[0]
                        self.ledger._db.execute(
                            "INSERT INTO custom_block_approvals (block_id, approver) VALUES (?, ?)",
                            (block_id, approver),
                        )
                        self.ledger._db.commit()
            except Exception:
                pass

        return approved_meta

    def get(self, name: str) -> CustomBlockMeta | None:
        return self._blocks.get(name)

    def list_blocks(self) -> list[CustomBlockMeta]:
        return list(self._blocks.values())
