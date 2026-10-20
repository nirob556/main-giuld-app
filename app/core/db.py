# -*- coding: utf-8 -*-
# app/core/db.py — SQLite store for users, balances, redeem codes.
# One file, WAL mode, short-lived connections per call (fine at this scale —
# admin panel + a handful of concurrent dashboard users, not a trading engine).
import os
import secrets
import sqlite3
import time
import uuid
from contextlib import contextmanager
from typing import Any, Dict, List, Optional

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "zbr.db")

# balances are stored as integer cents to avoid float drift on repeated
# add/remove/redeem operations. $1.00 == 100.
STARTER_BALANCE_CENTS = 0
CENTS_PER_CLIENT = 100  # $1 per squad/client, per spec

# settings keys bootstrapped on first boot — admin panel edits these, nothing
# below should ever be read as a hardcoded fallback once init_db() has run.
DEFAULT_SETTINGS = {
    "usd_to_bdt_rate": "110",          # 1 USD = this many BDT
    "min_deposit_usd": "1",
    "max_deposit_usd": "500",
    "zrpay_api_key": "",
    "zrpay_secret_key": "",
    "zrpay_brand_key": "",
    "zrpay_mode": "live",              # docs expose one (live) endpoint set
    "maintenance_mode": "0",
    "maintenance_message": "We'll be back shortly. Thanks for your patience.",
    "announcement": "",
    "site_name": "ZBR FF GLORY",
}


