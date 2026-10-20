# -*- coding: utf-8 -*-
# app/api/live.py — WS push (account/slot updates only — no log stream to the
# browser, logs stay terminal-only via protocols.engine._pb/print_*) and stats.
import asyncio
import time

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from fastapi.websockets import WebSocketState

from app.core.auth import require_auth, verify_session_token
from app.core import clients as client_store
from app.core import db
from protocols import engine

router = APIRouter(tags=["live"])


@router.get("/api/stats")
async def get_stats(session: dict = Depends(require_auth)):
    user = db.get_user_by_id(session["user_id"])
    is_admin = user and user["role"] == "admin"
    clients = client_store.list_clients() if is_admin else client_store.list_clients_for_owner(session["user_id"])
    client_ids = {c["id"] for c in clients}
    accounts = [a for a in engine.bot_state.accounts.values() if is_admin or a.get("client_id") in client_ids]
    glory = getattr(engine, "GLORY_COUNT", 0)
    return {
        "status": "ok",
        "stats": {
            "glory_count": glory,
            "total_clients": len(clients),
            "running_clients": sum(1 for c in clients if c.get("running")),
            "total_accounts": len(accounts),
            "online_accounts": sum(1 for a in accounts if a.get("status") == "ONLINE"),
            "in_match_accounts": sum(1 for a in accounts if a.get("match_state") == "IN_MATCH"),
            "balance": round((user["balance_cents"] if user else 0) / 100, 2),
        },
    }


@router.websocket("/ws/live")
async def ws_live(ws: WebSocket):
    token = ws.cookies.get("zbr_session")
    session = verify_session_token(token)
    if not session:
        await ws.close(code=4401)
        return
    await ws.accept()
    q = engine.bot_state.subscribe()
    try:
        while True:
            try:
                event = await asyncio.wait_for(q.get(), timeout=20.0)
                if event.get("type") == "log":
                    continue  # terminal-only, never forwarded to the browser
                await ws.send_json(event)
            except asyncio.TimeoutError:
                if ws.application_state != WebSocketState.CONNECTED:
                    break
                await ws.send_json({"type": "ping", "data": {"ts": time.time()}})
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        engine.bot_state.unsubscribe(q)
