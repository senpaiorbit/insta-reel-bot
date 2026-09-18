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

# Upstream bug (installed instaharvest-v2): several modules call bare
# ``build_request_headers(...)`` without importing it — the real function
# lives in ``instaharvest_v2.http_utils``. Symptom: NameError on GraphQL /
# users / client calls, which feed methods swallow into empty results.
# We pre-import every plausible submodule and inject the genuine function
# into ALL loaded instaharvest_v2 modules that lack it (harmless unused).
_PATCH_CANDIDATES = (
    "instaharvest_v2.api.graphql",
    "instaharvest_v2.api.users",
    "instaharvest_v2.api.transport",
    "instaharvest_v2.api.feed",
    "instaharvest_v2.api.account",
    "instaharvest_v2.api.media",
    "instaharvest_v2.api.async_graphql",
    "instaharvest_v2.api.async_users",
    "instaharvest_v2.graphql",
    "instaharvest_v2.users",
    "instaharvest_v2.transport",
    "instaharvest_v2.feed",
    "instaharvest_v2.account",
    "instaharvest_v2.client",
)

def patch_missing_library_imports() -> int:
    """Inject http_utils.build_request_headers everywhere it is missing.

    Returns number of modules patched. Never raises (no-op if the library
    is absent or already fixed upstream).
    """
    try:
        import importlib
        import sys as _sys
        from instaharvest_v2 import http_utils
        func = http_utils.build_request_headers
    except Exception as exc:
        log.warning("COMPAT http_utils unavailable, skipping patches: %r", exc)
        return 0
    for mod_name in _PATCH_CANDIDATES:
        try:
            importlib.import_module(mod_name)
        except Exception:
            continue
    patched = 0
    for name, mod in list(_sys.modules.items()):
        if name == "instaharvest_v2.http_utils":
            continue
        if name == "instaharvest_v2" or name.startswith("instaharvest_v2."):
            try:
                if not hasattr(mod, "build_request_headers"):
                    setattr(mod, "build_request_headers", func)
                    patched += 1
            except Exception:
                continue
    if patched:
        log.info("COMPAT patched %d modules with build_request_headers", patched)
    return patched

# Feature-detection results (honest reporting, never faked).
SUPPORTS_HIDDEN_COUNTS = False  # verified: post_reel() has no such parameter
SUPPORTS_SHARE_TO_FEED = False  # verified: post_reel() has no such parameter;
# share-to-feed is applied via uploader.upload_reel, which sets
# clips_share_preview_to_feed on its own configure_to_clips call.
SUPPORTS_REEL_COVER = True  # via thumbnail_path / thumbnail_data on post_reel()
REELS_FEED_SIGNATURE = "get_reels_feed(count=20, cursor=None) -> {posts, has_next, end_cursor, count}"

@dataclass
class ReelCandidate:
    source_media_id: str
    shortcode: str = ""
    username: str = ""
    video_url: str = ""
    duration: float = 0.0
    media_type: int = 2
    caption_text: str = ""
    raw: dict = field(default_factory=dict)

def _get(obj: Any, *names: str, default: Any = "") -> Any:
    """Read attr-or-key across Pydantic models and plain dicts."""
    for name in names:
        if isinstance(obj, dict):
            if obj.get(name) not in (None, ""):
                return obj[name]
        else:
            val = getattr(obj, name, None)
            if val not in (None, ""):
                return val
            try:
                val = obj.get(name)  # type: ignore[union-attr]
                if val not in (None, ""):
                    return val
            except Exception:
                pass
    return default

def _coerce_width(obj: Any) -> int:
    """Width as int; handles int-or-string, '1080p', width_px, etc."""
    for key in ("width", "width_px", "w"):
        try:
            raw = obj.get(key) if isinstance(obj, dict) else getattr(obj, key, None)
        except Exception:
            raw = None
        if raw in (None, ""):
            continue
        try:
            if isinstance(raw, str):
                m = re.search(r"\d+", raw)
                if not m:
                    continue
                return int(m.group(0))
            return int(raw)
        except (TypeError, ValueError):
            continue
    return 0


def _is_image_url(url: str) -> bool:
    try:
        path = urlparse((url or "").split("?")[0]).path.lower()
    except Exception:
        path = (url or "").lower()
    return path.endswith((".jpg", ".jpeg", ".png", ".webp"))


