"""Application-level authentication, shared by every tool.

Sign-in belongs to the application, not to any one tool - a preparer signs in
once and reaches whichever tools they are entitled to. Tools depend on
`require_session` and nothing else, so none of them implements or weakens
authentication on its own.

There is no anonymous access to anything.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import os
import secrets

from fastapi import HTTPException, Request

SESSION_COOKIE = "wb_session"
SESSION_HOURS = 12

_STATE: dict[str, str] = {}


def _secret() -> bytes:
    s = os.environ.get("APP_SECRET") or os.environ.get("RSUFA_SECRET")
    if not s:
        s = _STATE.get("secret") or secrets.token_hex(32)
        _STATE["secret"] = s
    return s.encode()


def password() -> str:
    """The configured password, or one generated and printed once at startup."""
    pw = os.environ.get("APP_PASSWORD") or os.environ.get("RSUFA_PASSWORD")
    if not pw:
        pw = _STATE.get("password")
        if not pw:
            pw = secrets.token_urlsafe(9)
            _STATE["password"] = pw
            print("\n" + "=" * 68)
            print("  No APP_PASSWORD set. This session's password is:\n")
            print(f"      {pw}\n")
            print("  Set APP_PASSWORD in the environment for a stable one.")
            print("=" * 68 + "\n", flush=True)
    return pw


def sign(value: str) -> str:
    mac = hmac.new(_secret(), value.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{value}.{mac}"


def valid(token: str | None) -> bool:
    if not token or "." not in token:
        return False
    if not hmac.compare_digest(sign(token.rpartition(".")[0]), token):
        return False
    try:
        return dt.datetime.utcnow() < dt.datetime.fromisoformat(
            token.rpartition(".")[0])
    except ValueError:
        return False


def new_token() -> tuple[str, str]:
    expiry = (dt.datetime.utcnow() + dt.timedelta(hours=SESSION_HOURS)).isoformat()
    return sign(expiry), expiry


def check_password(supplied: str) -> bool:
    return hmac.compare_digest(supplied or "", password())


def require_session(request: Request) -> str:
    """Every route in every tool depends on this."""
    if not valid(request.cookies.get(SESSION_COOKIE)):
        raise HTTPException(status_code=401, detail="Sign in required")
    return "user"
