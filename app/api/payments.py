# -*- coding: utf-8 -*-
# app/api/payments.py — Add Balance flow: create a pending deposit, send the
# user to ZR Pay BD, verify server-side on return, credit atomically exactly
# once. See app/core/gateway.py for the documented API surface and
# app/core/db.py (credit_deposit_if_unverified) for the idempotency choke
# point every path below funnels through.
import asyncio
import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, field_validator

from app.core import db
from app.core import gateway
from app.core.auth import current_user

router = APIRouter(prefix="/api/payments", tags=["payments"])

# simple in-process rate limit: user_id -> list of unix timestamps of recent
# create/verify calls. Fine at this scale (single worker, per README); swap
# for a shared store if you ever go multi-process.
_rate_buckets: dict = {}
RATE_LIMIT_WINDOW = 60.0
RATE_LIMIT_MAX = 8


def _rate_limit(user_id: str, bucket: str):
    key = f"{bucket}:{user_id}"
    now = time.time()
    hits = [t for t in _rate_buckets.get(key, []) if now - t < RATE_LIMIT_WINDOW]
    if len(hits) >= RATE_LIMIT_MAX:
        raise HTTPException(429, "too many requests — slow down and try again shortly")
    hits.append(now)
    _rate_buckets[key] = hits


class CreateDepositBody(BaseModel):
    amount_usd: float

    @field_validator("amount_usd")
    @classmethod
    def _v_amount(cls, v):
        if v <= 0:
            raise ValueError("amount must be greater than 0")
        return round(v, 2)


def _public_deposit(d: dict) -> dict:
    return {
        "id": d["id"],
        "amount_usd": round(d["amount_usd_cents"] / 100, 2),
        "amount_bdt": round(d["amount_bdt_cents"] / 100, 2),
        "exchange_rate": float(d["exchange_rate"]),
        "status": d["status"],
        "gateway": d["gateway"],
        "gateway_transaction_id": d["gateway_transaction_id"],
        "payment_url": d["payment_url"],
        "failure_reason": d["failure_reason"],
        "created_at": d["created_at"],
        "updated_at": d["updated_at"],
        "verified_at": d["verified_at"],
    }


@router.get("/announcement")
async def public_announcement():
    """Unauthenticated — the banner needs to show on /login too."""
    return {
        "status": "ok",
        "announcement": db.get_setting("announcement", ""),
        "maintenance_mode": db.get_setting("maintenance_mode") == "1",
        "maintenance_message": db.get_setting("maintenance_message", ""),
    }


@router.get("/config")
async def payment_config(user: dict = Depends(current_user)):
    """Everything the Add Balance widget needs to render: current rate, limits.
    Never exposes gateway secrets — those never leave the server."""
    settings = db.get_settings()
    min_c, max_c = db.get_deposit_limits_cents()
    return {
        "status": "ok",
        "exchange_rate": db.get_exchange_rate(),
        "min_deposit_usd": round(min_c / 100, 2),
        "max_deposit_usd": round(max_c / 100, 2),
        "maintenance_mode": settings.get("maintenance_mode") == "1",
    }


@router.post("/deposits")
async def create_deposit(body: CreateDepositBody, request: Request, user: dict = Depends(current_user)):
    _rate_limit(user["id"], "create")

    if db.get_setting("maintenance_mode") == "1":
        raise HTTPException(503, "payments are temporarily paused for maintenance")

    amount_cents = round(body.amount_usd * 100)
    min_c, max_c = db.get_deposit_limits_cents()
    if amount_cents < min_c:
        raise HTTPException(400, f"minimum deposit is ${min_c/100:.2f}")
    if amount_cents > max_c:
        raise HTTPException(400, f"maximum deposit is ${max_c/100:.2f}")

    rate = db.get_exchange_rate()
    if rate <= 0:
        raise HTTPException(500, "exchange rate is not configured — contact an admin")
    bdt_cents = round(amount_cents / 100 * rate * 100)

    # Step 7: save the pending record before talking to the gateway, so a
    # gateway failure still leaves an auditable attempt and nothing is
    # invented client-side.
    deposit = db.create_deposit(user["id"], amount_cents, bdt_cents, rate)

    base = str(request.base_url).rstrip("/")
    success_url = f"{base}/api/payments/return/{deposit['id']}"
    cancel_url = f"{base}/api/payments/return/{deposit['id']}"

    try:
        result = await gateway.create_payment(
            # ZR Pay BD charges in BDT — this is the ৳ the user actually pays
            # at checkout, NOT the $ "coin" amount credited to their balance
            # on success (that's amount_cents, applied in credit_deposit_if_unverified).
            amount_bdt_cents=bdt_cents,
            success_url=success_url,
            cancel_url=cancel_url,
            user_id=user["id"],
            cus_name=user.get("username", ""),
            cus_email=user.get("email", ""),
            meta_data={"deposit_id": deposit["id"], "user_id": user["id"]},
        )
    except gateway.GatewayError as e:
        db.mark_deposit_status(deposit["id"], "failed", str(e))
        raise HTTPException(502, f"could not reach payment gateway: {e}")

    if not result.ok:
        db.mark_deposit_status(deposit["id"], "failed", result.message or "gateway declined")
        raise HTTPException(502, result.message or "gateway declined to create this payment")

    # ZR Pay BD's create response doesn't hand back its own transaction_id up
    # front (only on the redirect/verify side) — gateway_transaction_id gets
    # filled in from the return URL's query param once the user lands back
    # (see payment_return / attach_gateway_txn). Store payment_url now so the
    # frontend has somewhere to send the user.
    db.set_deposit_payment_url(deposit["id"], result.payment_url)
    deposit = db.get_deposit(deposit["id"])

    return {"status": "ok", "deposit": _public_deposit(deposit)}


