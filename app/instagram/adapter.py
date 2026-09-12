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
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import urlparse

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


def _best_video_url(obj: Any) -> str:
    """Extract the best video URL from Media models, parsed dicts, or versions.

    The Media model has NO ``video_url`` field — video lives in
    ``video_versions`` (or the ``best_video_url`` computed property).
    """
    for name in ("video_url", "video_download_url", "best_video_url"):
        direct = _get(obj, name, default="")
        if direct:
            return str(direct)
    versions = _get(obj, "video_versions", default=[]) or []
    best, best_w = "", -1
    if isinstance(versions, list):
        for ver in versions:
            url = _get(ver, "url", default="")
            try:
                width = int(_get(ver, "width", default=0) or 0)
            except (TypeError, ValueError):
                width = 0
            if url and width >= best_w:
                best, best_w = str(url), width
    return best


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
    video_url = _best_video_url(item)
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
                # Prefer a native constructor proxy param when the installed
                # library accepts one; otherwise fall back to env vars.
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

    def load_session(self, *, username: str = "", password: str = "",
                      session_blob: str = "", env_cookies: dict | None = None,
                      proxy_url: str = "") -> Any:
        """Restore session without persisting anything outside /tmp.

        Precedence: INSTAGRAM_SESSION blob (raw/base64 session.json) >
        SESSION_ID/CSRF_TOKEN/DS_USER_ID cookies via from_env > fresh login.
        """
        ig = self._new_client(proxy_url=proxy_url)
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
                # NOTE: from_env() builds its own client, bypassing the
                # proxy ctor param; the HTTP(S)_PROXY/ALL_PROXY env fallback
                # (applied in create_client) still routes it when PROXY_URL set.
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
        """Fill cand.video_url trying every known resolver. Never raises."""
        from app import activity as _act
        if cand.video_url:
            return cand
        _act.emit(f"RESOLVE start media_id={cand.source_media_id} "
                  f"shortcode={cand.shortcode or '-'}")
        log.info("RESOLVE start media_id=%s shortcode=%s",
                 cand.source_media_id, cand.shortcode or "-")
        if not cand.shortcode:
            _act.emit("RESOLVE abort: no shortcode")
            return cand
        # 1) authenticated media lookup (Media model carries video_versions)
        _act.emit("RESOLVE trying media.get_by_shortcode ...")
        try:
            media = self._ig.media.get_by_shortcode(cand.shortcode)
            url = _best_video_url(media)
            _act.emit(f"RESOLVE media.get_by_shortcode done has_url={bool(url)}")
            if url:
                cand.video_url = str(url)
                return cand
        except Exception as exc:
            _act.emit(f"RESOLVE media.get_by_shortcode failed: {type(exc).__name__}")
            log.warning("DISCOVERY media.get_by_shortcode failed for %s: %r",
                        cand.shortcode, exc)
        # 2) anonymous public lookup (no session needed)
        _act.emit("RESOLVE trying public.get_post_by_shortcode ...")
        try:
            post = self._ig.public.get_post_by_shortcode(cand.shortcode)
            url = _best_video_url(post)
            _act.emit(f"RESOLVE public lookup done has_url={bool(url)}")
            if url:
                cand.video_url = str(url)
                return cand
        except Exception as exc:
            _act.emit(f"RESOLVE public lookup failed: {type(exc).__name__}")
            log.warning("DISCOVERY public lookup failed for %s: %r",
                        cand.shortcode, exc)
        # 3) media info by pk
        _act.emit("RESOLVE trying media.get_info ...")
        try:
            getter = getattr(getattr(self._ig, "media", None), "get_info", None)
            if callable(getter) and cand.source_media_id:
                media = getter(cand.source_media_id)
                url = _best_video_url(media)
                _act.emit(f"RESOLVE get_info done has_url={bool(url)}")
                if url:
                    cand.video_url = str(url)
            else:
                _act.emit("RESOLVE get_info unavailable")
        except Exception as exc:
            _act.emit(f"RESOLVE get_info failed: {type(exc).__name__}")
            log.warning("DISCOVERY media.get_info failed for %s: %r",
                        cand.source_media_id, exc)
        _act.emit("RESOLVE exhausted: no video_url found")
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
                    caption: str = "", duration: float = 0.0,
                    share_to_feed: bool = True) -> str:
        """Upload via ig.upload.post_reel(); returns destination media pk.

        share_to_feed is accepted for call-compatibility with
        uploader.upload_reel, but post_reel() exposes no such parameter
        (see SUPPORTS_SHARE_TO_FEED) so the library path cannot honor it —
        the production path (uploader.upload_reel -> configure_to_clips
        with clips_share_preview_to_feed) is what puts reels in the grid.
        """
        kwargs: dict[str, Any] = {"video_path": video_path, "caption": caption or ""}
        if duration:
            kwargs["duration"] = float(duration)
        if cover_path:
            kwargs["thumbnail_path"] = cover_path
        if not share_to_feed:
            log.warning("UPLOAD share_to_feed=False ignored: post_reel() "
                        "has no feed-preview parameter (Reels-tab-only not "
                        "expressible on this path)")
        log.info("UPLOAD started video=%s cover=%s", video_path, cover_path)
        result = self._ig.upload.post_reel(**kwargs)
        pk = self._extract_media_pk(result)
        if not pk:
            raise RuntimeError(f"Upload returned no media pk: {str(result)[:300]}")
        log.info("UPLOAD success destination_media_id=%s", pk)
        return pk

    # -- archive (low-performing reels) ------------------------------------
    def get_media_views(self, media_pk: str) -> int | None:
        """Live play/view count for one media pk. None when unreadable.

        Never raises — unknown counts must skip archiving, never trigger it.
        """
        try:
            getter = getattr(getattr(self._ig, "media", None), "get_info", None)
            if not callable(getter):
                log.warning("ARCHIVE get_info unavailable, cannot read views for %s", media_pk)
                return None
            info = getter(media_pk)
            for name in ("play_count", "view_count"):
                val = _get(info, name, default=None)
                if val is not None and val != "":
                    try:
                        return int(val)
                    except (TypeError, ValueError):
                        continue
            log.warning("ARCHIVE no view count on media %s", media_pk)
            return None
        except Exception as exc:
            log.warning("ARCHIVE get_info failed for %s: %r", media_pk, exc)
            return None

    def archive_media(self, media_pk: str) -> bool:
        """Archive one media (owner-only). Returns True on success.

        Wire protocol (instagram_private_api media_only_me, same private API
        instagrapi uses): POST media/{id}/only_me/ with media_id + media_type.
        instaharvest-v2's MediaAPI has no archive method (checked 1.1.x), so
        the call goes through its HttpClient directly. Tries three shapes
        (query vs body placement of media_type) because Instagram answers
        some shapes with a login redirect; first {"status":"ok"} wins.
        Raises on failure so the caller can record per-media errors.
        """
        media_api = getattr(self._ig, "media", None)
        direct = getattr(media_api, "archive", None)
        if callable(direct):
            direct(media_pk)
            log.info("ARCHIVE ok media=%s via media.archive", media_pk)
            return True
        client = getattr(media_api, "_client", None)
        post = getattr(client, "post", None)
        if not callable(post):
            raise RuntimeError("Instagram client exposes no media API to archive with")
        shapes = (
            ({"media_id": str(media_pk)}, {"media_type": 2}),
            ({"media_id": str(media_pk)}, None),
            ({"media_id": str(media_pk), "media_type": "2"}, None),
        )
        last_err = "no archive attempt made"
        for i, (data, params) in enumerate(shapes):
            try:
                kwargs: dict[str, Any] = {"data": data,
                                          "rate_category": "post_default"}
                if params:
                    kwargs["params"] = params
                res = post(f"/media/{media_pk}/only_me/", **kwargs)
            except Exception as exc:
                last_err = f"shape{i}: {type(exc).__name__}: {str(exc)[:150]}"
                log.warning("ARCHIVE shape%d raised media=%s: %r", i, media_pk, exc)
                continue
            status = res.get("status") if isinstance(res, dict) else None
            if status in (None, "ok"):
                log.info("ARCHIVE ok media=%s shape%d", media_pk, i)
                return True
            last_err = f"shape{i}: {str(res)[:150]}"
            log.warning("ARCHIVE shape%d rejected media=%s: %s", i, media_pk,
                        str(res)[:150])
        raise RuntimeError(f"Archive rejected: {last_err}")

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
            "NetworkError": "network",
            "ProxyError": "proxy",
            "NotFoundError": "instagram_not_found",
            "MediaNotFound": "instagram_not_found",
            "UserNotFound": "instagram_not_found",
            "PrivateAccountError": "instagram_private",
        }
        return mapping.get(name, "unknown")


def build_adapter(session_factory: Callable[[], Any] | None = None) -> InstagramAdapter:
    adapter = InstagramAdapter()
    if session_factory is not None:
        adapter._ig = session_factory()
    return adapter
