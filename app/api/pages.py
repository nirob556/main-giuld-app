# -*- coding: utf-8 -*-
# app/api/pages.py — serves HTML shells (login, register, dashboard SPA, admin)
import os

from fastapi import APIRouter, Cookie
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from typing import Optional

from app.core.auth import verify_session_token
from app.core.verify import active_site_key
from app.core import db

router = APIRouter(tags=["pages"])

TEMPLATES_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web", "templates")


def _render(template_name: str) -> HTMLResponse:
    """Serves a template with __SITE_KEY__ substituted — same mechanism as
    api/verify.py's verify_page(), used here for register.html's inline widget."""
    with open(os.path.join(TEMPLATES_DIR, template_name), "r", encoding="utf-8") as f:
        html = f.read()
    return HTMLResponse(html.replace("__SITE_KEY__", active_site_key()))


@router.get("/")
async def root(zbr_session: Optional[str] = Cookie(default=None)):
    if verify_session_token(zbr_session):
        return RedirectResponse("/dashboard")
    return RedirectResponse("/login")


@router.get("/login")
async def login_page(zbr_session: Optional[str] = Cookie(default=None)):
    if verify_session_token(zbr_session):
        return RedirectResponse("/dashboard")
    return FileResponse(os.path.join(TEMPLATES_DIR, "login.html"))


@router.get("/register")
async def register_page(zbr_session: Optional[str] = Cookie(default=None)):
    if verify_session_token(zbr_session):
        return RedirectResponse("/dashboard")
    return _render("register.html")


@router.get("/dashboard")
async def dashboard_page(zbr_session: Optional[str] = Cookie(default=None)):
    if not verify_session_token(zbr_session):
        return RedirectResponse("/login")
    return FileResponse(os.path.join(TEMPLATES_DIR, "dashboard.html"))


@router.get("/admin")
async def admin_page(zbr_session: Optional[str] = Cookie(default=None)):
    session = verify_session_token(zbr_session)
    if not session:
        return RedirectResponse("/login")
    user = db.get_user_by_id(session["user_id"])
    if not user or user["role"] != "admin":
        return RedirectResponse("/dashboard")
    return FileResponse(os.path.join(TEMPLATES_DIR, "admin.html"))
