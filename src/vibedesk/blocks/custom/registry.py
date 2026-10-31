"""Custom Block registry and approval management."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from vibedesk.blocks.registry import LIBRARY, BlockDef, BlockRegistry


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
    block_def: BlockDef | None = None
    error: str | None = None


class CustomBlockRegistry:
    """Registry managing AI-authored custom blocks in blocks/custom/ (spec §8, ADR 0012)."""

    def __init__(
        self,
        ledger: Any | None = None,
        library: BlockRegistry | None = LIBRARY,
    ) -> None:
        self.ledger = ledger
        self.library = library
        self._blocks: dict[str, CustomBlockMeta] = {}

    def _is_ledger_approved(self, name: str, sha256: str) -> tuple[bool, str | None]:
        """Check if this code hash was approved in the Ledger (Migration 4)."""
        if self.ledger is not None and hasattr(self.ledger, "_db"):
            try:
                with self.ledger._lock:
                    cur = self.ledger._db.cursor()
                    cur.execute(
                        """
                        SELECT a.approved_at FROM custom_block_approvals a
                        JOIN custom_blocks b ON a.block_id = b.id
                        WHERE b.name = ? AND b.sha256 = ?
                        ORDER BY a.id DESC LIMIT 1
                        """,
                        (name, sha256),
                    )
                    row = cur.fetchone()
                    if row:
                        return True, str(row[0])
            except Exception:
                pass
        return False, None

    def register(
        self,
        name: str,
        kind: str,
        code: str,
        author: str = "ai",
        block_def: BlockDef | None = None,
    ) -> CustomBlockMeta:
        """Register a new custom block.

        Unapproved by default unless the code SHA-256 matches an existing Ledger approval.
        """
        if name in self._blocks:
            raise ValueError(f"Custom block {name!r} is already registered")

        sha256 = hashlib.sha256(code.encode("utf-8")).hexdigest()
        is_approved, approved_at = self._is_ledger_approved(name, sha256)

        meta = CustomBlockMeta(
            name=name,
            kind=kind,
            code=code,
            approved=is_approved,
            approved_at=approved_at,
            sha256=sha256,
            author=author,
            block_def=block_def,
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

        if is_approved and block_def is not None and self.library is not None:
            self.library._defs[name] = block_def

        return meta

    def update_code(
        self,
        name: str,
        code: str,
        block_def: BlockDef | None = None,
    ) -> CustomBlockMeta:
        """Update code for an existing custom block.

        A changed file (hash differs) requires approval again (spec §8.3, ADR 0012).
        """
        if name not in self._blocks:
            raise KeyError(f"Custom block {name!r} not found")

        old = self._blocks[name]
        new_sha256 = hashlib.sha256(code.encode("utf-8")).hexdigest()

        # Check if the new hash is approved in the Ledger
        is_approved, approved_at = self._is_ledger_approved(name, new_sha256)

        new_bdef = block_def or old.block_def

        meta = CustomBlockMeta(
            name=old.name,
            kind=old.kind,
            code=code,
            approved=is_approved,
            approved_at=approved_at,
            sha256=new_sha256,
            author=old.author,
            block_def=new_bdef,
        )
        self._blocks[name] = meta

        if not is_approved and self.library is not None:
            self.library._defs.pop(name, None)
        elif is_approved and new_bdef is not None and self.library is not None:
            self.library._defs[name] = new_bdef

        if self.ledger is not None and hasattr(self.ledger, "_db"):
            try:
                with self.ledger._lock:
                    self.ledger._db.execute(
                        "INSERT INTO custom_blocks (name, kind, code, sha256) VALUES (?, ?, ?, ?)",
                        (name, old.kind, code, new_sha256),
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
        now_iso = datetime.now(UTC).isoformat()
        approved_meta = CustomBlockMeta(
            name=old.name,
            kind=old.kind,
            code=old.code,
            approved=True,
            approved_at=now_iso,
            sha256=old.sha256,
            author=old.author,
            block_def=old.block_def,
        )
        self._blocks[name] = approved_meta

        if self.ledger is not None and hasattr(self.ledger, "_db"):
            try:
                with self.ledger._lock:
                    cur = self.ledger._db.cursor()
                    cur.execute(
                        "SELECT id FROM custom_blocks WHERE name = ? AND sha256 = ? "
                        "ORDER BY id DESC LIMIT 1",
                        (name, old.sha256),
                    )
                    row = cur.fetchone()
                    if row:
                        block_id = row[0]
                    else:
                        self.ledger._db.execute(
                            "INSERT INTO custom_blocks (name, kind, code, sha256) "
                            "VALUES (?, ?, ?, ?)",
                            (name, old.kind, old.code, old.sha256),
                        )
                        block_id = cur.lastrowid

                    self.ledger._db.execute(
                        "INSERT INTO custom_block_approvals (block_id, approver) VALUES (?, ?)",
                        (block_id, approver),
                    )
                    self.ledger._db.commit()
            except Exception:
                pass

        if old.block_def is not None and self.library is not None:
            self.library._defs[name] = old.block_def

        return approved_meta

    def revoke(self, name: str) -> CustomBlockMeta:
        """Revoke approval for a custom block."""
        if name not in self._blocks:
            raise KeyError(f"Custom block {name!r} not found")

        old = self._blocks[name]
        revoked = CustomBlockMeta(
            name=old.name,
            kind=old.kind,
            code=old.code,
            approved=False,
            approved_at=None,
            sha256=old.sha256,
            author=old.author,
            block_def=old.block_def,
        )
        self._blocks[name] = revoked
        if self.library is not None:
            self.library._defs.pop(name, None)
        return revoked

    def is_approved(self, name: str) -> bool:
        """Return True if block exists, is marked approved, and current hash matches."""
        meta = self._blocks.get(name)
        if meta is None or not meta.approved:
            return False
        current_hash = hashlib.sha256(meta.code.encode("utf-8")).hexdigest()
        return current_hash == meta.sha256

    def get(self, name: str) -> CustomBlockMeta | None:
        return self._blocks.get(name)

    def list_blocks(self) -> list[CustomBlockMeta]:
        return list(self._blocks.values())

    def to_block_registry(self, include_unapproved: bool = False) -> BlockRegistry:
        """Construct a BlockRegistry view containing built-ins and custom blocks."""
        reg = BlockRegistry()
        if self.library is not None:
            for b in self.library.all():
                reg._defs[b.name] = b

        for meta in self._blocks.values():
            if meta.block_def is not None and (meta.approved or include_unapproved):
                reg._defs[meta.name] = meta.block_def
        return reg
