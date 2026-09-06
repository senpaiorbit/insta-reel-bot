"""FastAPI service: /health, /status, /upload, /live activity, /api/debug."""

from __future__ import annotations

import logging
import secrets
import sys
import threading
import time
import traceback
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse

from app import activity
from app import live_page
from app.config import get_settings
from app.database import repository as repo
from app.database.db import TursoClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("reelbot")

settings = get_settings()
try:
    logging.getLogger().setLevel(settings.LOG_LEVEL.upper())
except Exception:
    pass

_db: TursoClient | None = None
_thread_lock = threading.Lock()  # application-level guard (same instance)


def get_db() -> TursoClient:
    global _db
    if _db is None:
        _db = TursoClient(settings.TURSO_DATABASE_URL, settings.TURSO_AUTH_TOKEN,
                          timeout=settings.HTTP_TIMEOUT_SEC)
        repo.init_schema(_db)
    return _db


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        get_db()  # create tables idempotently on startup
        log.info("STARTUP schema ready")
    except Exception as exc:
        log.error("STARTUP schema init failed (will retry per-request): %r", exc)
    yield


app = FastAPI(title="Instagram Reel Bot", lifespan=lifespan)


def _tb() -> list[str]:
    """Traceback footprints (filenames + lines only, no values/secrets)."""
    out = []
    for frame in traceback.extract_tb(sys.exc_info()[2])[-4:]:
        out.append(f"{frame.filename.split('/')[-1]}:{frame.lineno}:{frame.name}")
    return out


def _shape(obj: object, depth: int = 0) -> object:
    """Key-shape of nested data: key names + value type names only."""
    if isinstance(obj, dict):
        if depth > 1:
            return {"keys": sorted(obj.keys())[:20]}
        return {k: _shape(v, depth + 1) for k, v in list(obj.items())[:25]}
    if isinstance(obj, list):
        return {"list_len": len(obj),
                "item": _shape(obj[0], depth + 1) if obj else None}
    return type(obj).__name__


def _authorized(authorization: str | None = None, token: str | None = None) -> bool:
    """Bearer header OR ?token= query param (simple UptimeRobot/browser use)."""
    if authorization and authorization.startswith("Bearer "):
        cand = authorization[len("Bearer "):].strip()
        if cand and secrets.compare_digest(cand, settings.UPLOAD_SECRET):
            return True
    if token and secrets.compare_digest(token, settings.UPLOAD_SECRET):
        return True
    return False


def parse_hide_like(value: str | None, default: bool) -> bool:
    """Parse ?hide_like=. Absent -> default (auto-hide ON)."""
    if value is None or value == "":
        return default
    return value.strip().lower() not in ("0", "false", "no", "off")


@app.get("/")
@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/ping")
@app.head("/ping")
def ping():
    """Ultra-light keep-alive for UptimeRobot: no DB, no Instagram, instant.

    Ping every 5 min to beat Render Free's ~15 min sleep. Use a SEPARATE
    monitor from /upload so wake-ups never trigger uploads.
    """
    return {"status": "ok"}


@app.get("/live")
def live():
    """Live activity page. Open as /live?token=YOUR_SECRET and watch runs."""
    return HTMLResponse(live_page.LIVE_HTML,
                        headers={"Cache-Control": "no-store, max-age=0"})


@app.get("/api/activity")
def api_activity(token: str | None = None,
                 authorization: str | None = Header(default=None)):
    if not _authorized(authorization, token):
        raise HTTPException(status_code=401, detail="unauthorized")
    try:
        info = repo.counts(get_db())
    except Exception as exc:
        return {"events": activity.recent(), "last_run": None,
                "db_error": str(exc)[:200],
                "service_time": time.strftime("%H:%M:%S")}
    return {"events": activity.recent(), "last_run": info.get("last_run"),
            "processed_completed": info.get("processed_completed"),
            "service_time": time.strftime("%H:%M:%S")}


