# -*- coding: utf-8 -*-
# app/main.py — ZBR FF GLORY dashboard entrypoint

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

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
    "/api/auth",
    "/api/admin",
    "/static",
    "/login",
    "/admin",
    "/ws/live",
    "/api/payments/announcement",
)


# Maintenance mode remains enabled when configured.
# Verification wall has been removed.

@app.middleware("http")
async def maintenance_mode_gate(request, call_next):
    """Keep maintenance mode protection without the verification wall."""
    path = request.url.path

    if path.startswith(_MAINTENANCE_EXEMPT_PREFIXES):
        return await call_next(request)

    from app.core import db as _db
    from fastapi.responses import JSONResponse

    if (
        _db.get_setting("maintenance_mode") == "1"
        and path.startswith("/api/")
    ):
        from app.core.auth import verify_session_token

        session = verify_session_token(
            request.cookies.get("zbr_session")
        )

        if not session or session.get("role") != "admin":
            return JSONResponse(
                status_code=503,
                content={
                    "status": "error",
                    "detail": _db.get_setting("maintenance_message"),
                },
            )

    return await call_next(request)


BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app.mount(
    "/static",
    StaticFiles(directory=os.path.join(BASE_DIR, "web", "static")),
    name="static",
)

# Verification router removed; other application routes remain.
app.include_router(auth_routes.router)
app.include_router(client_routes.router)
app.include_router(live_routes.router)
app.include_router(page_routes.router)
app.include_router(admin_routes.router)
app.include_router(redeem_routes.router)
app.include_router(payment_routes.router)


@app.on_event("startup")
async def on_startup():
    # Resume clients that were running before a restart.
    from app.core import clients as client_store
    from protocols import engine

    for client in client_store.list_clients():
        if client.get("running"):
            asyncio.create_task(engine.start_client(client))

    # Reconcile pending payments.
    asyncio.create_task(payment_routes.reconcile_pending_loop())


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "20333")),
        reload=False,
    )
