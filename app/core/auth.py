# -*- coding: utf-8 -*-
# app/core/auth.py — user auth: PBKDF2 password hashing, signed session
# cookies carrying user_id + role. No external auth deps (stdlib hashlib only).
import binascii
import hashlib
import hmac
import os
import secrets
import time
from typing import Optional, Tuple

from fastapi import Cookie, HTTPException, status

SESSION_SECRET = os.environ.get("ZBR_SESSION_SECRET", "zbr-ff-glory-dev-secret-change-me").encode()
SESSION_TTL = 60 * 60 * 24 * 7  # 7 days
COOKIE_NAME = "zbr_session"

PBKDF2_ITERATIONS = 200_000


def hash_password(password: str, salt: Optional[str] = None) -> Tuple[str, str]:
    salt = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), PBKDF2_ITERATIONS)
    return binascii.hexlify(dk).decode(), salt


def verify_password(password: str, stored_hash: str, salt: str) -> bool:
    computed, _ = hash_password(password, salt)
    return hmac.compare_digest(computed, stored_hash)


def _sign(payload: str) -> str:
    return hmac.new(SESSION_SECRET, payload.encode(), hashlib.sha256).hexdigest()


def make_session_token(user_id: str, role: str) -> str:
    expires = int(time.time()) + SESSION_TTL
    payload = f"{user_id}:{role}:{expires}"
    sig = _sign(payload)
    return f"{payload}:{sig}"


def verify_session_token(token: Optional[str]) -> Optional[dict]:
    if not token:
        return None
    try:
        user_id, role, expires_s, sig = token.rsplit(":", 3)
        payload = f"{user_id}:{role}:{expires_s}"
        if not hmac.compare_digest(sig, _sign(payload)):
            return None
        if int(expires_s) < time.time():
            return None
        return {"user_id": user_id, "role": role}
    except Exception:
        return None


async def require_auth(zbr_session: Optional[str] = Cookie(default=None)) -> dict:
    """Returns {user_id, role} from the signed cookie, no DB hit. Use
    current_user() in routes that need the fresh record (balance etc)."""
    session = verify_session_token(zbr_session)
    if not session:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="not authenticated")
    return session


async def current_user(zbr_session: Optional[str] = Cookie(default=None)) -> dict:
    from app.core import db
    s = verify_session_token(zbr_session)
    if not s:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="not authenticated")
    user = db.get_user_by_id(s["user_id"])
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="account no longer exists")
    return user


async def require_admin(zbr_session: Optional[str] = Cookie(default=None)) -> dict:
    from app.core import db
    s = verify_session_token(zbr_session)
    if not s:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="not authenticated")
    user = db.get_user_by_id(s["user_id"])
    if not user or user["role"] != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="admin only")
    return user