@app.get("/api/debug")
def api_debug(token: str | None = None,
              authorization: str | None = Header(default=None),
              media_pk: str | None = None):
    """Token-gated Instagram probe. With media_pk: lean existence check only."""
    if not _authorized(authorization, token):
        raise HTTPException(status_code=401, detail="unauthorized")
    out: dict = {}
    try:
        from app.instagram.client import create_client
        adapter = create_client(settings)
        out["auth"] = "ok"
        if media_pk:
            try:
                getter = getattr(getattr(adapter._ig, "media", None), "get_info", None)
                info = getter(media_pk) if callable(getter) else None
                code = getattr(info, "code", None) if info is not None else None
                if code is None and isinstance(info, dict):
                    code = info.get("code")
                out["media_verify"] = {
                    "pk": media_pk,
                    "exists": info is not None,
                    "code": code,
                }
            except Exception as exc:
                out["media_verify"] = {
                    "pk": media_pk,
                    "exists": False,
                    "error": f"{type(exc).__name__}: {str(exc)[:200]}",
                }
            return out
        try:
            me = adapter._ig.account.get_current_user()
            out["account"] = getattr(me, "username", None) or "unknown"
        except Exception as exc:
            out["account"] = f"unreadable: {type(exc).__name__}: {str(exc)[:200]}"
        for name, call in (
            ("reels_feed", lambda: adapter._ig.feed.get_reels_feed(count=12)),
            ("timeline", lambda: adapter._ig.feed.get_timeline(count=5)),
        ):
            try:
                raw = call()
                if isinstance(raw, dict):
                    posts = raw.get("posts", raw.get("items", []))
                    out[name] = {
                        "keys": sorted(raw.keys())[:15],
                        "posts": len(posts),
                        "has_next": raw.get("has_next", raw.get("more_available")),
                    }
                else:
                    out[name] = {"type": type(raw).__name__}
            except Exception as exc:
                out[name] = {"error": f"{type(exc).__name__}: {str(exc)[:300]}"}
        gql = getattr(adapter._ig, "graphql", None)
        if gql is None:
            out["graphql_layer"] = "missing"
        else:
            try:
                raw = gql.get_reels_trending_v2(count=12)
                posts = raw.get("posts", []) if isinstance(raw, dict) else []
                out["graphql_reels"] = {"posts": len(posts)}
            except Exception as exc:
                out["graphql_reels"] = {
                    "error": f"{type(exc).__name__}: {str(exc)[:300]}",
                    "tb": _tb()}
        try:
            raw = adapter._ig.feed.get_reels_feed(count=3)
            posts = raw.get("posts", raw.get("items", [])) if isinstance(raw, dict) else []
            if posts:
                out["reel_shape"] = _shape(posts[0])
                code = posts[0].get("shortcode") or posts[0].get("code") or ""
                if code:
                    for pname, pcall in (
                        ("media_by_shortcode",
                         lambda: adapter._ig.media.get_by_shortcode(code)),
                        ("public_by_shortcode",
                         lambda: adapter._ig.public.get_post_by_shortcode(code)),
                    ):
                        try:
                            detail = pcall()
                            det = detail.to_dict() if hasattr(detail, "to_dict") else detail
                            keys = sorted(det.keys())[:30] if isinstance(det, dict) else []
                            has_video = bool(
                                (det.get("video_url") if isinstance(det, dict) else ""))
                            out[pname] = {"keys": keys, "has_video_url": has_video}
                        except Exception as exc:
                            out[pname] = {
                                "error": f"{type(exc).__name__}: {str(exc)[:200]}"}
            else:
                out["reel_shape"] = {"empty": True}
        except Exception as exc:
            out["reel_shape"] = {
                "error": f"{type(exc).__name__}: {str(exc)[:200]}"}
        who = settings.DESTINATION_USERNAME or settings.INSTAGRAM_USERNAME or ""
        if who:
            try:
                user = adapter._ig.users.get_by_username(who)
                out["self_profile"] = {
                    "username": getattr(user, "username", None),
                    "followers": getattr(user, "followers", None),
                }
            except Exception as exc:
                out["self_profile"] = {
                    "error": f"{type(exc).__name__}: {str(exc)[:300]}",
                    "tb": _tb()}
        else:
            out["self_profile"] = {"skipped": "no username configured"}
    except Exception as exc:
        out["auth"] = f"failed: {type(exc).__name__}: {str(exc)[:300]}"
    return out


@app.api_route("/upload", methods=["GET", "POST"])
def upload(token: str | None = None,
           hide_like: str | None = None,
           authorization: str | None = Header(default=None)):
    """Trigger one upload cycle. GET works from a browser address bar.
    hide_like: 1 (default) hides like/view counts, 0 leaves them visible."""
    if not _authorized(authorization, token):
        raise HTTPException(status_code=401, detail="unauthorized")
    hide = parse_hide_like(hide_like, settings.HIDE_LIKE_VIEW_COUNTS)
    if _thread_lock.locked():
        activity.emit("BUSY — overlapping /upload call rejected")
        return JSONResponse({"status": "busy"}, status_code=429)
    db = get_db()
    try:
        if not repo.acquire_lock(db, ttl_sec=settings.UPLOAD_LOCK_TIMEOUT_SEC):
            activity.emit("BUSY — distributed lock held, rejecting")
            return JSONResponse({"status": "busy"}, status_code=429)
    except Exception as exc:
        log.error("LOCK acquire failed: %r", exc)
        return JSONResponse({"status": "failed", "error": "lock_unavailable"},
                            status_code=503)
    if not _thread_lock.acquire(blocking=False):
        try:
            repo.release_lock(db)
        except Exception:
            pass
        return JSONResponse({"status": "busy"}, status_code=429)
    try:
        activity.emit(f"UPLOAD request accepted — starting pipeline (hide_like={int(hide)})")
        from app.instagram.client import create_client
        from app.worker import run_once
        adapter = create_client(settings)
        result = run_once(settings=settings, db=db, adapter=adapter,
                          hide_counts=hide)
        code = 200 if result.get("status") in ("success", "no_new_reel") else 500
        return JSONResponse(result, status_code=code)
    finally:
        _thread_lock.release()
        try:
            repo.release_lock(db)
        except Exception:
            pass
