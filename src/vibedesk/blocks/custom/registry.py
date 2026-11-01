"""Custom Block registry and approval management."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

from vibedesk.blocks.custom.lookahead import test_custom_block_lookahead
from vibedesk.blocks.custom.validator import validate_custom_block_code
from vibedesk.blocks.registry import LIBRARY, BlockDef, BlockRegistry


@dataclass(frozen=True)
class CustomBlockMeta:
    name: str
    kind: str
    code: str
    approved: bool
    approved_at: str | None
    sha256: str
    author: str = "ai"
    block_def: BlockDef | None = None
    error: str | None = None
    verified: bool = False
    block_id: int | None = None


class CustomBlockRegistry:
    """Explicit custom library; never mutates the built-in global registry."""

    def __init__(self, ledger: Any | None = None, library: BlockRegistry | None = None):
        if library is LIBRARY:
            raise ValueError("Use an isolated BlockRegistry for custom blocks")
        self.ledger = ledger
        self.library = library
        self._blocks: dict[str, CustomBlockMeta] = {}

    def _store(self, meta: CustomBlockMeta) -> CustomBlockMeta:
        self._blocks[meta.name] = meta
        if self.library is not None:
            if meta.block_def is not None:
                self.library._defs[meta.name] = meta.block_def
            else:
                self.library._defs.pop(meta.name, None)
        return meta

    def invalidate(self, name: str) -> None:
        if name in self._blocks:
            old = self._blocks[name]
            self._store(
                replace(
                    old,
                    approved=False,
                    approved_at=None,
                    verified=False,
                    block_def=None,
                    error="Source changed; reload required",
                )
            )

    def _prepare(self, name, kind, code, author, block_def):
        digest = hashlib.sha256(code.encode("utf-8")).hexdigest()
        ok, error = validate_custom_block_code(code)
        if ok:
            try:
                if block_def is None:
                    raise ValueError("No valid BlockDef")
                if block_def.name != name or block_def.kind.value != kind:
                    raise ValueError("BlockDef name or kind mismatch")
                for field in block_def.params.model_fields.values():
                    if field.annotation in (int, float):
                        lower = any(
                            getattr(m, "ge", None) is not None or getattr(m, "gt", None) is not None
                            for m in field.metadata
                        )
                        upper = any(
                            getattr(m, "le", None) is not None or getattr(m, "lt", None) is not None
                            for m in field.metadata
                        )
                        if not lower or not upper:
                            raise ValueError("Numeric parameters require lower and upper bounds")
                test_custom_block_lookahead(block_def)
            except Exception as exc:
                error = str(exc)
        verified = ok and error is None
        block_id = None
        approved = False
        approved_at = None
        if self.ledger is not None:
            records = self.ledger.list_custom_blocks()
            record = next((r for r in records if r.name == name and r.sha256 == digest), None)
            block_id = (
                record.id if record else self.ledger.register_custom_block(name, kind, code, digest)
            )
            state = self.ledger.read_custom_block_approval_state(block_id)
            approved = verified and state["approved"]
            approved_at = state["approved_at"] if approved else None
        bdef = replace(block_def, approved=approved) if block_def is not None else None
        return CustomBlockMeta(
            name, kind, code, approved, approved_at, digest, author, bdef, error, verified, block_id
        )

    def register(
        self, name: str, kind: str, code: str, author: str = "ai", block_def: BlockDef | None = None
    ) -> CustomBlockMeta:
        if (
            name in self._blocks
            or LIBRARY.get(name) is not None
            or (self.library is not None and self.library.get(name) is not None)
        ):
            raise ValueError(
                f"Custom block {name!r} is already registered or clashes with a built-in"
            )
        return self._store(self._prepare(name, kind, code, author, block_def))

    def update_code(
        self, name: str, code: str, block_def: BlockDef | None = None
    ) -> CustomBlockMeta:
        old = self._blocks[name]
        # A new source must supply its newly loaded callable; never reuse stale code.
        self.invalidate(name)
        return self._store(self._prepare(name, old.kind, code, old.author, block_def))

    def approve(self, name: str, approver: str = "owner") -> CustomBlockMeta:
        old = self._blocks[name]
        if old.error or old.block_def is None or not old.verified:
            raise ValueError(f"Custom block {name!r} failed verification: {old.error}")
        approved_at = datetime.now(UTC).isoformat()
        if self.ledger is not None:
            state = self.ledger.read_custom_block_approval_state(old.block_id)
            if state["sha256"] != old.sha256:
                raise ValueError("Ledger code hash mismatch")
            if not state["approved"]:
                self.ledger.record_custom_block_approval(old.block_id, approver=approver)
            approved_at = self.ledger.read_custom_block_approval_state(old.block_id)["approved_at"]
        return self._store(
            replace(
                old,
                approved=True,
                approved_at=approved_at,
                block_def=replace(old.block_def, approved=True),
            )
        )

    def is_approved(self, name: str) -> bool:
        meta = self._blocks.get(name)
        if meta is None or not meta.approved or not meta.verified:
            return False
        if self.ledger is not None:
            state = self.ledger.read_custom_block_approval_state(meta.block_id)
            return bool(state["approved"] and state["sha256"] == meta.sha256)
        return True

    def get(self, name: str) -> CustomBlockMeta | None:
        return self._blocks.get(name)

    def list_blocks(self) -> list[CustomBlockMeta]:
        return list(self._blocks.values())

    def to_block_registry(self, include_unapproved: bool = False) -> BlockRegistry:
        reg = BlockRegistry()
        for block in LIBRARY.all():
            reg._defs[block.name] = block
        for meta in self._blocks.values():
            if meta.block_def is not None and (self.is_approved(meta.name) or include_unapproved):
                reg._defs[meta.name] = meta.block_def
        return reg
