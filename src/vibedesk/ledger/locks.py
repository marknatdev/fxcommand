"""Synchronization locks for the Ledger: per-family serialization (CCC-55)."""

from __future__ import annotations

import threading

_FAMILY_LOCKS: dict[int, threading.Lock] = {}
_LOCKS_LOCK = threading.Lock()


def family_lock(family_id: int) -> threading.Lock:
    """Acquire or create a per-family lock to serialize Trial runs and freeze N (CCC-55)."""
    with _LOCKS_LOCK:
        if family_id not in _FAMILY_LOCKS:
            _FAMILY_LOCKS[family_id] = threading.Lock()
        return _FAMILY_LOCKS[family_id]