def _best_video_url(obj: Any) -> str:
    """Extract the best video URL from Media models, parsed dicts, or versions.

    The Media model has NO ``video_url`` field — video lives in
    ``video_versions`` (or the ``best_video_url`` computed property).
    Hardened: dicts/objects, width coercion, image URLs (.jpg/.webp)
    kept only as a last resort, yt-dlp ``formats[]`` supported.
    """
    for name in ("video_url", "video_download_url", "best_video_url"):
        direct = _get(obj, name, default="")
        if direct:
            return str(direct)
    # yt-dlp info dicts sometimes carry the file directly.
    if isinstance(obj, dict):
        direct_dl = obj.get("url") if isinstance(obj.get("url"), str) else ""
        if direct_dl and not _is_image_url(direct_dl):
            return str(direct_dl)
    versions = _get(obj, "video_versions", default=[]) or []
    formats = _get(obj, "formats", default=[]) or []
    pooled: list = []
    if isinstance(versions, list):
        pooled.extend(versions)
    elif isinstance(versions, dict):
        pooled.append(versions)
    if isinstance(formats, list):
        # yt-dlp formats: skip video-less (vcodec == "none") entries.
        for f in formats:
            try:
                vc = f.get("vcodec") if isinstance(f, dict) else getattr(f, "vcodec", None)
            except Exception:
                vc = None
            if isinstance(vc, str) and vc.lower() == "none":
                continue
            pooled.append(f)
    best, best_w = "", -1
    image_fallback, image_w = "", -1
    for ver in pooled:
        url = str(_get(ver, "url", "src", default="") or "")
        if not url:
            continue
        width = _coerce_width(ver)
        if _is_image_url(url):
            if width >= image_w:
                image_fallback, image_w = url, width
            continue
        if width >= best_w:
            best, best_w = url, width
    if best:
        return best
    return image_fallback

def _coerce_duration(item: Any) -> float:
    """Duration from video_duration/duration/length; int/str/dict-safe."""
    for key in ("video_duration", "duration", "length", "clip_duration"):
        try:
            raw = item.get(key) if isinstance(item, dict) else getattr(item, key, None)
        except Exception:
            raw = None
        if raw in (None, ""):
            continue
        try:
            if isinstance(raw, dict):
                raw = raw.get("value", raw.get("duration", 0))
            if isinstance(raw, str):
                m = re.search(r"\d+(?:\.\d+)?", raw)
                if not m:
                    continue
                return float(m.group(0))
            val = float(raw)
            if val:
                return val
        except (TypeError, ValueError):
            continue
    return 0.0


def _caption_text(item: Any) -> str:
    """Caption text from dict/object/plain-string captions. Never raises."""
    try:
        for key in ("caption_text", "caption", "text"):
            try:
                cap = item.get(key) if isinstance(item, dict) else getattr(item, key, None)
            except Exception:
                cap = None
            if cap in (None, ""):
                continue
            if isinstance(cap, dict):
                txt = cap.get("text", cap.get("caption", ""))
                if txt:
                    return str(txt)
                continue
            try:
                nested = getattr(cap, "text", None)
            except Exception:
                nested = None
            if nested:
                return str(nested)
            if isinstance(cap, str):
                return cap
            try:
                as_str = str(cap)
                if as_str and as_str not in ("{}", "None"):
                    return as_str
            except Exception:
                continue
        return ""
    except Exception:
        return ""


def normalize_reel(item: Any) -> ReelCandidate | None:
    """Normalize one feed entry. Returns None when identity is missing."""
    media_id = str(_get(item, "pk", "id", "media_id", default="") or "")
    if not media_id or media_id == "0":
        return None
    shortcode = str(_get(item, "shortcode", "code", default="") or "").strip()
    # URL shortcode normalize: derive from link/share/embed URLs when the
    # code field itself is missing or is a full URL.
    if not shortcode or "/" in shortcode or shortcode.startswith("http"):
        for key in ("share_url", "link", "url", "embed_url", "permalink"):
            try:
                cand = item.get(key) if isinstance(item, dict) else getattr(item, key, None)
            except Exception:
                cand = None
            if cand:
                code = extract_shortcode_from_url(str(cand))
                if code:
                    shortcode = code
                    break
        if "/" in shortcode or shortcode.startswith("http"):
            shortcode = extract_shortcode_from_url(shortcode)
    shortcode = (shortcode or "").strip().rstrip("/")
    user = _get(item, "user", "owner", default={})
    username = str(_get(user, "username", default="") or "")
    # NOTE: media_type stays numeric (int-or-string "2"); product_type
    # ("clips"/"CLIPS") is a separate string signal checked in
    # is_reel_video() — never conflated into this int.
    try:
        media_type = int(str(_get(item, "media_type", default=2) or 2).strip())
    except (TypeError, ValueError, AttributeError):
        media_type = 2
    duration = _coerce_duration(item)
    video_url = _best_video_url(item)
    caption_text = _caption_text(item)
    raw = item if isinstance(item, dict) else getattr(item, "to_dict", lambda: {})()
    try:
        raw = dict(raw)
    except Exception:
        raw = {}
    return ReelCandidate(
        source_media_id=media_id, shortcode=shortcode, username=username,
        video_url=video_url, duration=duration, media_type=media_type,
        caption_text=caption_text[:500], raw=raw,
    )

