"""InstagramAdapter — the ONLY module that talks to instaharvest_v2.

Verified against installed instaharvest-v2 source (not just docs):
  reels feed : ``ig.feed.get_reels_feed(count=20, cursor=None)``
                -> {posts, has_next, end_cursor, count}.
  media model: ``Media`` has NO ``video_url`` field — video lives in
                ``video_versions`` / ``best_video_url``. See _best_video_url().
  upload     : ``ig.upload.post_reel(video_path, thumbnail_path, caption,
                duration, width, height)`` -> dict with media pk.
  views      : ``ig.media.get_info(pk)`` -> Media with play_count/view_count.
  archive    : MediaAPI has NO archive method (checked 1.1.x) — archiving
                goes through its HttpClient directly: POST
                media/{id}/only_me/ (same private endpoint instagrapi and
                instagram_private_api's media_only_me() use). Three request
                shapes are tried; first {"status":"ok"} wins.
  pin        : instaharvest-v2's pin_comment() posts to
                media/{id}/comment/{cid}/pin/ which 404s persistently
                (verified live); the working shape per instagrapi source
                (mixins/comment.py::comment_pin) is
                POST media/{id}/pin_comment/{cid}/ with trailing slash.
  upstream bug: installed GraphQL/users/client internals call bare
                ``build_request_headers()`` — patched at runtime, see
                ``patch_missing_library_imports()``.

Everything else in the app uses the ReelCandidate dataclass below, never the
library's internals — so the client can be swapped later.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import urlparse

from app.instagram import totp as _totp

log = logging.getLogger(__name__)

PROXY_SCHEMES = ("http", "https", "socks5", "socks5h")

def proxy_host_for_log(proxy_url: str) -> str:
    """Host (never credentials) for safe logging."""
    try:
        parsed = urlparse((proxy_url or "").strip())
        host = parsed.hostname or ""
        if parsed.port:
            host = f"{host}:{parsed.port}"
        return host or "(unparseable)"
    except Exception:
        return "(unparseable)"

def is_valid_proxy(proxy_url: str) -> bool:
    """Scheme must be http/https/socks5/socks5h with a host."""
    try:
        parsed = urlparse((proxy_url or "").strip())
        return parsed.scheme.lower() in PROXY_SCHEMES and bool(parsed.hostname)
    except Exception:
        return False

def apply_proxy_env(proxy_url: str) -> bool:
    """Set HTTP(S)_PROXY (and ALL_PROXY for socks) for libraries without
    a proxy constructor param. Returns True when applied, False (direct)
    when empty/invalid. Logs host only, never credentials."""
    proxy_url = (proxy_url or "").strip()
    if not proxy_url:
        return False
    if not is_valid_proxy(proxy_url):
        log.warning("PROXY invalid scheme/host, running direct (host=%s)",
                    proxy_host_for_log(proxy_url))
        return False
    scheme = urlparse(proxy_url).scheme.lower()
    os.environ["HTTP_PROXY"] = proxy_url
    os.environ["HTTPS_PROXY"] = proxy_url
    if scheme in ("socks5", "socks5h"):
        os.environ["ALL_PROXY"] = proxy_url
    log.info("PROXY enabled host=%s scheme=%s", proxy_host_for_log(proxy_url), scheme)
    return True

# -- stealth URL helpers (stdlib only: re + urllib) ----------------------
# Additive-only: used for shortcode normalization + yt-dlp fallback.
_SHORTCODE_PATH_RE = re.compile(
    r"/(?:reel|reels|p|tv|share)/([A-Za-z0-9_-]{2,})", re.IGNORECASE)
_SHORTCODE_PARAM_RE = re.compile(
    r"(?:media_id|media-id|shortcode|code)=([A-Za-z0-9_-]{2,})", re.IGNORECASE)


def is_instagram_url(url: str) -> bool:
    """True for instagram http(s) links, /embed paths, or instagram:// URIs."""
    raw = (url or "").strip()
    if not raw:
        return False
    if raw.startswith("instagram://"):
        return True
    try:
        parsed = urlparse(raw if "://" in raw else f"https://{raw}")
    except Exception:
        return False
    host = (parsed.hostname or "").lower()
    if not host:
        return "instagram.com" in raw.lower() or "instagr.am" in raw.lower()
    if host == "instagr.am" or host.endswith(".instagr.am"):
        return True
    if host == "instagram.com" or host.endswith(".instagram.com"):
        return True
    path = (parsed.path or "").lower()
    if "/embed" in path and ("instagram" in raw.lower()):
        return True
    return False


def extract_shortcode_from_url(url: str) -> str:
    """Shortcode from /reel/|/reels/|/p/|/tv/|/share/|embed|instagram://.

    Never raises; returns "" when no shortcode is present.
    """
    raw = (url or "").strip()
    if not raw:
        return ""
    try:
        if raw.startswith("instagram://"):
            m = _SHORTCODE_PATH_RE.search(raw)
            if m:
                return m.group(1).split("?")[0].split("&")[0]
            m2 = _SHORTCODE_PARAM_RE.search(raw)
            if m2:
                return m2.group(1)
            tail = raw.rstrip("/").split("/")[-1]
            tail = tail.split("?")[0].split("&")[0]
            if re.fullmatch(r"[A-Za-z0-9_-]{2,}", tail or ""):
                return tail
            return ""
        path_hit = _SHORTCODE_PATH_RE.search(raw)
        if path_hit:
            code = path_hit.group(1).split("?")[0].split("&")[0].strip()
            return code.rstrip("/")
        if "/embed" in raw.lower():
            try:
                parsed = urlparse(raw)
                segs = [s for s in (parsed.path or "").split("/") if s]
                for seg in reversed(segs):
                    if re.fullmatch(r"[A-Za-z0-9_-]{2,}", seg or "") and seg.lower() not in (
                            "embed", "reel", "reels", "p", "tv", "share"):
                        return seg
            except Exception:
                pass
        return ""
    except Exception:
        return ""