@contextmanager
def _conn():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                username TEXT UNIQUE NOT NULL,
                email TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                password_salt TEXT NOT NULL,
                balance_cents INTEGER NOT NULL DEFAULT 0,
                role TEXT NOT NULL DEFAULT 'user',
                created_at REAL NOT NULL,
                last_login_at REAL
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS redeem_codes (
                code TEXT PRIMARY KEY,
                value_cents INTEGER NOT NULL,
                created_by TEXT NOT NULL,
                created_at REAL NOT NULL,
                redeemed_by TEXT,
                redeemed_at REAL,
                FOREIGN KEY(created_by) REFERENCES users(id),
                FOREIGN KEY(redeemed_by) REFERENCES users(id)
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS balance_ledger (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                delta_cents INTEGER NOT NULL,
                reason TEXT NOT NULL,
                actor_id TEXT,
                created_at REAL NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id)
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at REAL NOT NULL,
                updated_by TEXT
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS deposits (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                amount_usd_cents INTEGER NOT NULL,
                amount_bdt_cents INTEGER NOT NULL,
                exchange_rate TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                gateway TEXT NOT NULL DEFAULT 'zrpaybd',
                gateway_transaction_id TEXT,
                payment_url TEXT,
                failure_reason TEXT,
                credited INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                verified_at REAL,
                FOREIGN KEY(user_id) REFERENCES users(id)
            )
        """)
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_deposits_gw_txn ON deposits(gateway_transaction_id) WHERE gateway_transaction_id IS NOT NULL")
        c.execute("CREATE INDEX IF NOT EXISTS idx_deposits_user ON deposits(user_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_deposits_status ON deposits(status)")
        c.execute("""
            CREATE TABLE IF NOT EXISTS admin_audit_log (
                id TEXT PRIMARY KEY,
                admin_id TEXT NOT NULL,
                action TEXT NOT NULL,
                target_type TEXT,
                target_id TEXT,
                detail TEXT,
                created_at REAL NOT NULL
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_audit_created ON admin_audit_log(created_at)")
        for k, v in DEFAULT_SETTINGS.items():
            c.execute(
                "INSERT OR IGNORE INTO settings (key, value, updated_at, updated_by) VALUES (?,?,?,NULL)",
                (k, v, time.time()),
            )
        # bootstrap one admin if the table is empty, so the panel is reachable
        # on first boot without editing source — credentials come from env,
        # matching the previous single-admin setup's convention.
        row = c.execute("SELECT COUNT(*) AS n FROM users").fetchone()
        if row["n"] == 0:
            from app.core.auth import hash_password
            admin_user = os.environ.get("ZBR_ADMIN_USER", "admin")
            admin_pass = os.environ.get("ZBR_ADMIN_PASS", "zbrglory2026")
            admin_email = os.environ.get("ZBR_ADMIN_EMAIL", "admin@zbr.local")
            pw_hash, pw_salt = hash_password(admin_pass)
            c.execute(
                "INSERT INTO users (id, username, email, password_hash, password_salt, balance_cents, role, created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (uuid.uuid4().hex[:16], admin_user, admin_email, pw_hash, pw_salt, 0, "admin", time.time()),
            )


# ---------------- users ----------------

def _row_to_user(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "id": row["id"], "username": row["username"], "email": row["email"],
        "balance_cents": row["balance_cents"], "role": row["role"],
        "created_at": row["created_at"], "last_login_at": row["last_login_at"],
    }


def create_user(username: str, email: str, password_hash: str, password_salt: str) -> Dict[str, Any]:
    uid = uuid.uuid4().hex[:16]
    with _conn() as c:
        c.execute(
            "INSERT INTO users (id, username, email, password_hash, password_salt, balance_cents, role, created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (uid, username.strip(), email.strip().lower(), password_hash, password_salt,
             STARTER_BALANCE_CENTS, "user", time.time()),
        )
        row = c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        return _row_to_user(row)


def get_user_by_id(user_id: str) -> Optional[Dict[str, Any]]:
    with _conn() as c:
        row = c.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        return _row_to_user(row) if row else None


def get_user_auth_row(login: str) -> Optional[sqlite3.Row]:
    """username or email -> full row (including hash/salt), for login only."""
    with _conn() as c:
        row = c.execute(
            "SELECT * FROM users WHERE username=? OR email=?", (login, login.strip().lower())
        ).fetchone()
        return row


def username_or_email_taken(username: str, email: str) -> bool:
    with _conn() as c:
        row = c.execute(
            "SELECT COUNT(*) AS n FROM users WHERE username=? OR email=?",
            (username.strip(), email.strip().lower()),
        ).fetchone()
        return row["n"] > 0


def touch_login(user_id: str):
    with _conn() as c:
        c.execute("UPDATE users SET last_login_at=? WHERE id=?", (time.time(), user_id))


def list_users() -> List[Dict[str, Any]]:
    with _conn() as c:
        rows = c.execute("SELECT * FROM users ORDER BY created_at DESC").fetchall()
        return [_row_to_user(r) for r in rows]


def adjust_balance(user_id: str, delta_cents: int, reason: str, actor_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Admin add/remove, or system debit/credit (redeem create/redeem claim,
    client-create charge). Clamped at 0 floor — never goes negative."""
    with _conn() as c:
        row = c.execute("SELECT balance_cents FROM users WHERE id=?", (user_id,)).fetchone()
        if not row:
            return None
        new_balance = max(0, row["balance_cents"] + delta_cents)
        c.execute("UPDATE users SET balance_cents=? WHERE id=?", (new_balance, user_id))
        c.execute(
            "INSERT INTO balance_ledger (id, user_id, delta_cents, reason, actor_id, created_at) VALUES (?,?,?,?,?,?)",
            (uuid.uuid4().hex[:16], user_id, delta_cents, reason, actor_id, time.time()),
        )
        urow = c.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        return _row_to_user(urow)


def set_balance(user_id: str, new_balance_cents: int, actor_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    with _conn() as c:
        row = c.execute("SELECT balance_cents FROM users WHERE id=?", (user_id,)).fetchone()
        if not row:
            return None
        delta = new_balance_cents - row["balance_cents"]
        c.execute("UPDATE users SET balance_cents=? WHERE id=?", (max(0, new_balance_cents), user_id))
        c.execute(
            "INSERT INTO balance_ledger (id, user_id, delta_cents, reason, actor_id, created_at) VALUES (?,?,?,?,?,?)",
            (uuid.uuid4().hex[:16], user_id, delta, "admin_set_balance", actor_id, time.time()),
        )
        urow = c.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        return _row_to_user(urow)


# ---------------- redeem codes ----------------

def _row_to_code(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "code": row["code"], "value_cents": row["value_cents"],
        "created_by": row["created_by"], "created_at": row["created_at"],
        "redeemed_by": row["redeemed_by"], "redeemed_at": row["redeemed_at"],
    }


def _gen_code() -> str:
    # ZBR-XXXX-XXXX-XXXX, uppercase alnum, collision retried by caller
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I ambiguity
    parts = ["".join(secrets.choice(alphabet) for _ in range(4)) for _ in range(3)]
    return "ZBR-" + "-".join(parts)


def create_redeem_code(value_cents: int, created_by: str) -> Dict[str, Any]:
    with _conn() as c:
        for _ in range(8):
            code = _gen_code()
            exists = c.execute("SELECT 1 FROM redeem_codes WHERE code=?", (code,)).fetchone()
            if not exists:
                break
        else:
            raise RuntimeError("could not generate a unique redeem code, retry")
        c.execute(
            "INSERT INTO redeem_codes (code, value_cents, created_by, created_at) VALUES (?,?,?,?)",
            (code, value_cents, created_by, time.time()),
        )
        row = c.execute("SELECT * FROM redeem_codes WHERE code=?", (code,)).fetchone()
        return _row_to_code(row)


def get_redeem_code(code: str) -> Optional[Dict[str, Any]]:
    with _conn() as c:
        row = c.execute("SELECT * FROM redeem_codes WHERE code=?", (code.strip().upper(),)).fetchone()
        return _row_to_code(row) if row else None


def mark_redeemed(code: str, redeemed_by: str) -> Optional[Dict[str, Any]]:
    """Atomic claim: only succeeds if still unredeemed (WHERE redeemed_by IS NULL),
    so two concurrent redeem attempts on the same code can't both succeed."""
    with _conn() as c:
        cur = c.execute(
            "UPDATE redeem_codes SET redeemed_by=?, redeemed_at=? WHERE code=? AND redeemed_by IS NULL",
            (redeemed_by, time.time(), code.strip().upper()),
        )
        if cur.rowcount == 0:
            return None
        row = c.execute("SELECT * FROM redeem_codes WHERE code=?", (code.strip().upper(),)).fetchone()
        return _row_to_code(row)


def list_codes_for_user(user_id: str) -> List[Dict[str, Any]]:
    """Codes this user created (their redeem-code history, shows who claimed each)."""
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM redeem_codes WHERE created_by=? ORDER BY created_at DESC", (user_id,)
        ).fetchall()
        return [_row_to_code(r) for r in rows]


def list_all_codes() -> List[Dict[str, Any]]:
    with _conn() as c:
        rows = c.execute("SELECT * FROM redeem_codes ORDER BY created_at DESC").fetchall()
        return [_row_to_code(r) for r in rows]


# ---------------- settings ----------------

def get_settings() -> Dict[str, str]:
    with _conn() as c:
        rows = c.execute("SELECT key, value FROM settings").fetchall()
        out = dict(DEFAULT_SETTINGS)
        out.update({r["key"]: r["value"] for r in rows})
        return out


def get_setting(key: str, default: Optional[str] = None) -> Optional[str]:
    with _conn() as c:
        row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else (default if default is not None else DEFAULT_SETTINGS.get(key))


def set_settings(values: Dict[str, str], updated_by: Optional[str] = None) -> None:
    with _conn() as c:
        for k, v in values.items():
            c.execute(
                "INSERT INTO settings (key, value, updated_at, updated_by) VALUES (?,?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at, updated_by=excluded.updated_by",
                (k, str(v), time.time(), updated_by),
            )


def get_exchange_rate() -> float:
    try:
        return float(get_setting("usd_to_bdt_rate", "110"))
    except (TypeError, ValueError):
        return 110.0


def get_deposit_limits_cents() -> tuple:
    try:
        mn = round(float(get_setting("min_deposit_usd", "1")) * 100)
        mx = round(float(get_setting("max_deposit_usd", "500")) * 100)
    except (TypeError, ValueError):
        mn, mx = 100, 50000
    return mn, mx


# ---------------- deposits ----------------

def _row_to_deposit(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "id": row["id"], "user_id": row["user_id"],
        "amount_usd_cents": row["amount_usd_cents"], "amount_bdt_cents": row["amount_bdt_cents"],
        "exchange_rate": row["exchange_rate"], "status": row["status"], "gateway": row["gateway"],
        "gateway_transaction_id": row["gateway_transaction_id"], "payment_url": row["payment_url"],
        "failure_reason": row["failure_reason"], "credited": bool(row["credited"]),
        "created_at": row["created_at"], "updated_at": row["updated_at"], "verified_at": row["verified_at"],
    }


def create_deposit(user_id: str, amount_usd_cents: int, amount_bdt_cents: int, exchange_rate: float) -> Dict[str, Any]:
    dep_id = uuid.uuid4().hex
    now = time.time()
    with _conn() as c:
        c.execute(
            "INSERT INTO deposits (id, user_id, amount_usd_cents, amount_bdt_cents, exchange_rate, status, created_at, updated_at) "
            "VALUES (?,?,?,?,?,'pending',?,?)",
            (dep_id, user_id, amount_usd_cents, amount_bdt_cents, str(exchange_rate), now, now),
        )
        row = c.execute("SELECT * FROM deposits WHERE id=?", (dep_id,)).fetchone()
        return _row_to_deposit(row)


def get_deposit(deposit_id: str) -> Optional[Dict[str, Any]]:
    with _conn() as c:
        row = c.execute("SELECT * FROM deposits WHERE id=?", (deposit_id,)).fetchone()
        return _row_to_deposit(row) if row else None


def get_deposit_by_gateway_txn(gateway_transaction_id: str) -> Optional[Dict[str, Any]]:
    with _conn() as c:
        row = c.execute("SELECT * FROM deposits WHERE gateway_transaction_id=?", (gateway_transaction_id,)).fetchone()
        return _row_to_deposit(row) if row else None


def attach_gateway_txn(deposit_id: str, gateway_transaction_id: str, payment_url: str) -> Optional[Dict[str, Any]]:
    """Called right after the gateway create() call returns, before redirecting the
    user — this is what lets verify() look a pending deposit up by gateway txn id,
    and what the unique index on gateway_transaction_id relies on for idempotency."""
    with _conn() as c:
        try:
            c.execute(
                "UPDATE deposits SET gateway_transaction_id=?, payment_url=?, updated_at=? WHERE id=?",
                (gateway_transaction_id, payment_url, time.time(), deposit_id),
            )
        except sqlite3.IntegrityError:
            return None  # another deposit already claimed this gateway txn id
        row = c.execute("SELECT * FROM deposits WHERE id=?", (deposit_id,)).fetchone()
        return _row_to_deposit(row) if row else None


def set_deposit_payment_url(deposit_id: str, payment_url: str) -> Optional[Dict[str, Any]]:
    with _conn() as c:
        c.execute("UPDATE deposits SET payment_url=?, updated_at=? WHERE id=?", (payment_url, time.time(), deposit_id))
        row = c.execute("SELECT * FROM deposits WHERE id=?", (deposit_id,)).fetchone()
        return _row_to_deposit(row) if row else None


def mark_deposit_status(deposit_id: str, status: str, failure_reason: Optional[str] = None) -> Optional[Dict[str, Any]]:
    with _conn() as c:
        c.execute(
            "UPDATE deposits SET status=?, failure_reason=?, updated_at=? WHERE id=?",
            (status, failure_reason, time.time(), deposit_id),
        )
        row = c.execute("SELECT * FROM deposits WHERE id=?", (deposit_id,)).fetchone()
        return _row_to_deposit(row) if row else None


def credit_deposit_if_unverified(deposit_id: str) -> Optional[Dict[str, Any]]:
    """Atomic claim-and-credit: only the first caller to flip credited 0->1 for a
    still-pending/verifying deposit actually adjusts the balance. A duplicate
    callback, a duplicate verify poll, and a second browser tab on the return
    URL all converge here and only one wins the UPDATE — same pattern as
    mark_redeemed()'s WHERE redeemed_by IS NULL. This is the single choke point
    that makes "opening the payment URL twice never double-credits" true.
    Does the balance update inline in the same connection/transaction as the
    claim (not via adjust_balance(), which opens its own connection and would
    deadlock against this one under sqlite's single-writer lock)."""
    with _conn() as c:
        cur = c.execute(
            "UPDATE deposits SET credited=1, status='success', verified_at=?, updated_at=? "
            "WHERE id=? AND credited=0 AND status IN ('pending','verifying')",
            (time.time(), time.time(), deposit_id),
        )
        if cur.rowcount == 0:
            return None  # already credited (or not in a creditable state) — caller must not credit again
        row = c.execute("SELECT * FROM deposits WHERE id=?", (deposit_id,)).fetchone()
        if not row:
            return None
        dep = _row_to_deposit(row)

        urow = c.execute("SELECT balance_cents FROM users WHERE id=?", (dep["user_id"],)).fetchone()
        if not urow:
            raise RuntimeError(f"deposit {deposit_id} credited=1 but owning user {dep['user_id']} is gone")
        new_balance = max(0, urow["balance_cents"] + dep["amount_usd_cents"])
        c.execute("UPDATE users SET balance_cents=? WHERE id=?", (new_balance, dep["user_id"]))
        c.execute(
            "INSERT INTO balance_ledger (id, user_id, delta_cents, reason, actor_id, created_at) VALUES (?,?,?,?,?,?)",
            (uuid.uuid4().hex[:16], dep["user_id"], dep["amount_usd_cents"], "deposit_credit", None, time.time()),
        )
        new_user = _row_to_user(c.execute("SELECT * FROM users WHERE id=?", (dep["user_id"],)).fetchone())
        return {**dep, "user_after": new_user}


def list_deposits_for_user(user_id: str, limit: int = 100) -> List[Dict[str, Any]]:
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM deposits WHERE user_id=? ORDER BY created_at DESC LIMIT ?", (user_id, limit)
        ).fetchall()
        return [_row_to_deposit(r) for r in rows]


