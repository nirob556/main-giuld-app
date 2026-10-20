# -*- coding: utf-8 -*-
# app/api/admin.py — admin-only: dashboard, users (search/paginate/balance),
# deposits (search/filter/manual verify), settings, audit log, squads,
# redeem codes. Gated by require_admin on the whole router. Every mutating
# action here writes to admin_audit_log.
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.core import clients as client_store
from app.core import db
from app.core import gateway
from app.core.auth import require_admin
from protocols import engine

router = APIRouter(prefix="/api/admin", tags=["admin"], dependencies=[Depends(require_admin)])


class AdjustBalanceBody(BaseModel):
    delta: float  # dollars, can be negative to remove
    reason: Optional[str] = None


class SetBalanceBody(BaseModel):
    balance: float  # dollars, absolute
    reason: Optional[str] = None


class UpdateSettingsBody(BaseModel):
    usd_to_bdt_rate: Optional[float] = None
    min_deposit_usd: Optional[float] = None
    max_deposit_usd: Optional[float] = None
    zrpay_api_key: Optional[str] = None
    zrpay_secret_key: Optional[str] = None
    zrpay_brand_key: Optional[str] = None
    maintenance_mode: Optional[bool] = None
    maintenance_message: Optional[str] = None
    announcement: Optional[str] = None
    site_name: Optional[str] = None


def _public_user(u: dict) -> dict:
    return {
        "id": u["id"], "username": u["username"], "email": u["email"],
        "balance_cents": u["balance_cents"], "balance": round(u["balance_cents"] / 100, 2),
        "role": u["role"], "created_at": u["created_at"], "last_login_at": u["last_login_at"],
    }


# ---------------- dashboard ----------------

@router.get("/dashboard")
async def dashboard():
    stats = db.dashboard_stats()
    clients = client_store.list_clients()
    stats["total_squads"] = len(clients)
    stats["running_squads"] = sum(1 for c in clients if c.get("running"))
    stats["total_revenue"] = round(stats["total_revenue_cents"] / 100, 2)
    return {"status": "ok", "stats": stats}


# ---------------- users ----------------

@router.get("/users")
async def list_users(search: Optional[str] = None, limit: int = 25, offset: int = 0):
    result = db.list_users_paginated(search=search, limit=min(limit, 200), offset=max(offset, 0))
    client_counts = {}
    for c in client_store.list_clients():
        client_counts[c.get("owner_id")] = client_counts.get(c.get("owner_id"), 0) + 1
    out = []
    for u in result["users"]:
        pu = _public_user(u)
        pu["client_count"] = client_counts.get(u["id"], 0)
        out.append(pu)
    return {"status": "ok", "users": out, "total": result["total"], "limit": limit, "offset": offset}


@router.get("/users/{user_id}")
async def user_detail(user_id: str):
    u = db.get_user_by_id(user_id)
    if not u:
        raise HTTPException(404, "user not found")
    deposits = db.list_deposits_for_user(user_id, limit=50)
    squads = client_store.list_clients_for_owner(user_id)
    return {
        "status": "ok",
        "user": _public_user(u),
        "deposits": [{
            "id": d["id"], "amount_usd": round(d["amount_usd_cents"] / 100, 2),
            "amount_bdt": round(d["amount_bdt_cents"] / 100, 2), "status": d["status"],
            "created_at": d["created_at"],
        } for d in deposits],
        "squad_count": len(squads),
    }


@router.post("/users/{user_id}/balance/adjust")
async def adjust_balance(user_id: str, body: AdjustBalanceBody, admin: dict = Depends(require_admin)):
    delta_cents = round(body.delta * 100)
    if delta_cents == 0:
        raise HTTPException(400, "delta must be non-zero")
    user = db.adjust_balance(user_id, delta_cents, "admin_adjust", actor_id=admin["id"])
    if not user:
        raise HTTPException(404, "user not found")
    db.log_admin_action(admin["id"], "balance_adjust", "user", user_id,
                         f"delta={body.delta:+.2f} reason={body.reason or ''}")
    return {"status": "ok", "user": _public_user(user)}


