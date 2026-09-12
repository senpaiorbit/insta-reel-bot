"""Telegram log system (optional, off by default).

Per-run override via ``?logbot=1`` (force) / ``?logbot=0`` (silence),
threaded as ``notify_override`` from main -> worker -> notify calls.
Never raises: failures are logged as warnings so the pipeline keeps running.
Never include secrets, session material, or caption bodies in messages.
"""

from __future__ import annotations

import json
import logging
import urllib.request

log = logging.getLogger(__name__)

TELEGRAM_API_TIMEOUT_SEC = 10
TELEGRAM_MAX_CHARS = 3500


def send_telegram(token: str, chat_id: str, text: str) -> bool:
    """POST one message via stdlib urllib. Returns True on success.

    Never raises — returns False and logs a warning on any failure.
    """
    try:
        token = (token or "").strip()
        chat_id = (chat_id or "").strip()
        if not token or not chat_id:
            return False
        text = (text or "")[:TELEGRAM_MAX_CHARS]
        payload = json.dumps({"chat_id": chat_id, "text": text}).encode("utf-8")
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=TELEGRAM_API_TIMEOUT_SEC) as resp:  # noqa: S310
            body = resp.read(4096).decode("utf-8", errors="replace")
        try:
            ok = bool(json.loads(body).get("ok", False))
        except Exception:
            ok = resp.status == 200
        if not ok:
            log.warning("TELEGRAM send rejected: %.150s", body)
        return ok
    except Exception as exc:
        log.warning("TELEGRAM send failed: %r", exc)
        return False


def notify(settings, text: str, force: bool | None = None) -> bool:
    """Send a Telegram log line honoring global switch + per-run override.

    - ``force is False`` (``?logbot=0``) always silences this run.
    - ``force is True`` (``?logbot=1``) sends even when TELEGRAM_ENABLED=false.
    - otherwise sends only when TELEGRAM_ENABLED=true.
    - no-op when token/chat are unset. Never raises.
    """
    try:
        if force is False:
            return False
        enabled = bool(getattr(settings, "TELEGRAM_ENABLED", False)) or force is True
        if not enabled:
            return False
        token = (getattr(settings, "TELEGRAM_BOT_TOKEN", "") or "").strip()
        chat_id = (getattr(settings, "TELEGRAM_CHAT_ID", "") or "").strip()
        if not token or not chat_id:
            log.warning("TELEGRAM enabled but BOT_TOKEN/CHAT_ID unset, skipping")
            return False
        return send_telegram(token, chat_id, text)
    except Exception as exc:
        log.warning("TELEGRAM notify failed: %r", exc)
        return False
