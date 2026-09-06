"""Ephemeral in-process activity ring for the /live page.

Intentionally NOT persistent: Turso (bot_runs / processed_reels) stays the
source of truth. This is just the recent tail for live viewing.
"""

from __future__ import annotations

import collections
import threading
import time

_events: collections.deque = collections.deque(maxlen=200)
_lock = threading.Lock()


def emit(msg: str) -> None:
    stamp = time.strftime("%H:%M:%S")
    with _lock:
        _events.append(f"[{stamp}] {msg}")


def recent() -> list[str]:
    with _lock:
        return list(_events)
