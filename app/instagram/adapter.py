"""InstagramAdapter — the ONLY module that talks to instaharvest_v2.

Verified against instaharvest-v2 1.1.x (mpython77/instaharvest_v2):
  package/class : ``pip install instaharvest-v2`` / ``from instaharvest_v2 import Instagram``
  auth          : ``Instagram.from_env()``, ``ig.login(u, p)``,
                  ``ig.save_session(path)`` / ``ig.auth.save_session/load_session``,
                  ``ig.auth.validate_session()``, ``Instagram.from_session_file(path)``
  reels feed    : ``ig.feed.get_reels_feed(count=20, cursor=None)`` -> dict
                  {posts, has_next, end_cursor, count} (GraphQL v2, REST
                  fallback may use {items, more_available, next_max_id}).
                  Verified against installed source api/feed.py — the docs'
                  ``max_id`` kwarg does NOT exist. ``get_timeline`` is home
                  feed — NOT used.
  media model   : Media.{pk, shortcode, media_type(1/2/8), video_url, video_duration, ...}
                  raw feed dicts use ``pk``/``id``, ``code``/``shortcode``.
  upload        : ``ig.upload.post_reel(video_path|video_data, thumbnail_path|
                  thumbnail_data, caption, duration, width, height)`` -> dict with
                  media pk. NO hide-like-counts parameter exists.
  download      : ``ig.download.download_media(media_pk)``; public fallback
                  ``ig.public.get_post_by_shortcode(code)`` -> video_url.
  exceptions    : ``instaharvest_v2.exceptions``: InstagramError, LoginRequired,
                  ChallengeRequired, CheckpointRequired, ConsentRequired,
                  RateLimitError, NotFoundError/MediaNotFound, NetworkError, ProxyError.

Everything else in the app uses the ReelCandidate dataclass below, never the
library's internals — so the client can be swapped later.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Callable

log = logging.getLogger(__name__)

# Feature-detection results (honest reporting, never faked).
SUPPORTS_HIDDEN_COUNTS = False  # verified: post_reel() has no such parameter
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


def normalize_reel(item: Any) -> ReelCandidate | None:
    """Normalize one feed entry. Returns None when identity is missing."""
    media_id = str(_get(item, "pk", "id", "media_id", default="") or "")
    if not media_id or media_id == "0":
        return None
    shortcode = str(_get(item, "shortcode", "code", default="") or "")
    user = _get(item, "user", "owner", default={})
    username = str(_get(user, "username", default="") or "")
    try:
        media_type = int(_get(item, "media_type", "product_type", default=2) or 2)
    except (TypeError, ValueError):
        media_type = 2
    try:
        duration = float(_get(item, "video_duration", "duration", default=0.0) or 0.0)
    except (TypeError, ValueError):
        duration = 0.0
    video_url = str(_get(item, "video_url", "video_download_url", default="") or "")
    if not video_url:  # nested video_versions fallback (raw REST payloads)
        versions = _get(item, "video_versions", default=[]) or []
        if isinstance(versions, list) and versions:
            video_url = str(_get(versions[0], "url", default="") or "")
    cap = _get(item, "caption", default="")
    caption_text = cap.get("text", "") if isinstance(cap, dict) else str(cap or "")
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
    if c.media_type in (2,):  # video / clips
        return True
    if c.raw.get("product_type") in ("clips", "reels"):
        return True
    return bool(c.video_url)  # downloadable video present


class InstagramAdapter:
    """Thin wrapper around instaharvest_v2.Instagram (lazy import)."""

    def __init__(self, session_file: str = "/tmp/igh_session.json"):
        self._ig: Any = None
        self.session_file = session_file

    # -- construction -----------------------------------------------------
    def _new_client(self) -> Any:
        try:
            from instaharvest_v2 import Instagram
        except ImportError as exc:
            raise RuntimeError(
                "instaharvest-v2 is not installed. "
                "Add 'instaharvest-v2' to requirements.txt and deploy to Render."
            ) from exc
        return Instagram()

    def load_session(self, *, username: str = "", password: str = "",
                     session_blob: str = "", env_cookies: dict | None = None) -> Any:
        """Restore session without persisting anything outside /tmp.

        Precedence: INSTAGRAM_SESSION blob (raw/base64 session.json) >
        SESSION_ID/CSRF_TOKEN/DS_USER_ID cookies via from_env > fresh login.
        """
        ig = self._new_client()
        blob = (session_blob or "").strip()
        if blob:
            data = blob
            try:  # base64-encoded session.json?
                decoded = base64.b64decode(blob).decode("utf-8")
                json.loads(decoded)
                data = decoded
            except (binascii.Error, ValueError, UnicodeDecodeError):
                pass
            if data.lstrip().startswith("{"):
                with open(self.session_file, "w") as fh:
                    fh.write(data)
                try:
                    loader = getattr(ig.auth, "load_session", None)
                    if callable(loader):
                        loader(self.session_file)
                    else:  # older API: Instagram.from_session_file
                        from instaharvest_v2 import Instagram as IG
                        ig = IG.from_session_file(self.session_file)
                    log.info("AUTH session restored from INSTAGRAM_SESSION blob")
                    return self._validated(ig)
                except Exception as exc:
                    log.warning("AUTH blob session failed: %r", exc)
            else:
                log.warning("AUTH INSTAGRAM_SESSION is not session JSON; trying cookies/login")
        try:
            from instaharvest_v2 import Instagram as IG
            if os.environ.get("SESSION_ID"):
                ig = IG.from_env()
                log.info("AUTH session loaded via from_env cookies")
                return self._validated(ig)
        except Exception as exc:
            log.warning("AUTH from_env failed: %r", exc)
        if username and password:
            ig.login(username, password)
            try:
                saver = getattr(ig, "save_session", None) or getattr(ig.auth, "save_session", None)
                if callable(saver):
                    saver(self.session_file)
            except Exception:
                pass
            log.info("AUTH fresh login completed")
            return self._validated(ig)
        raise RuntimeError(
            "No usable Instagram session: set INSTAGRAM_SESSION (session.json content), "
            "or SESSION_ID/CSRF_TOKEN/DS_USER_ID cookies, or INSTAGRAM_USERNAME/PASSWORD."
        )

    def _validated(self, ig: Any) -> Any:
        validator = getattr(getattr(ig, "auth", ig), "validate_session", None)
        if callable(validator):
            try:
                ok = validator()
                if ok is False:
                    # Some sessions validate False on one endpoint (new IP,
                    # pending checkpoint) yet still work for feed/upload.
                    # Proceed — the feed call is the real test and its
                    # LoginRequired error is classified + reported cleanly.
                    log.warning("AUTH validate_session returned False; proceeding "
                                "anyway, feed will confirm")
            except RuntimeError:
                raise
            except Exception as exc:
                log.warning("AUTH validate_session inconclusive: %r", exc)
        self._ig = ig
        return ig

    # -- reels feed (dedicated endpoint, paginated to `count`) ------------
    def get_reels(self, count: int = 30) -> list[ReelCandidate]:
        if self._ig is None:
            raise RuntimeError("Instagram client not loaded; call load_session() first")
        log.info("DISCOVERY started target=%d", count)
        items: list = []
        cursor = None
        seen_pages = 0
        # Real signature (installed source): get_reels_feed(count, cursor)
        # -> {posts, has_next, end_cursor, count}. REST fallback may use
        # {items, more_available, next_max_id} — accept both.
        while len(items) < count and seen_pages < 10:
            page = self._ig.feed.get_reels_feed(count=min(count, 20), cursor=cursor)
            if isinstance(page, dict):
                batch = page.get("posts", page.get("items", []))
            else:
                batch = []
            if not batch and hasattr(page, "items") and not isinstance(page, dict):
                items_attr = page.items  # type: ignore[union-attr]
                batch = items_attr if isinstance(items_attr, list) else []
            if not batch:
                break
            items.extend(batch)
            if isinstance(page, dict):
                more = page.get("has_next", page.get("more_available", False))
                cursor = page.get("end_cursor", page.get("next_max_id"))
            else:
                more, cursor = False, None
            seen_pages += 1
            if not more or not cursor:
                break
        out = []
        for entry in items[:count]:
            cand = normalize_reel(entry)
            if cand:
                out.append(cand)
        log.info("DISCOVERY found=%d usable=%d", len(items), len(out))
        return out

    # -- video url fallback ------------------------------------------------
    def ensure_video_url(self, cand: ReelCandidate) -> ReelCandidate:
        if cand.video_url or not cand.shortcode:
            return cand
        try:
            post = self._ig.public.get_post_by_shortcode(cand.shortcode)
            url = _get(post, "video_url", default="")
            if url:
                cand.video_url = str(url)
        except Exception as exc:
            log.warning("DISCOVERY no video_url for %s: %r", cand.shortcode, exc)
        return cand

    # -- upload ------------------------------------------------------------
    def supports_hidden_counts(self) -> bool:
        return SUPPORTS_HIDDEN_COUNTS

    def _extract_media_pk(self, result: Any) -> str:
        media = _get(result, "media", default={})
        pk = _get(media if isinstance(media, (dict,)) else result, "pk", "id", "media_id", default="")
        if not pk:
            pk = _get(result, "pk", "id", "media_id", default="")
        return str(pk or "")

    def upload_reel(self, *, video_path: str, cover_path: str | None,
                    caption: str = "", duration: float = 0.0) -> str:
        """Upload via ig.upload.post_reel(); returns destination media pk."""
        kwargs: dict[str, Any] = {"video_path": video_path, "caption": caption or ""}
        if duration:
            kwargs["duration"] = float(duration)
        if cover_path:
            kwargs["thumbnail_path"] = cover_path
        log.info("UPLOAD started video=%s cover=%s", video_path, cover_path)
        result = self._ig.upload.post_reel(**kwargs)
        pk = self._extract_media_pk(result)
        if not pk:
            raise RuntimeError(f"Upload returned no media pk: {str(result)[:300]}")
        log.info("UPLOAD success destination_media_id=%s", pk)
        return pk

    # -- error classification ----------------------------------------------
    @staticmethod
    def classify_error(exc: BaseException) -> str:
        name = type(exc).__name__
        mapping = {
            "LoginRequired": "instagram_auth",
            "ChallengeRequired": "instagram_challenge",
            "CheckpointRequired": "instagram_checkpoint",
            "ConsentRequired": "instagram_consent",
            "RateLimitError": "instagram_rate_limited",
            "NotFoundError": "instagram_not_found",
            "MediaNotFound": "instagram_not_found",
            "UserNotFound": "instagram_not_found",
            "PrivateAccountError": "instagram_private",
            "NetworkError": "network",
            "ProxyError": "proxy",
        }
        return mapping.get(name, "unknown")


def build_adapter(session_factory: Callable[[], Any] | None = None) -> InstagramAdapter:
    adapter = InstagramAdapter()
    if session_factory is not None:
        adapter._ig = session_factory()
    return adapter
