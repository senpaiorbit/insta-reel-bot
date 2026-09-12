"""Download source reels to /tmp only (never into the repo or Turso)."""

from __future__ import annotations

import logging
import os
import tempfile
import time
from pathlib import Path

import httpx

log = logging.getLogger(__name__)


def download_to_tmp(video_url: str, *, timeout: int = 60,
                    max_bytes: int = 100 * 1024 * 1024) -> str:
    if not video_url:
        raise ValueError("No video_url for this reel")
    suffix = ".mp4"
    tmp = tempfile.NamedTemporaryFile(prefix="reel_", suffix=suffix,
                                      dir="/tmp", delete=False)
    tmp_path = tmp.name
    tmp.close()
    log.info("DOWNLOAD started url=%.80s", video_url)
    size = 0
    try:
        with httpx.stream("GET", video_url, timeout=timeout,
                          headers={"User-Agent": "Mozilla/5.0"}) as resp:
            resp.raise_for_status()
            with open(tmp_path, "wb") as fh:
                for chunk in resp.iter_bytes(65536):
                    size += len(chunk)
                    if size > max_bytes:
                        raise ValueError(f"Video exceeds {max_bytes} bytes cap")
                    fh.write(chunk)
    except Exception:
        Path(tmp_path).unlink(missing_ok=True)
        raise
    log.info("DOWNLOAD success path=%s bytes=%d", tmp_path, size)
    return tmp_path


def cleanup(*paths: str | None) -> None:
    for path in paths:
        if not path:
            continue
        try:
            Path(path).unlink(missing_ok=True)
            log.info("CLEANUP removed=%s", path)
        except Exception as exc:  # noqa: BLE001 - best effort
            log.warning("CLEANUP failed path=%s err=%r", path, exc)

def sweep_stale_tmp(*, max_age_sec: int = 7200, tmp_dir: str = "/tmp") -> int:
    """Best-effort startup sweep: remove /tmp/reel_*.mp4 older than 2h.

    Render Free: 512MB RAM, ephemeral /tmp, sleeps. Never raises; returns
    the number of files removed.
    """
    removed = 0
    try:
        now = time.time()
        root = Path(tmp_dir)
        if not root.is_dir():
            return 0
        for path in root.glob("reel_*.mp4"):
            try:
                if not path.is_file():
                    continue
                try:
                    age = now - path.stat().st_mtime
                except OSError:
                    continue
                if age > max_age_sec:
                    path.unlink(missing_ok=True)
                    removed += 1
            except Exception as exc:  # noqa: BLE001 - best effort per file
                log.warning("SWEEP failed path=%s err=%r", path, exc)
        if removed:
            log.info("SWEEP removed=%d stale reel tmp files", removed)
    except Exception as exc:  # noqa: BLE001 - sweep must never raise
        log.warning("SWEEP skipped err=%r", exc)
    return removed