def list_all_deposits(status: Optional[str] = None, search: Optional[str] = None,
                       limit: int = 50, offset: int = 0) -> Dict[str, Any]:
    with _conn() as c:
        where = []
        params: list = []
        if status and status != "all":
            where.append("d.status=?")
            params.append(status)
        if search:
            where.append("(d.id LIKE ? OR d.gateway_transaction_id LIKE ? OR u.username LIKE ?)")
            like = f"%{search}%"
            params += [like, like, like]
        where_sql = ("WHERE " + " AND ".join(where)) if where else ""
        total = c.execute(
            f"SELECT COUNT(*) AS n FROM deposits d JOIN users u ON u.id=d.user_id {where_sql}", params
        ).fetchone()["n"]
        rows = c.execute(
            f"SELECT d.*, u.username FROM deposits d JOIN users u ON u.id=d.user_id {where_sql} "
            f"ORDER BY d.created_at DESC LIMIT ? OFFSET ?",
            params + [limit, offset],
        ).fetchall()
        out = []
        for r in rows:
            dep = _row_to_deposit(r)
            dep["username"] = r["username"]
            out.append(dep)
        return {"deposits": out, "total": total}


def list_pending_deposits(older_than_seconds: float = 0) -> List[Dict[str, Any]]:
    cutoff = time.time() - older_than_seconds
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM deposits WHERE status IN ('pending','verifying') AND created_at <= ? ORDER BY created_at ASC",
            (cutoff,),
        ).fetchall()
        return [_row_to_deposit(r) for r in rows]


