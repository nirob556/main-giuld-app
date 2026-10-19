# -*- coding: utf-8 -*-
# app/main.py — ZBR FF GLORY dashboard entrypoint
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()  # reads .env at project root before any env-dependent module (verify/auth) imports

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

from app.api import admin as admin_routes
from app.api import auth as auth_routes
from app.api import clients as client_routes
from app.api import live as live_routes
from app.api import pages as page_routes
from app.api import payments as payment_routes
from app.api import redeem as redeem_routes
from app.api import verify as verify_routes
from app.core.db import init_db

init_db()

app = FastAPI(title="ZBR FF GLORY", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

_MAINTENANCE_EXEMPT_PREFIXES = (
    "/api/auth", "/api/admin", "/static", "/login", "/admin", "/ws/live",
    "/api/payments/announcement",
)

# Admin is its own trust boundary: a logged-in admin session should never get
# stuck behind the bot wall (they already proved who they are), and the admin
# surface is small/low-traffic enough that it isn't the thing the wall exists
# to protect. Everything else — registration, dashboard, APIs, direct domain
# hits — goes through the gate.
_GATE_EXEMPT_PREFIXES = (
    "/verify", "/api/verify", "/static", "/admin", "/api/admin", "/ws/live",
)
_GATE_EXEMPT_API_PREFIXES = ("/api/",)  # API 403s instead of redirecting


@app.middleware("http")
async def bot_wall_gate(request, call_next):
    """Site-wide verification wall. Blocks everything except the verify page
    itself and the admin surface (separate trust boundary, see above) until
    the signed zbr_gate cookie is present and valid. Page requests bounce to
    /verify?next=<path>; API requests get a 403 JSON body instead of HTML,
    since a redirect is meaningless to fetch()/curl and would just surface as
    a broken JSON parse on the frontend."""
    path = request.url.path
    if path.startswith(_GATE_EXEMPT_PREFIXES):
        return await call_next(request)

    from app.core.verify import client_ip, verify_gate_token
    from fastapi.responses import JSONResponse, RedirectResponse

    token = request.cookies.get("zbr_gate")
    if verify_gate_token(token, client_ip(request)):
        return await call_next(request)

    if path.startswith(_GATE_EXEMPT_API_PREFIXES):
        return JSONResponse(status_code=403, content={"status": "error", "detail": "verification required"})

    from urllib.parse import quote
    return RedirectResponse(f"/verify?next={quote(path)}")


@app.middleware("http")
async def maintenance_mode_gate(request, call_next):
    """Blocks non-admin, non-auth traffic with 503 when maintenance_mode is
    on. Admin API/page and auth (so an admin can still log in) stay open."""
    path = request.url.path
    if path.startswith(_MAINTENANCE_EXEMPT_PREFIXES):
        return await call_next(request)
    from app.core import db as _db
    from fastapi.responses import JSONResponse
    if _db.get_setting("maintenance_mode") == "1" and path.startswith("/api/"):
        from app.core.auth import verify_session_token
        session = verify_session_token(request.cookies.get("zbr_session"))
        if not session or session.get("role") != "admin":
            return JSONResponse(
                status_code=503,
                content={"status": "error", "detail": _db.get_setting("maintenance_message")},
            )
    return await call_next(request)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "web", "static")), name="static")

app.include_router(verify_routes.router)
app.include_router(auth_routes.router)
app.include_router(client_routes.router)
app.include_router(live_routes.router)
app.include_router(page_routes.router)
app.include_router(admin_routes.router)
app.include_router(redeem_routes.router)
app.include_router(payment_routes.router)


@app.on_event("startup")
async def on_startup():
    # resume any clients that were marked running before a restart
    from app.core import clients as client_store
    from protocols import engine

    for c in client_store.list_clients():
        if c.get("running"):
            asyncio.create_task(engine.start_client(c))

    # reconciliation poller — the only retry path for deposits whose return
    # hit never landed (closed tab, crash), since the gateway docs expose no
    # push webhook to retry into. See payments.reconcile_pending_loop.
    asyncio.create_task(payment_routes.reconcile_pending_loop())


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="0.0.0.0", port=20333, reload=False)