def is_reel_video(c: ReelCandidate) -> bool:
    # int-or-string media_type == 2 covers video/clips.
    try:
        if int(str(c.media_type).strip()) == 2:
            return True
    except (TypeError, ValueError, AttributeError):
        pass
    try:
        raw_mt = c.raw.get("media_type") if isinstance(getattr(c, "raw", None), dict) else None
        if raw_mt is not None and str(raw_mt).strip() == "2":
            return True
    except Exception:
        pass
    # Case-insensitive product_type signal (kept separate from media_type).
    try:
        pt = c.raw.get("product_type") if isinstance(getattr(c, "raw", None), dict) else ""
        if isinstance(pt, str) and pt.strip().lower() in ("clips", "clip", "reels", "reel"):
            return True
    except Exception:
        pass
    # Video payload present (feed entries without product_type set).
    try:
        vv = c.raw.get("video_versions") if isinstance(getattr(c, "raw", None), dict) else None
        if isinstance(vv, list) and any(
                bool(v.get("url") if isinstance(v, dict) else getattr(v, "url", None))
                for v in vv):
            return True
    except Exception:
        pass
    return bool(c.video_url)  # downloadable video present

def _resolve_with_ytdlp(shortcode: str) -> str:
    """Step-4 video resolver via yt-dlp. Never raises; "" on any failure.

    Lazy-imports yt_dlp so environments without it (or offline CI) simply
    skip this resolver and keep the harvest paths as the source of truth.
    """
    code = (shortcode or "").strip().rstrip("/")
    if not code or "/" in code or " " in code:
        return ""
    try:
        import yt_dlp  # type: ignore[import-not-found]
    except Exception as exc:
        log.debug("RESOLVE yt-dlp unavailable, skipping: %r", exc)
        return ""
    url = f"https://www.instagram.com/reel/{code}/"
    opts = {"quiet": True, "no_warnings": True, "skip_download": True,
            "noplaylist": True, "socket_timeout": 30, "retries": 1}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:  # type: ignore[attr-defined]
            info = ydl.extract_info(url, download=False)
        if not isinstance(info, dict):
            return ""
        best = _best_video_url(info)
        if best:
            log.info("RESOLVE yt-dlp ok shortcode=%s", code)
            return str(best)
        direct = info.get("url")
        if isinstance(direct, str) and direct.startswith("http"):
            return direct
        return ""
    except Exception as exc:
        log.warning("RESOLVE yt-dlp failed shortcode=%s: %r", code, exc)
        return ""


def _get_instagrapi_client(adapter: Any) -> Any | None:
    """Logged-in instagrapi client or None. Never raises."""
    try:
        cached = getattr(adapter, "_insta_g", None)
        if cached is not None:
            return cached
        from instagrapi import Client as _IgClient  # type: ignore[import-not-found]
        username = (getattr(adapter, "_username", "") or "").strip()
        password = (getattr(adapter, "_password", "") or "").strip()
        if not username or not password:
            return None
        client = _IgClient()
        proxy_url = (getattr(adapter, "_proxy_url", "") or "").strip()
        if proxy_url:
            try:
                client.set_proxy(proxy_url)
            except Exception:
                pass
        client.login(username, password)
        try:
            adapter._insta_g = client
        except Exception:
            pass
        log.info("INSTAGRAPI session ready account=%s", username)
        return client
    except Exception as exc:
        log.debug("INSTAGRAPI unavailable, harvest fallback: %r", exc)
        return None

class InstagramAdapter:
    """Thin wrapper around instaharvest_v2.Instagram (lazy import)."""

    def __init__(self, session_file: str = "/tmp/igh_session.json"):
        self._ig: Any = None
        self.session_file = session_file
        self._insta_g: Any = None
        self._username: str = ""
        self._password: str = ""

    def _new_client(self, proxy_url: str = "") -> Any:
        try:
            from instaharvest_v2 import Instagram
        except ImportError as exc:
            raise RuntimeError(
                "instaharvest-v2 is not installed. "
                "Add 'instaharvest-v2' to requirements.txt and deploy to Render."
            ) from exc
        patch_missing_library_imports()
        proxy_url = (proxy_url or "").strip()
        if proxy_url:
            if not is_valid_proxy(proxy_url):
                log.warning("PROXY invalid scheme/host, running direct (host=%s)",
                            proxy_host_for_log(proxy_url))
            else:
                try:
                    import inspect as _inspect
                    params = _inspect.signature(Instagram.__init__).parameters
                    names = {n.lower() for n in params}
                    for cand in ("proxy", "proxy_url", "proxies", "http_proxy"):
                        if cand in names:
                            real = next(n for n in params if n.lower() == cand)
                            log.info("PROXY enabled host=%s via ctor param=%s",
                                     proxy_host_for_log(proxy_url), real)
                            return Instagram(**{real: proxy_url})
                except Exception as exc:
                    log.warning("PROXY ctor probe failed, using env fallback: %r", exc)
                apply_proxy_env(proxy_url)
                return Instagram()
        return Instagram()
