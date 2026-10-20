# -*- coding: utf-8 -*-
# app/api/redeem.py — user-facing redeem code system.
# create: spend N dollars of your own balance to mint a code worth N dollars.
#   if nobody redeems it, your balance stays down (the value is "in" the code,
#   not gone) — if you redeem your own code back the dollars return to you,
#   same as anyone else redeeming it. We don't special-case self-redeem.
# redeem: spend a code, credit its value to your balance. Atomic claim in
#   db.mark_redeemed() means a code can only ever be redeemed once even
#   under concurrent requests.
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator

from app.core import db
from app.core.auth import current_user

router = APIRouter(prefix="/api/redeem", tags=["redeem"])


class CreateCodeBody(BaseModel):
    amount: float

    @field_validator("amount")
    @classmethod
    def _v_amount(cls, v):
        if v <= 0:
            raise ValueError("amount must be greater than 0")
        return round(v, 2)


class RedeemCodeBody(BaseModel):
    code: str


def _public_code(c: dict) -> dict:
    return {
        "code": c["code"], "value": round(c["value_cents"] / 100, 2),
        "created_at": c["created_at"], "redeemed_by": c["redeemed_by"],
        "redeemed_at": c["redeemed_at"], "is_redeemed": c["redeemed_by"] is not None,
    }


@router.post("/create")
async def create_code(body: CreateCodeBody, user: dict = Depends(current_user)):
    amount_cents = round(body.amount * 100)
    fresh = db.get_user_by_id(user["id"])
    if fresh["balance_cents"] < amount_cents:
        raise HTTPException(402, f"insufficient balance — you have ${fresh['balance_cents']/100:.2f}")
    db.adjust_balance(user["id"], -amount_cents, "redeem_code_create", actor_id=user["id"])
    code = db.create_redeem_code(amount_cents, created_by=user["id"])
    return {"status": "ok", "code": _public_code(code)}


@router.post("/claim")
async def claim_code(body: RedeemCodeBody, user: dict = Depends(current_user)):
    raw = body.code.strip().upper()
    existing = db.get_redeem_code(raw)
    if not existing:
        raise HTTPException(404, "invalid redeem code")
    if existing["redeemed_by"] is not None:
        raise HTTPException(409, "this code has already been redeemed")
    claimed = db.mark_redeemed(raw, redeemed_by=user["id"])
    if not claimed:
        # lost the race to another concurrent claim on the same code
        raise HTTPException(409, "this code has already been redeemed")
    user_after = db.adjust_balance(user["id"], claimed["value_cents"], "redeem_code_claim", actor_id=user["id"])
    return {
        "status": "ok",
        "credited": round(claimed["value_cents"] / 100, 2),
        "new_balance": round(user_after["balance_cents"] / 100, 2),
    }


@router.get("/mine")
async def my_codes(user: dict = Depends(current_user)):
    codes = db.list_codes_for_user(user["id"])
    users_cache = {}

    def _redeemer_name(uid):
        if not uid:
            return None
        if uid not in users_cache:
            u = db.get_user_by_id(uid)
            users_cache[uid] = u["username"] if u else "(deleted user)"
        return users_cache[uid]

    out = []
    for c in codes:
        pc = _public_code(c)
        pc["redeemed_by_username"] = _redeemer_name(c["redeemed_by"])
        out.append(pc)
    return {"status": "ok", "codes": out}
