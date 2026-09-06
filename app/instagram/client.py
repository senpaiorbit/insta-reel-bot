"""Session bootstrap: env blob -> /tmp file -> adapter.load_session()."""

from __future__ import annotations

import logging
import os

from app.instagram.adapter import InstagramAdapter

log = logging.getLogger(__name__)


def create_client(settings) -> InstagramAdapter:
    adapter = InstagramAdapter()
    cookies = {
        "SESSION_ID": settings.SESSION_ID or os.environ.get("SESSION_ID", ""),
        "CSRF_TOKEN": settings.CSRF_TOKEN or os.environ.get("CSRF_TOKEN", ""),
        "DS_USER_ID": settings.DS_USER_ID or os.environ.get("DS_USER_ID", ""),
    }
    adapter.load_session(
        username=settings.INSTAGRAM_USERNAME,
        password=settings.INSTAGRAM_PASSWORD,
        session_blob=settings.INSTAGRAM_SESSION,
        env_cookies=cookies,
    )
    user = settings.INSTAGRAM_USERNAME or settings.DESTINATION_USERNAME or "?"
    log.info("AUTH ready account=%s", user)
    return adapter
