# -*- coding: utf-8 -*-
# app/api/verify.py — the gate's own tiny surface: full-screen challenge page
# + the one endpoint that checks a Turnstile token and issues the gate cookie.
import os

from fastapi import APIRouter, Request, Response
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

from app.core.verify import (
    GATE_COOKIE, GATE_TTL, active_site_key, check_turnstile,
    client_ip, make_gate_token,
)

router = APIRouter(tags=["verify"])

TEMPLATES_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web", "templates")


class VerifyBody(BaseModel):
    token: str


@router.get("/verify")
async def verify_page():
    path = os.path.join(TEMPLATES_DIR, "verify.html")
    with open(path, "r", encoding="utf-8") as f:
        html = f.read()
    html = html.replace("__SITE_KEY__", active_site_key())
    return HTMLResponse(html)


@router.post("/api/verify/check")
async def verify_check(body: VerifyBody, request: Request, response: Response):
    ip = client_ip(request)
    ok = await check_turnstile(body.token, remote_ip=ip)
    if not ok:
        return {"status": "error", "error": "verification failed, please try again"}
    token = make_gate_token(ip)
    response.set_cookie(
        key=GATE_COOKIE, value=token, max_age=GATE_TTL,
        httponly=True, samesite="lax",
        secure=os.environ.get("ZBR_COOKIE_SECURE", "0") == "1",
    )
    return {"status": "ok"}
