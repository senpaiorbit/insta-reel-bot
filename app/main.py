"""FastAPI service: /health, /status, POST /upload (UptimeRobot)."""

from __future__ import annotations

import logging
import secrets
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from app import login_flow
from app import login_page
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


def _authorized(authorization: str | None) -> bool:
    if not authorization or not authorization.startswith("Bearer "):
        return False
    token = authorization[len("Bearer "):].strip()
    return bool(token) and secrets.compare_digest(token, settings.UPLOAD_SECRET)


@app.get("/")
@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/status")
def status():
    try:
        info = repo.counts(get_db())
    except Exception as exc:
        return {"status": "degraded", "error": str(exc)[:300]}
    return {"status": "ok", **info}


@app.post("/upload")
def upload(authorization: str | None = Header(default=None)):
    if not _authorized(authorization):
        raise HTTPException(status_code=401, detail="unauthorized")
    if _thread_lock.locked():
        return JSONResponse({"status": "busy"}, status_code=429)
    db = get_db()
    # Database-level distributed lock (mandatory for multi-instance).
    try:
        if not repo.acquire_lock(db, ttl_sec=settings.UPLOAD_LOCK_TIMEOUT_SEC):
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
        from app.instagram.client import create_client
        from app.worker import run_once
        adapter = create_client(settings)
        result = run_once(settings=settings, db=db, adapter=adapter)
        code = 200 if result.get("status") in ("success", "no_new_reel") else 500
        return JSONResponse(result, status_code=code)
    finally:
        _thread_lock.release()
        try:
            repo.release_lock(db)
        except Exception:
            pass


# ---------------- interactive login helper ----------------

class LoginStart(BaseModel):
    username: str
    password: str
    email: str = ""  # Gmail for auto-verifying email checkpoints
    app_password: str = ""  # Gmail app password (Google Account -> 2FA -> app passwords)


class LoginCode(BaseModel):
    job_id: str
    code: str


@app.get("/login")
def login_ui():
    """Terminal-style page: enter IG credentials, watch log, get session JSON."""
    return HTMLResponse(login_page.LOGIN_HTML,
                        headers={"Cache-Control": "no-store, max-age=0"})


@app.post("/login/start")
def login_start(body: LoginStart, authorization: str | None = Header(default=None)):
    if not _authorized(authorization):
        raise HTTPException(status_code=401, detail="unauthorized")
    if not body.username or not body.password:
        raise HTTPException(status_code=400, detail="username and password required")
    job_id = login_flow.start_job(body.username.strip(), body.password,
                                    body.email.strip(), body.app_password)
    return {"job_id": job_id}


@app.get("/login/status/{job_id}")
def login_status(job_id: str, authorization: str | None = Header(default=None)):
    if not _authorized(authorization):
        raise HTTPException(status_code=401, detail="unauthorized")
    job = login_flow.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="unknown or expired job")
    return {
        "status": job.status,
        "logs": job.logs,
        "challenge": {"type": job.challenge_type, "contact": job.contact_point}
        if job.status == "awaiting_code" else None,
        "error": job.error,
        "account_username": job.account_username,
    }


@app.post("/login/code")
def login_code(body: LoginCode, authorization: str | None = Header(default=None)):
    if not _authorized(authorization):
        raise HTTPException(status_code=401, detail="unauthorized")
    if not login_flow.submit_code(body.job_id, body.code):
        raise HTTPException(status_code=404, detail="unknown job or no code requested")
    return {"ok": True}


@app.get("/login/session/{job_id}")
def login_session(job_id: str, authorization: str | None = Header(default=None)):
    if not _authorized(authorization):
        raise HTTPException(status_code=401, detail="unauthorized")
    job = login_flow.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="unknown or expired job")
    if job.status != "done" or not job.session_json:
        raise HTTPException(status_code=409, detail=f"session not ready (status={job.status})")
    return {"session": job.session_json, "account_username": job.account_username}