@router.post("/users/{user_id}/balance/set")
async def set_balance(user_id: str, body: SetBalanceBody, admin: dict = Depends(require_admin)):
    if body.balance < 0:
        raise HTTPException(400, "balance cannot be negative")
    user = db.set_balance(user_id, round(body.balance * 100), actor_id=admin["id"])
    if not user:
        raise HTTPException(404, "user not found")
    db.log_admin_action(admin["id"], "balance_set", "user", user_id,
                         f"new_balance={body.balance:.2f} reason={body.reason or ''}")
    return {"status": "ok", "user": _public_user(user)}


# ---------------- deposits / payments ----------------

@router.get("/deposits")
async def list_deposits(status: Optional[str] = None, search: Optional[str] = None,
                         limit: int = 50, offset: int = 0):
    result = db.list_all_deposits(status=status, search=search, limit=min(limit, 200), offset=max(offset, 0))
    out = []
    for d in result["deposits"]:
        out.append({
            "id": d["id"], "username": d["username"],
            "amount_usd": round(d["amount_usd_cents"] / 100, 2),
            "amount_bdt": round(d["amount_bdt_cents"] / 100, 2),
            "exchange_rate": float(d["exchange_rate"]), "status": d["status"],
            "gateway_transaction_id": d["gateway_transaction_id"], "credited": d["credited"],
            "failure_reason": d["failure_reason"], "created_at": d["created_at"],
            "verified_at": d["verified_at"],
        })
    return {"status": "ok", "deposits": out, "total": result["total"], "limit": limit, "offset": offset}


@router.post("/deposits/{deposit_id}/reverify")
async def reverify_deposit(deposit_id: str, admin: dict = Depends(require_admin)):
    """Manual reconciliation: re-run the documented verify() call against the
    gateway and apply the same atomic credit path a normal return/poll would.
    Never credits twice — same credit_deposit_if_unverified() choke point."""
    from app.api.payments import _finalize_from_gateway
    dep = db.get_deposit(deposit_id)
    if not dep:
        raise HTTPException(404, "deposit not found")
    updated = await _finalize_from_gateway(deposit_id, None)
    db.log_admin_action(admin["id"], "deposit_reverify", "deposit", deposit_id, f"result_status={updated['status']}")
    return {"status": "ok", "deposit": {
        "id": updated["id"], "status": updated["status"], "credited": updated["credited"],
        "failure_reason": updated["failure_reason"],
    }}


# ---------------- settings ----------------

@router.get("/settings")
async def get_settings():
    s = db.get_settings()
    def _mask(v):
        return (v[:4] + "…" + v[-4:]) if v and len(v) > 10 else ("set" if v else "")
    return {
        "status": "ok",
        "settings": {
            "usd_to_bdt_rate": float(s.get("usd_to_bdt_rate", 110)),
            "min_deposit_usd": float(s.get("min_deposit_usd", 1)),
            "max_deposit_usd": float(s.get("max_deposit_usd", 500)),
            "zrpay_api_key_set": bool(s.get("zrpay_api_key")),
            "zrpay_api_key_masked": _mask(s.get("zrpay_api_key", "")),
            "zrpay_secret_key_set": bool(s.get("zrpay_secret_key")),
            "zrpay_brand_key_set": bool(s.get("zrpay_brand_key")),
            "zrpay_configured": gateway.is_configured(),
            "maintenance_mode": s.get("maintenance_mode") == "1",
            "maintenance_message": s.get("maintenance_message", ""),
            "announcement": s.get("announcement", ""),
            "site_name": s.get("site_name", "ZBR FF GLORY"),
        },
    }


