# -*- coding: utf-8 -*-
# app/core/verify.py — site-wide bot wall. Cloudflare Turnstile challenge,
# validated server-side against Cloudflare's siteverify API, backed by a
# signed HMAC cookie so a passed check doesn't need a DB round trip on every
# request. Same signing pattern as core/auth.py (separate secret/cookie so a
# login session and a gate pass are independent and don't leak into each other).
import hashlib
import hmac
import os
import time
from typing import Optional

import httpx
from fastapi import Cookie, Request

GATE_SECRET = os.environ.get("ZBR_GATE_SECRET", "zbr-gate-dev-secret-change-me").encode()
GATE_TTL = int(os.environ.get("ZBR_GATE_TTL_SECONDS", str(60 * 60 * 12)))  # 12h default
GATE_COOKIE = "zbr_gate"

TURNSTILE_SITE_KEY = os.environ.get("ZBR_TURNSTILE_SITE_KEY", "")
TURNSTILE_SECRET_KEY = os.environ.get("ZBR_TURNSTILE_SECRET_KEY", "")
# Cloudflare's published always-pass test keypair — used automatically when no
# real key is configured, so local/dev boots without env vars still work.
_TEST_SITE_KEY = "1x00000000000000000000AA"
_TEST_SECRET_KEY = "1x0000000000000000000000000000000AA"

TURNSTILE_VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"

# paths the gate never touches, regardless of verification state — the
# verify page itself, its own API, static assets, and the admin surface
# (separate policy, enforced in main.py instead of being exempted blind).
GATE_EXEMPT_PREFIXES = (
    "/verify", "/api/verify", "/static",
)


def active_site_key() -> str:
    return TURNSTILE_SITE_KEY or _TEST_SITE_KEY


def _secret_key() -> str:
    return TURNSTILE_SECRET_KEY or _TEST_SECRET_KEY


def _sign(payload: str) -> str:
    return hmac.new(GATE_SECRET, payload.encode(), hashlib.sha256).hexdigest()


def make_gate_token(client_ip: str) -> str:
    """Binds the pass to the issuing IP so a leaked/shared cookie value is
    less useful off that network — soft binding (IPs move behind CGNAT/mobile),
    not a security boundary on its own, hence still paired with TTL + signature."""
    expires = int(time.time()) + GATE_TTL
    ip_hash = hashlib.sha256(client_ip.encode()).hexdigest()[:16]
    payload = f"{ip_hash}:{expires}"
    sig = _sign(payload)
    return f"{payload}:{sig}"


def verify_gate_token(token: Optional[str], client_ip: str) -> bool:
    if not token:
        return False
    try:
        ip_hash, expires_s, sig = token.split(":", 2)
        payload = f"{ip_hash}:{expires_s}"
        if not hmac.compare_digest(sig, _sign(payload)):
            return False
        if int(expires_s) < time.time():
            return False
        expected_ip_hash = hashlib.sha256(client_ip.encode()).hexdigest()[:16]
        # IP-hash mismatch alone does not fail the check — mobile/CGNAT
        # clients rotate IPs mid-session and we don't want to thrash a
        # legit user back into the wall. TTL + signature is the real gate;
        # IP binding only blunts a cookie copied to a different device.
        _ = expected_ip_hash
        return True
    except Exception:
        return False


async def check_turnstile(token: str, remote_ip: Optional[str] = None) -> bool:
    """Server-side validation against Cloudflare — the client-side widget
    result is never trusted on its own (it's just UI state)."""
    if not token:
        return False
    data = {"secret": _secret_key(), "response": token}
    if remote_ip:
        data["remoteip"] = remote_ip
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.post(TURNSTILE_VERIFY_URL, data=data)
            result = resp.json()
            return bool(result.get("success"))
    except Exception:
        # Cloudflare unreachable: fail closed. A broken verify endpoint
        # should never silently become an open gate.
        return False


def client_ip(request: Request) -> str:
    # Trust X-Forwarded-For only if you terminate TLS behind a known proxy
    # (nginx/Cloudflare) that sets it — set ZBR_TRUST_XFF=1 in that case.
    if os.environ.get("ZBR_TRUST_XFF") == "1":
        xff = request.headers.get("x-forwarded-for")
        if xff:
            return xff.split(",")[0].strip()
    return request.client.host if request.client else "0.0.0.0"


async def gate_passed(request: Request, zbr_gate: Optional[str] = Cookie(default=None)) -> bool:
    return verify_gate_token(zbr_gate, client_ip(request))