def deposit_stats() -> Dict[str, Any]:
    with _conn() as c:
        row = c.execute("""
            SELECT
              COUNT(*) AS total_tx,
              COALESCE(SUM(CASE WHEN status='success' THEN amount_usd_cents ELSE 0 END), 0) AS total_revenue_cents,
              COALESCE(SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END), 0) AS pending_count
            FROM deposits
        """).fetchone()
        return {
            "total_transactions": row["total_tx"],
            "total_revenue_cents": row["total_revenue_cents"],
            "pending_count": row["pending_count"],
        }


# ---------------- admin audit log ----------------

def log_admin_action(admin_id: str, action: str, target_type: Optional[str] = None,
                      target_id: Optional[str] = None, detail: Optional[str] = None) -> None:
    with _conn() as c:
        c.execute(
            "INSERT INTO admin_audit_log (id, admin_id, action, target_type, target_id, detail, created_at) VALUES (?,?,?,?,?,?,?)",
            (uuid.uuid4().hex[:16], admin_id, action, target_type, target_id, detail, time.time()),
        )


def list_audit_log(limit: int = 100, offset: int = 0) -> Dict[str, Any]:
    with _conn() as c:
        total = c.execute("SELECT COUNT(*) AS n FROM admin_audit_log").fetchone()["n"]
        rows = c.execute(
            "SELECT a.*, u.username AS admin_username FROM admin_audit_log a "
            "LEFT JOIN users u ON u.id=a.admin_id ORDER BY a.created_at DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
        out = [{
            "id": r["id"], "admin_id": r["admin_id"], "admin_username": r["admin_username"] or "(deleted)",
            "action": r["action"], "target_type": r["target_type"], "target_id": r["target_id"],
            "detail": r["detail"], "created_at": r["created_at"],
        } for r in rows]
        return {"entries": out, "total": total}


# ---------------- user search / pagination / dashboard stats ----------------

def list_users_paginated(search: Optional[str] = None, limit: int = 25, offset: int = 0) -> Dict[str, Any]:
    with _conn() as c:
        where = ""
        params: list = []
        if search:
            where = "WHERE username LIKE ? OR email LIKE ? OR id LIKE ?"
            like = f"%{search}%"
            params = [like, like, like]
        total = c.execute(f"SELECT COUNT(*) AS n FROM users {where}", params).fetchone()["n"]
        rows = c.execute(
            f"SELECT * FROM users {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
            params + [limit, offset],
        ).fetchall()
        return {"users": [_row_to_user(r) for r in rows], "total": total}


def dashboard_stats() -> Dict[str, Any]:
    with _conn() as c:
        users_row = c.execute("SELECT COUNT(*) AS n FROM users").fetchone()
        active_row = c.execute("SELECT COUNT(*) AS n FROM users WHERE last_login_at > ?", (time.time() - 7 * 86400,)).fetchone()
        dep_row = c.execute("""
            SELECT
              COALESCE(SUM(CASE WHEN status='success' THEN amount_usd_cents ELSE 0 END), 0) AS revenue_cents,
              COALESCE(SUM(CASE WHEN status='success' THEN 1 ELSE 0 END), 0) AS total_deposits,
              COALESCE(SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END), 0) AS pending_payments
            FROM deposits
        """).fetchone()
        return {
            "total_users": users_row["n"],
            "active_users_7d": active_row["n"],
            "total_deposits": dep_row["total_deposits"],
            "total_revenue_cents": dep_row["revenue_cents"],
            "pending_payments": dep_row["pending_payments"],
        }
