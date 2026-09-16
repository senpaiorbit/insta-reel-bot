"""TOTP 2FA helper for Instagram fresh login — additive only.

Primary source: external TOTP provider over HTTPS (uses ``httpx``,
already in requirements.txt)::

    GET {TOTP_PROVIDER_URL}/?seed={SEED}&json=1

Example::

    https://ig-totp.tanbirst2st2.workers.dev/?seed={SEED}&json=1
    -> {"code": "123456"}  (also accepts totp/otp/token/pin)

Fallback: local ``pyotp`` generation ONLY if already installed
(``import pyotp`` guarded; ``ImportError`` -> skip gracefully, no new dep).

Contracts:
- No seed configured -> caller must keep exact current behavior
  (plain ``ig.login(username, password)``).
- Seed present -> fetch code just-in-time, pass as ``verification_code``;
  on ``TwoFactorRequired`` fetch ONE fresh code and retry ONCE.
- Never log seed/code (only has_seed/has_code booleans).
- Never persist secrets to disk (no file writes; /tmp session handling
  stays in adapter.py only).
"""

from __future__ import annotations

import logging
import os
from typing import Any

log = logging.getLogger(__name__)

DEFAULT_TOTP_PROVIDER_URL = "https://ig-totp.tanbirst2st2.workers.dev"

_CODE_KEYS = ("code", "totp", "otp", "token", "pin")
_SEED_ENVS = ("INSTAGRAM_TOTP_SEED", "IG_TOTP_SEED", "TOTP_SEED")


def resolve_totp_seed(settings: Any | None = None, explicit: str = "") -> str:
    """Return configured TOTP seed or "" when 2FA is not configured."""
    if explicit and explicit.strip():
        return explicit.strip()
    if settings is not None:
        for name in _SEED_ENVS:
            try:
                val = getattr(settings, name, "")
            except Exception:
                val = ""
            if val and str(val).strip():
                return str(val).strip()
    for name in _SEED_ENVS:
        val = os.environ.get(name, "")
        if val and val.strip():
            return val.strip()
    return ""


def resolve_totp_provider_url(settings: Any | None = None, explicit: str = "") -> str:
    """Return provider base URL (no query string). Never returns ""."""
    if explicit and explicit.strip():
        return explicit.strip().rstrip("/")
    if settings is not None:
        try:
            val = getattr(settings, "TOTP_PROVIDER_URL", "")
            if val and str(val).strip():
                return str(val).strip().rstrip("/")
        except Exception:
            pass
    val = os.environ.get("TOTP_PROVIDER_URL", "")
    if val and val.strip():
        return val.strip().rstrip("/")
    return DEFAULT_TOTP_PROVIDER_URL


def _extract_code(payload: Any) -> str:
    """Extract code/totp/otp/token/pin from provider JSON. Returns "" if none."""
    if not isinstance(payload, dict):
        return ""
    for key in _CODE_KEYS:
        val = payload.get(key)
        if val in (None, ""):
            continue
        text = str(val).strip().replace(" ", "")
        if text:
            return text
    for nest in ("data", "result"):
        inner = payload.get(nest)
        if isinstance(inner, dict):
            found = _extract_code(inner)
            if found:
                return found
    return ""


def fetch_totp_code_via_provider(seed: str, provider_url: str = "", timeout: float = 10.0) -> str:
    """GET {provider_url}/?seed={seed}&json=1 -> code string or ""."""
    seed = (seed or "").strip()
    if not seed:
        return ""
    base = (provider_url or "").strip().rstrip("/") or DEFAULT_TOTP_PROVIDER_URL
    try:
        import httpx
    except ImportError as exc:
        raise RuntimeError("httpx unavailable for TOTP provider") from exc
    resp = httpx.get(base, params={"seed": seed, "json": "1"}, timeout=timeout)
    resp.raise_for_status()
    try:
        payload = resp.json()
    except Exception as exc:
        raise RuntimeError("TOTP provider returned non-JSON") from exc
    code = _extract_code(payload)
    if not code:
        raise RuntimeError("TOTP provider JSON had no code field")
    return code


def generate_totp_code_local(seed: str) -> str:
    """Local pyotp TOTP if installed, else "". Never raises for ImportError."""
    seed = (seed or "").strip()
    if not seed:
        return ""
    try:
        import pyotp
    except ImportError:
        return ""
    try:
        normalized = seed.replace(" ", "").strip()
        return str(pyotp.TOTP(normalized).now()).strip()
    except Exception as exc:
        log.warning("TOTP local pyotp generation failed: %r", exc)
        return ""


def get_totp_code(seed: str = "", provider_url: str = "", settings: Any | None = None, timeout: float = 10.0) -> str:
    """Resolve seed/provider if not given, then provider -> pyotp fallback."""
    eff_seed = (seed or "").strip() or resolve_totp_seed(settings=settings)
    if not eff_seed:
        return ""
    eff_provider = ((provider_url or "").strip() or resolve_totp_provider_url(settings=settings))
    try:
        code = fetch_totp_code_via_provider(eff_seed, provider_url=eff_provider, timeout=timeout)
        if code and code.strip():
            return code.strip()
    except Exception as exc:
        log.warning("TOTP provider fetch failed (has_seed=True), trying pyotp fallback: %r", exc)
    local = generate_totp_code_local(eff_seed)
    return local.strip() if local else ""


def _is_two_factor_error(exc: BaseException) -> bool:
    """Name-based check so helper never hard-depends on lib import path."""
    try:
        from instaharvest_v2.exceptions import TwoFactorRequired
        if isinstance(exc, TwoFactorRequired):
            return True
    except Exception:
        pass
    return type(exc).__name__ == "TwoFactorRequired"


def login_with_totp_retry(ig: Any, username: str, password: str, seed: str = "", provider_url: str = "", settings: Any | None = None) -> Any:
    """Fresh-login with additive TOTP 2FA. Returns ig.login() result."""
    eff_seed = (seed or "").strip() or resolve_totp_seed(settings=settings)
    if not eff_seed:
        return ig.login(username, password)
    eff_provider = ((provider_url or "").strip() or resolve_totp_provider_url(settings=settings))
    def _fresh_code() -> str:
        return get_totp_code(seed=eff_seed, provider_url=eff_provider, settings=settings)
    code = _fresh_code()
    if code:
        try:
            return ig.login(username, password, verification_code=code)
        except Exception as exc:
            if not _is_two_factor_error(exc):
                raise
            log.info("AUTH 2FA challenge, retrying once with fresh code")
            fresh = _fresh_code()
            if not fresh:
                raise
            return ig.login(username, password, verification_code=fresh)
    try:
        return ig.login(username, password)
    except Exception as exc:
        if not _is_two_factor_error(exc):
            raise
        log.info("AUTH 2FA required, fetching code for single retry")
        fresh = _fresh_code()
        if not fresh:
            raise
        return ig.login(username, password, verification_code=fresh)
