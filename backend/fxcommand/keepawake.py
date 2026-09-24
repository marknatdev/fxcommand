"""Keep Windows awake while a Session is active (SetThreadExecutionState). No-op elsewhere.

``ES_CONTINUOUS`` binds the request to the calling thread, so this must always be called from the
same long-lived thread — the app calls it from the asyncio event loop thread after every engine pass.
"""

from __future__ import annotations

import sys

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001

_state: bool | None = None


def set_awake(active: bool) -> None:
    global _state
    if active == _state or sys.platform != "win32":
        return
    import ctypes

    flags = ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if active else 0)
    ctypes.windll.kernel32.SetThreadExecutionState(flags)
    _state = active