async def _finalize_from_gateway(deposit_id: str, gw_txn_id: Optional[str]) -> dict:
    """Shared by the return-URL handler and the reconciliation poller. Looks
    the deposit up, calls the real verify endpoint (never trusts a query
    param's status), and credits exactly once via the atomic claim in db.py."""
    deposit = db.get_deposit(deposit_id)
    if not deposit:
        raise HTTPException(404, "deposit not found")

    if deposit["credited"]:
        return deposit  # already done — idempotent no-op, e.g. double return hit

    if gw_txn_id and not deposit["gateway_transaction_id"]:
        attached = db.attach_gateway_txn(deposit_id, gw_txn_id, deposit["payment_url"] or "")
        if attached:
            deposit = attached
        else:
            # another request already attached a (possibly different) txn id to
            # this deposit, or this gw txn id is already claimed elsewhere —
            # re-read and continue verifying whatever is actually on the row
            deposit = db.get_deposit(deposit_id)

    txn_to_verify = deposit.get("gateway_transaction_id") or gw_txn_id
    if not txn_to_verify:
        return deposit  # nothing to verify yet — still pending

    db.mark_deposit_status(deposit_id, "verifying")
    try:
        result = await gateway.verify_payment(txn_to_verify)
    except gateway.GatewayError as e:
        db.mark_deposit_status(deposit_id, "pending", f"verify error, will retry: {e}")
        return db.get_deposit(deposit_id)

    if not result.ok:
        db.mark_deposit_status(deposit_id, "pending", result.message or "verify returned no status")
        return db.get_deposit(deposit_id)

    if result.status == "COMPLETED":
        # amount check (req F): gateway's reported amount is in BDT (what the
        # user was actually billed) — compare against amount_bdt_cents, not
        # the USD coin amount that gets credited on success.
        expected = deposit["amount_bdt_cents"] / 100
        try:
            reported = float(result.amount_usd) if result.amount_usd is not None else expected
        except (TypeError, ValueError):
            reported = expected
        if abs(reported - expected) > 0.01:
            db.mark_deposit_status(deposit_id, "failed", f"amount mismatch: expected ৳{expected}, gateway reported {reported}")
            return db.get_deposit(deposit_id)

        credited = db.credit_deposit_if_unverified(deposit_id)
        if credited is None:
            # lost the race to a concurrent finalize — fine, the other caller
            # already credited it; return current state, not an error
            return db.get_deposit(deposit_id)
        return db.get_deposit(deposit_id)

    if result.status == "ERROR":
        db.mark_deposit_status(deposit_id, "failed", "gateway reported ERROR")
        return db.get_deposit(deposit_id)

    # PENDING — leave it pending, the poller or next return hit will retry
    db.mark_deposit_status(deposit_id, "pending")
    return db.get_deposit(deposit_id)


@router.get("/return/{deposit_id}")
async def payment_return(deposit_id: str, transactionId: Optional[str] = None,
                          status: Optional[str] = None):
    """success_url == cancel_url by design (req: never trust the browser
    redirect or a frontend success flag) — whichever one the gateway hits,
    this handler ignores the `status` query param entirely and does a real
    server-side verify() using transactionId. The frontend status shown to
    the user comes from that verify call via the deposit record, not from
    this redirect."""
    await _finalize_from_gateway(deposit_id, transactionId)
    # Hand off to the SPA, which polls GET /api/payments/deposits/{id} for
    # the verified status instead of trusting anything in this URL.
    return RedirectResponse(f"/dashboard?deposit={deposit_id}#redeem")


@router.get("/deposits/{deposit_id}")
async def get_deposit_status(deposit_id: str, user: dict = Depends(current_user)):
    _rate_limit(user["id"], "poll")
    deposit = db.get_deposit(deposit_id)
    if not deposit:
        raise HTTPException(404, "deposit not found")
    if deposit["user_id"] != user["id"] and user["role"] != "admin":
        raise HTTPException(403, "not your deposit")
    # Opportunistic re-verify on poll if still pending and old enough that the
    # return hit may never have landed (closed tab before redirect completed).
    if deposit["status"] in ("pending", "verifying") and deposit["gateway_transaction_id"] \
            and time.time() - deposit["updated_at"] > 5:
        deposit = await _finalize_from_gateway(deposit_id, None)
    return {"status": "ok", "deposit": _public_deposit(deposit)}


@router.get("/deposits")
async def my_deposits(user: dict = Depends(current_user)):
    deposits = db.list_deposits_for_user(user["id"])
    return {"status": "ok", "deposits": [_public_deposit(d) for d in deposits]}


async def reconcile_pending_loop():
    """Background task: re-verifies deposits stuck pending for >2 min, since
    the documented API has no push webhook to retry into — this plus the
    return-URL handler are the only two paths balance ever gets credited
    through, both funneling through the same atomic claim in db.py."""
    while True:
        try:
            stale = db.list_pending_deposits(older_than_seconds=120)
            for dep in stale[:20]:  # bound per cycle
                if dep["gateway_transaction_id"]:
                    try:
                        await _finalize_from_gateway(dep["id"], None)
                    except Exception:
                        pass  # keep the loop alive; next cycle retries
        except Exception:
            pass
        await asyncio.sleep(60)