@router.post("/settings")
async def update_settings(body: UpdateSettingsBody, admin: dict = Depends(require_admin)):
    values = {}
    if body.usd_to_bdt_rate is not None:
        if body.usd_to_bdt_rate <= 0:
            raise HTTPException(400, "exchange rate must be positive")
        values["usd_to_bdt_rate"] = body.usd_to_bdt_rate
    if body.min_deposit_usd is not None:
        values["min_deposit_usd"] = body.min_deposit_usd
    if body.max_deposit_usd is not None:
        values["max_deposit_usd"] = body.max_deposit_usd
    if body.min_deposit_usd is not None and body.max_deposit_usd is not None:
        if body.min_deposit_usd > body.max_deposit_usd:
            raise HTTPException(400, "min deposit cannot exceed max deposit")
    if body.zrpay_api_key is not None:
        values["zrpay_api_key"] = body.zrpay_api_key
    if body.zrpay_secret_key is not None:
        values["zrpay_secret_key"] = body.zrpay_secret_key
    if body.zrpay_brand_key is not None:
        values["zrpay_brand_key"] = body.zrpay_brand_key
    if body.maintenance_mode is not None:
        values["maintenance_mode"] = "1" if body.maintenance_mode else "0"
    if body.maintenance_message is not None:
        values["maintenance_message"] = body.maintenance_message
    if body.announcement is not None:
        values["announcement"] = body.announcement
    if body.site_name is not None:
        values["site_name"] = body.site_name

    if not values:
        raise HTTPException(400, "no settings provided")

    db.set_settings(values, updated_by=admin["id"])
    safe_keys = [k for k in values if "key" not in k]
    secret_keys_changed = [k for k in values if "key" in k]
    detail = f"changed={','.join(safe_keys)}" + (f" secrets_rotated={','.join(secret_keys_changed)}" if secret_keys_changed else "")
    db.log_admin_action(admin["id"], "settings_update", "settings", None, detail)
    return await get_settings()


# ---------------- audit log ----------------

@router.get("/audit-log")
async def audit_log(limit: int = 100, offset: int = 0):
    result = db.list_audit_log(limit=min(limit, 200), offset=max(offset, 0))
    return {"status": "ok", "entries": result["entries"], "total": result["total"]}


# ---------------- squads (clients) ----------------

@router.get("/clients")
async def list_all_clients():
    users_by_id = {u["id"]: u for u in db.list_users()}
    out = []
    for c in client_store.list_clients():
        runtime = engine._client_runtimes.get(c["id"])
        owner = users_by_id.get(c.get("owner_id"))
        out.append({
            **c,
            "live_running": bool(runtime and runtime.is_running()),
            "online_count": sum(1 for a in engine.bot_state.accounts_for_client(c["id"]) if a.get("status") == "ONLINE"),
            "owner_username": owner["username"] if owner else "(deleted user)",
        })
    return {"status": "ok", "clients": out}


@router.post("/clients/{client_id}/stop")
async def admin_stop_client(client_id: str, admin: dict = Depends(require_admin)):
    c = client_store.get_client(client_id)
    if not c:
        raise HTTPException(404, "client not found")
    await engine.stop_client(client_id)
    client_store.set_running(client_id, False)
    db.log_admin_action(admin["id"], "squad_stop", "squad", client_id, c.get("name"))
    return {"status": "ok"}


@router.delete("/clients/{client_id}")
async def admin_delete_client(client_id: str, admin: dict = Depends(require_admin)):
    c = client_store.get_client(client_id)
    if not c:
        raise HTTPException(404, "client not found")
    await engine.stop_client(client_id)
    client_store.delete_client(client_id)
    db.log_admin_action(admin["id"], "squad_delete", "squad", client_id, c.get("name"))
    return {"status": "ok"}


# ---------------- redeem codes ----------------

@router.get("/redeem-codes")
async def list_all_redeem_codes():
    users_by_id = {u["id"]: u for u in db.list_users()}
    codes = db.list_all_codes()
    out = []
    for code in codes:
        creator = users_by_id.get(code["created_by"])
        redeemer = users_by_id.get(code["redeemed_by"]) if code["redeemed_by"] else None
        out.append({
            **code,
            "value": round(code["value_cents"] / 100, 2),
            "created_by_username": creator["username"] if creator else "(deleted user)",
            "redeemed_by_username": redeemer["username"] if redeemer else None,
        })
    return {"status": "ok", "codes": out}
