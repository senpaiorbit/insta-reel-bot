"""Interactive login jobs powering the GET /login web helper.

Flow: start_job(username, password) -> background thread runs
``ig.login()`` with a ``challenge_callback``. When Instagram demands a
verification code, the callback parks the job in ``awaiting_code`` and the
browser UI collects the code via POST /login/code. On success the session is
saved (instaharvest-v2 NATIVE format) and its content is handed back so it can
be pasted into the INSTAGRAM_SESSION env var.

Security: jobs live only in process memory with a 30-min TTL. Usernames are
logged, passwords NEVER are — the password reference is dropped as soon as the
login call is issued. All HTTP routes are Bearer-gated (see main.py).
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable

log = logging.getLogger(__name__)

JOB_TTL_SEC = 1800
CODE_TIMEOUT_SEC = 600
MAX_LOG_LINES = 500


@dataclass
class LoginJob:
    id: str
    status: str = "running"  # running | awaiting_code | done | failed
    logs: list = field(default_factory=list)
    challenge_type: str = ""
    contact_point: str = ""
    session_json: str = ""
    account_username: str = ""
    error: str = ""
    created_at: float = field(default_factory=time.time)
    _code: str | None = None
    _code_event: threading.Event = field(default_factory=threading.Event)


_jobs: dict[str, LoginJob] = {}
_lock = threading.Lock()


def _push(job: LoginJob, msg: str) -> None:
    stamp = time.strftime("%H:%M:%S")
    job.logs.append(f"[{stamp}] {msg}")
    if len(job.logs) > MAX_LOG_LINES:
        job.logs = job.logs[-MAX_LOG_LINES:]
    log.info("LOGIN %s %s", job.id[:8], msg)


def _purge() -> None:
    now = time.time()
    for jid in [k for k, j in _jobs.items() if now - j.created_at > JOB_TTL_SEC]:
        _jobs.pop(jid, None)


def start_job(username: str, password: str, email: str = "",
              app_password: str = "") -> str:
    _purge()
    job = LoginJob(id=uuid.uuid4().hex)
    with _lock:
        _jobs[job.id] = job
    thread = threading.Thread(target=_run, args=(job, username, password,
                                                 email, app_password),
                              daemon=True, name=f"login-{job.id[:8]}")
    thread.start()
    return job.id


def get_job(job_id: str) -> LoginJob | None:
    _purge()
    with _lock:
        return _jobs.get(job_id)


def submit_code(job_id: str, code: str) -> bool:
    job = get_job(job_id)
    if job is None or job.status != "awaiting_code":
        return False
    job._code = (code or "").strip()
    job.status = "running"
    _push(job, "code received, submitting to Instagram ...")
    job._code_event.set()
    return True


def _perform_login(job: LoginJob, username: str, password: str,
                   code_callback: Callable[[str, str], str],
                   email_creds: tuple[str, str] | None = None):
    """Library interaction, isolated for testability. Returns ig client."""
    from instaharvest_v2 import Instagram

    _push(job, "creating Instagram client ...")
    ig = Instagram(challenge_callback=code_callback)
    _push(job, f"logging in as @{username} ...")
    if email_creds:
        _push(job, "email auto-verify enabled (code will be read from Gmail) ...")
        ig.login(username, password, email_credentials=email_creds)
    else:
        ig.login(username, password)
    return ig


def _run(job: LoginJob, username: str, password: str,
         email: str = "", app_password: str = "") -> None:
    def code_callback(challenge_type: str = "", contact_point: str = "") -> str:
        job.challenge_type = str(challenge_type or "")
        job.contact_point = str(contact_point or "")
        job.status = "awaiting_code"
        _push(job, f"Instagram needs verification ({job.challenge_type or 'code'}). "
                   f"Code sent to: {job.contact_point or 'your email/phone'}. "
                   f"Enter it in the CODE box below.")
        job._code_event.clear()
        ok = job._code_event.wait(timeout=CODE_TIMEOUT_SEC)
        if not ok or not job._code:
            raise TimeoutError("No verification code provided in time (10 min).")
        return job._code

    try:
        _push(job, "job started.")
        creds = (email, app_password) if email and app_password else None
        ig = _perform_login(job, username, password, code_callback, creds)
    except Exception as exc:
        from app.instagram.adapter import InstagramAdapter
        kind = InstagramAdapter.classify_error(exc)
        job.status = "failed"
        job.error = f"{kind}: {exc}"
        _push(job, f"FAILED [{kind}]: {exc}")
        _push(job, "Tip: wrong password, expired challenge, or Instagram wants "
                   "in-app approval (open the Instagram app and try again).")
        return
    finally:
        password = ""  # noqa: F841 - drop credential references ASAP
        app_password = ""  # noqa: F841
        del password, app_password

    try:
        _push(job, "login accepted, validating session ...")
        validator = getattr(getattr(ig, "auth", ig), "validate_session", None)
        if callable(validator):
            try:
                validator()
            except Exception as exc:
                _push(job, f"validate_session inconclusive: {exc!r} (continuing)")
        path = f"/tmp/igh_login_{job.id}.json"
        saver = getattr(ig, "save_session", None) or \
            getattr(getattr(ig, "auth", ig), "save_session", None)
        if not callable(saver):
            raise RuntimeError("client has no save_session(); cannot export session")
        saver(path)
        with open(path) as fh:
            job.session_json = fh.read()
        _push(job, f"session saved ({len(job.session_json)} bytes, instaharvest-v2 format).")
        try:
            me = ig.account.get_current_user()
            name = getattr(me, "username", None) or (me.get("username") if hasattr(me, "get") else "")
            if name:
                job.account_username = str(name)
                _push(job, f"authenticated as @{job.account_username}.")
        except Exception as exc:
            _push(job, f"could not read profile name (non-fatal): {exc!r}")
        job.status = "done"
        _push(job, "DONE — copy the session JSON below into Render env var INSTAGRAM_SESSION, "
                   "then POST /upload to test.")
    except Exception as exc:
        job.status = "failed"
        job.error = str(exc)[:500]
        _push(job, f"FAILED while exporting session: {exc}")
