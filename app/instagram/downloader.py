"""Download source reels to /tmp only (never into the repo or Turso)."""

from __future__ import annotations

import logging
import tempfile
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
