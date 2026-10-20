# -*- coding: utf-8 -*-
# app/core/gateway.py — ZR Pay BD client. Endpoints, headers and field names
# taken verbatim from https://zrpaybd.xyz/developers/docs#section-2 (2024-06-06
# revision). No undocumented params invented — this is the full documented
# surface: payment/create, payment/verify, three headers, that's it.
#
# The docs do not describe a signed server-to-server webhook — only a
# browser-redirect return URL carrying transactionId/status on the query
# string, plus a POST /verify you call with that transaction_id. Per the task
# spec ("never rely solely on the user's browser redirect"), this module is
# used exactly once per deposit from a trusted path: the server-side /return
# handler calls verify() itself using the gateway's own transaction_id, not
# the status query param. A background poller (payments.py) re-verifies any
# deposit that never got a return hit (closed tab, crash) — that's the
# reconciliation path since there's no webhook to retry into.
import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, Optional

import httpx

from app.core import db

log = logging.getLogger("zbr.gateway")

CREATE_URL = "https://pay.zrpaybd.xyz/api/payment/create"
VERIFY_URL = "https://pay.zrpaybd.xyz/api/payment/verify"

TIMEOUT = httpx.Timeout(connect=10.0, read=20.0, write=10.0, pool=10.0)


class GatewayError(Exception):
    """Raised on transport failure, timeout, or a malformed/unexpected gateway
    response. Never raised for a normal FALSE/error status — that's returned
    as a result object so callers can show the gateway's own message."""


@dataclass
class CreateResult:
    ok: bool
    payment_url: Optional[str] = None
    message: Optional[str] = None


@dataclass
class VerifyResult:
    ok: bool
    status: Optional[str] = None       # COMPLETED | PENDING | ERROR
    amount_usd: Optional[str] = None   # gateway's reported amount, as sent at create time
    cus_name: Optional[str] = None
    cus_email: Optional[str] = None
    message: Optional[str] = None


def _credentials() -> Dict[str, str]:
    """Env vars win if set (ops convention for secrets — never put these in
    source control); admin-panel settings are the fallback so the gateway is
    configurable without a redeploy, per spec. Secrets never reach the
    frontend: this function only runs server-side."""
    return {
        "api_key": os.environ.get("ZRPAY_API_KEY") or db.get_setting("zrpay_api_key", ""),
        "secret_key": os.environ.get("ZRPAY_SECRET_KEY") or db.get_setting("zrpay_secret_key", ""),
        "brand_key": os.environ.get("ZRPAY_BRAND_KEY") or db.get_setting("zrpay_brand_key", ""),
    }


def _headers() -> Dict[str, str]:
    creds = _credentials()
    return {
        "Content-Type": "application/json",
        "API-KEY": creds["api_key"],
        "SECRET-KEY": creds["secret_key"],
        "BRAND-KEY": creds["brand_key"],
    }


def is_configured() -> bool:
    creds = _credentials()
    return bool(creds["api_key"] and creds["secret_key"])


def _amount_str(amount_bdt_cents: int) -> str:
    # docs: "skip the trailing zeros in case the amount is a natural number" —
    # 1000 cents -> "10", 1050 cents -> "10.5"
    taka = amount_bdt_cents / 100
    s = f"{taka:.2f}".rstrip("0").rstrip(".")
    return s or "0"


async def create_payment(amount_bdt_cents: int, success_url: str, cancel_url: str,
                          user_id: str = "", cus_name: str = "", cus_email: str = "",
                          meta_data: Optional[Dict[str, Any]] = None) -> CreateResult:
    """ZR Pay BD is a BDT gateway — amount_bdt_cents is what the user is
    actually billed at checkout. It is NOT the USD "coin" amount credited to
    their site balance on success; that conversion/crediting happens entirely
    on our side (see payments.create_deposit / credit_deposit_if_unverified).
    Sending the USD figure here instead of the BDT one undercharges the user
    by the exchange rate.

    cus_name/cus_email are required by the gateway (confirmed against a
    working reference integration) even though the public docs page doesn't
    call them out as mandatory — omitting them is what was causing create to
    fail. Falls back to synthesized values keyed off user_id so this still
    works when the caller doesn't have a real name/email on hand."""
    if not is_configured():
        raise GatewayError("ZR Pay BD is not configured — set API/SECRET/BRAND keys in the admin panel or env")
    body = {
        "cus_name": cus_name or f"User {user_id or 'guest'}",
        "cus_email": cus_email or f"user{user_id or 'guest'}@zbrffglory.local",
        "success_url": success_url,
        "cancel_url": cancel_url,
        "amount": _amount_str(amount_bdt_cents),
    }
    if meta_data:
        body["metadata"] = meta_data
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            resp = await client.post(CREATE_URL, json=body, headers=_headers())
    except httpx.TimeoutException as e:
        raise GatewayError(f"gateway create timed out: {e}") from e
    except httpx.TransportError as e:
        raise GatewayError(f"gateway create network failure: {e}") from e

    log.info("zrpay create -> HTTP %s body=%s", resp.status_code, resp.text[:500])

    try:
        data = resp.json()
    except ValueError as e:
        raise GatewayError(f"gateway create returned non-JSON (HTTP {resp.status_code}): {resp.text[:300]}") from e
    if not isinstance(data, dict):
        raise GatewayError(f"gateway create returned unexpected response shape: {data!r}")
    # the reference integration checks status == 1 (int); the public docs
    # example showed a bare boolean. Accept both so neither shape breaks this.
    status = data.get("status")
    if status in (1, True, "1", "true", "True"):
        if not data.get("payment_url"):
            raise GatewayError(f"gateway create returned status=ok but no payment_url: {data!r}")
        return CreateResult(ok=True, payment_url=data.get("payment_url"), message=data.get("message"))
    return CreateResult(ok=False, message=data.get("message") or f"gateway rejected create (HTTP {resp.status_code}): {data!r}")


async def verify_payment(gateway_transaction_id: str) -> VerifyResult:
    if not is_configured():
        raise GatewayError("ZR Pay BD is not configured — set API/SECRET/BRAND keys in the admin panel or env")
    body = {"transaction_id": gateway_transaction_id}
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            resp = await client.post(VERIFY_URL, json=body, headers=_headers())
    except httpx.TimeoutException as e:
        raise GatewayError(f"gateway verify timed out: {e}") from e
    except httpx.TransportError as e:
        raise GatewayError(f"gateway verify network failure: {e}") from e

    log.info("zrpay verify -> HTTP %s body=%s", resp.status_code, resp.text[:500])

    try:
        data = resp.json()
    except ValueError as e:
        raise GatewayError(f"gateway verify returned non-JSON (HTTP {resp.status_code}): {resp.text[:300]}") from e
    if not isinstance(data, dict):
        raise GatewayError(f"gateway verify returned unexpected response shape: {data!r}")
    # error shape per docs: {"status": false, "message": "..."}
    if data.get("status") in (False, 0, "false", "False"):
        return VerifyResult(ok=False, message=data.get("message") or "verification failed")
    # success shape: {"status": "COMPLETED"|"PENDING"|"ERROR", "amount": "...", ...}
    gw_status = data.get("status")
    if gw_status not in ("COMPLETED", "PENDING", "ERROR"):
        raise GatewayError(f"gateway verify returned unrecognized status: {gw_status!r}")
    return VerifyResult(
        ok=True, status=gw_status, amount_usd=data.get("amount"),
        cus_name=data.get("cus_name"), cus_email=data.get("cus_email"),
    )
