"""Interactive login jobs powering the GET /login web helper.

Two verification paths:
1. AUTO: ``ig.login()`` with ``challenge_callback`` — the library pauses
   mid-login, we park the job in ``awaiting_code``, the browser UI collects
   the code, the callback returns it.
2. MANUAL CHECKPOINT: some logins raise ``CheckpointRequired`` instead of
   invoking the callback. We then drive the library's real
   ``ChallengeHandler.resolve()`` (verified against
   instaharvest_v2/challenge.py source) with a callback that parks the job
   the same way, so the emailed code can be entered in the UI.

On success the session is saved (instaharvest-v2 NATIVE format) and handed
back for the INSTAGRAM_SESSION env var.

Security: jobs live only in process memory with a 30-min TTL. Passwords and
app-passwords are NEVER logged; references are dropped ASAP. All HTTP routes
are Bearer-gated (see main.py).
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

log = logging.getLogger(__name__)

JOB_TTL_SEC = 1800
CODE_TIMEOUT_SEC = 600
MAX_LOG_LINES = 500
CHALLENGE_EXC_NAMES = frozenset({
    "ChallengeRequired", "CheckpointRequired", "ChallengeError",
    "CheckpointError", "VerificationRequired",
})


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
    _ig: Any = None
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
                   code_callback: Callable[..., str],
                   email_creds: tuple[str, str] | None = None):
    """Library interaction, isolated for testability. Returns ig client."""
    from instaharvest_v2 import Instagram

    _push(job, "creating Instagram client ...")
    ig = Instagram(challenge_callback=code_callback)
    job._ig = ig
    _push(job, f"logging in as @{username} ...")
    if email_creds:
        _push(job, "gmail credentials provided: yes — library will auto-read the code ...")
        ig.login(username, password, email_credentials=email_creds)
    else:
        _push(job, "gmail credentials provided: no (manual code entry only) ...")
        ig.login(username, password)
    return ig


def _is_challenge_exc(exc: BaseException) -> bool:
    names = {type(exc).__name__} | {c.__name__ for c in type(exc).__mro__}
    return bool(names & CHALLENGE_EXC_NAMES)


def _probe_session_parts(ig: Any) -> tuple[Any, str, str]:
    """Best-effort extraction of (curl session, csrf, user-agent)."""
    cands = [getattr(ig, a, None) for a in ("_client", "client", "_session", "session")]
    cands.append(ig)
    session = next((c for c in cands if c is not None
                    and hasattr(c, "get") and hasattr(c, "post")), None)
    csrf = ""
    for obj in (cands[0], cands[1], ig):
        for attr in ("csrf_token", "csrf", "_csrf_token"):
            val = getattr(obj, attr, "") if obj is not None else ""
            if val:
                csrf = str(val)
                break
        if csrf:
            break
    ua = ""
    for obj in (cands[0], cands[1], ig):
        for attr in ("user_agent", "useragent", "_user_agent"):
            val = getattr(obj, attr, "") if obj is not None else ""
            if val:
                ua = str(val)
                break
        if ua:
            break
    return session, csrf, ua


def _try_manual_checkpoint(job: LoginJob, exc: BaseException,
                           wait_for_code: Callable[[str, str], str]) -> bool:
    """Drive ChallengeHandler.resolve() for checkpoint-style failures.

    Returns True when the challenge was resolved (caller: export session).
    """
    url = (getattr(exc, "challenge_url", "") or getattr(exc, "url", "") or "")
    if not url:
        _push(job, "no challenge URL attached to this error — cannot open manual flow.")
        return False
    try:
        try:
            from instaharvest_v2.challenge import ChallengeHandler
        except ImportError:
            from instaharvest_v2 import ChallengeHandler  # type: ignore[no-redef]
    except ImportError:
        _push(job, "ChallengeHandler not available in this library version.")
        return False
    session, csrf, ua = _probe_session_parts(job._ig)
    if session is None:
        _push(job, "could not access the client's HTTP session — cannot open manual flow.")
        return False

    def manual_cb(ctx: Any) -> str:
        ctype = str(getattr(ctx, "challenge_type", "") or "")
        contact = str(getattr(ctx, "contact_point", "") or "")
        _push(job, f"challenge opened (type={ctype or 'verification'}). "
                   f"Code sent to: {contact or 'your email/phone'}. "
                   f"Enter it in the CODE box below.")
        return wait_for_code(ctype, contact)

    _push(job, "opening manual verification (challenge page found) ...")
    try:
        handler = ChallengeHandler(code_callback=manual_cb)
        result = handler.resolve(session=session, challenge_url=str(url),
                                 csrf_token=csrf, user_agent=ua)
    except Exception as e:
        _push(job, f"manual verification error: {e}")
        return False
    if getattr(result, "success", False):
        _push(job, "manual verification accepted by Instagram.")
        return True
    _push(job, f"manual verification rejected: {getattr(result, 'message', result)}")
    return False


def _export_session(job: LoginJob, ig: Any) -> bool:
    """Validate + save session from an authenticated client. Returns ok."""
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
        return True
    except Exception as exc:
        job.status = "failed"
        job.error = str(exc)[:500]
        _push(job, f"FAILED while exporting session: {exc}")
        return False


def _run(job: LoginJob, username: str, password: str,
         email: str = "", app_password: str = "") -> None:
    def park_and_wait(challenge_type: str = "", contact_point: str = "") -> str:
        job.challenge_type = str(challenge_type or "")
        job.contact_point = str(contact_point or "")
        job.status = "awaiting_code"
        job._code_event.clear()
        ok = job._code_event.wait(timeout=CODE_TIMEOUT_SEC)
        if not ok or not job._code:
            raise TimeoutError("No verification code provided in time (10 min).")
        return job._code

    def code_callback(challenge_type: str = "", contact_point: str = "") -> str:
        _push(job, f"Instagram needs verification ({challenge_type or 'code'}). "
                   f"Code sent to: {contact_point or 'your email/phone'}. "
                   f"Enter it in the CODE box below.")
        return park_and_wait(challenge_type, contact_point)

    try:
        _push(job, "job started.")
        creds = (email, app_password) if email and app_password else None
        ig = _perform_login(job, username, password, code_callback, creds)
    except Exception as exc:
        from app.instagram.adapter import InstagramAdapter
        kind = InstagramAdapter.classify_error(exc)
        # Checkpoint-style failure: open the MANUAL code flow (same CODE box).
        if _is_challenge_exc(exc) and job._ig is not None:
            _push(job, f"checkpoint hit [{kind}]: {exc}")
            if _try_manual_checkpoint(job, exc, park_and_wait):
                _export_session(job, job._ig)
                return
        job.status = "failed"
        job.error = f"{kind}: {exc}"
        _push(job, f"FAILED [{kind}]: {exc}")
        _push(job, "Tip: hard-refresh the /login page (Ctrl+Shift+R) so the Gmail "
                   "fields appear, fill Gmail + app password, and retry. "
                   "Or approve the login in the Instagram app and retry.")
        return
    finally:
        password = ""  # noqa: F841 - drop credential references ASAP
        app_password = ""  # noqa: F841
        del password, app_password

    _export_session(job, ig)
