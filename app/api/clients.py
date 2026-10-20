# -*- coding: utf-8 -*-
# app/api/clients.py — REST surface over core/clients.py + protocols/engine.py.
# Ownership-scoped: a regular user only sees/touches their own clients; an
# admin sees/touches everyone's. Creating a client costs $1 (CENTS_PER_CLIENT),
# deducted from the owner's balance up front — insufficient balance blocks
# creation before any bot/account work starts.
import asyncio
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.core import clients as client_store
from app.core import db
from app.core.auth import current_user
from protocols import engine

router = APIRouter(prefix="/api/clients", tags=["clients"])


class CreateClientBody(BaseModel):
    name: str
    mode: str  # "with_me" | "normal"
    captain_uid: Optional[str] = ""
    clan_id: Optional[str] = ""
    bot_count: Optional[int] = None
    name_prefix: Optional[str] = "FYT_"
    notes: Optional[str] = ""
    auto_start: Optional[bool] = True


class UpdateClientBody(BaseModel):
    name: Optional[str] = None
    captain_uid: Optional[str] = None
    clan_id: Optional[str] = None
    bot_count: Optional[int] = None
    name_prefix: Optional[str] = None
    notes: Optional[str] = None


def _serialize(c: dict) -> dict:
    runtime = engine._client_runtimes.get(c["id"])
    return {
        **c,
        "live_running": bool(runtime and runtime.is_running()),
        "online_count": sum(1 for a in engine.bot_state.accounts_for_client(c["id"]) if a.get("status") == "ONLINE"),
        "slot_count": len(runtime.bot_uids) if runtime else c.get("bot_count", 0),
    }


def _owned_or_404(client_id: str, user: dict) -> dict:
    c = client_store.get_client(client_id)
    if not c:
        raise HTTPException(404, "client not found")
    if user["role"] != "admin" and c.get("owner_id") != user["id"]:
        raise HTTPException(403, "not your client")
    return c


@router.get("")
async def get_clients(user: dict = Depends(current_user)):
    clients = client_store.list_clients() if user["role"] == "admin" else client_store.list_clients_for_owner(user["id"])
    return {"status": "ok", "clients": [_serialize(c) for c in clients]}


@router.get("/{client_id}")
async def get_client(client_id: str, user: dict = Depends(current_user)):
    c = _owned_or_404(client_id, user)
    return {"status": "ok", "client": _serialize(c)}


@router.get("/{client_id}/slots")
async def get_client_slots(client_id: str, user: dict = Depends(current_user)):
    _owned_or_404(client_id, user)
    return {"status": "ok", "slots": engine.client_live_slots(client_id)}


@router.post("")
async def create_client(body: CreateClientBody, user: dict = Depends(current_user)):
    if body.mode not in ("with_me", "normal"):
        raise HTTPException(400, "mode must be 'with_me' or 'normal'")
    if body.mode == "with_me" and not (body.captain_uid or "").strip().isdigit():
        raise HTTPException(400, "captain_uid (your game UID) is required and must be numeric for 'with_me' mode")

    # $1 = one squad. Admin bypasses the charge (operator, not a customer).
    if user["role"] != "admin":
        fresh = db.get_user_by_id(user["id"])
        if fresh["balance_cents"] < db.CENTS_PER_CLIENT:
            raise HTTPException(402, f"insufficient balance — creating a squad costs ${db.CENTS_PER_CLIENT/100:.2f}, you have ${fresh['balance_cents']/100:.2f}")
        charged = db.adjust_balance(user["id"], -db.CENTS_PER_CLIENT, "client_create", actor_id=user["id"])
        if not charged:
            raise HTTPException(500, "could not charge balance")

    c = client_store.create_client(
        owner_id=user["id"], name=body.name, mode=body.mode, captain_uid=body.captain_uid or "",
        clan_id=body.clan_id or "", bot_count=body.bot_count,
        name_prefix=body.name_prefix or "FYT_", notes=body.notes or "",
    )
    if body.auto_start:
        asyncio.create_task(_start_and_flag(c))
    return {"status": "ok", "client": _serialize(c)}


@router.patch("/{client_id}")
async def patch_client(client_id: str, body: UpdateClientBody, user: dict = Depends(current_user)):
    _owned_or_404(client_id, user)
    c = client_store.update_client(client_id, **body.dict(exclude_unset=True))
    if not c:
        raise HTTPException(404, "client not found")
    return {"status": "ok", "client": _serialize(c)}


async def _start_and_flag(c: dict):
    await engine.start_client(c)
    client_store.set_running(c["id"], True)


async def _stop_and_flag(client_id: str):
    await engine.stop_client(client_id)
    client_store.set_running(client_id, False)


@router.post("/{client_id}/start")
async def start_client(client_id: str, user: dict = Depends(current_user)):
    c = _owned_or_404(client_id, user)
    asyncio.create_task(_start_and_flag(c))
    return {"status": "ok"}


@router.post("/{client_id}/stop")
async def stop_client(client_id: str, user: dict = Depends(current_user)):
    _owned_or_404(client_id, user)
    asyncio.create_task(_stop_and_flag(client_id))
    return {"status": "ok"}


@router.post("/{client_id}/restart")
async def restart_client(client_id: str, user: dict = Depends(current_user)):
    c = _owned_or_404(client_id, user)
    async def _do():
        await engine.restart_client(c)
        client_store.set_running(client_id, True)
    asyncio.create_task(_do())
    return {"status": "ok"}


@router.delete("/{client_id}")
async def delete_client(client_id: str, user: dict = Depends(current_user)):
    _owned_or_404(client_id, user)
    # engine.stop_client() cancels every bot task and awaits them; a worker
    # stuck mid-request (network stall, a stubborn socket) could otherwise
    # hang this await forever and make the frontend look like "delete does
    # nothing". Give it a bounded window, then delete the client record
    # regardless — a lingering runtime task is harmless once the client row
    # is gone (nothing references it anymore) and will finish cancelling on
    # its own.
    try:
        await asyncio.wait_for(engine.stop_client(client_id), timeout=10.0)
    except asyncio.TimeoutError:
        pass
    client_store.delete_client(client_id)
    return {"status": "ok"}
