# -*- coding: utf-8 -*-
# app/api/auth.py — register / login / logout / who am I

import re

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, field_validator

from app.core import db
from app.core.auth import (
    COOKIE_NAME,
    SESSION_TTL,
    current_user,
    hash_password,
    make_session_token,
    verify_password,
)

router = APIRouter(prefix="/api/auth", tags=["auth"])

USERNAME_RE = re.compile(r"^[a-zA-Z0-9_]{3,20}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class RegisterBody(BaseModel):
    username: str
    email: str
    password: str

    @field_validator("username")
    @classmethod
    def _v_username(cls, v):
        v = v.strip()
        if not USERNAME_RE.fullmatch(v):
            raise ValueError(
                "username must be 3-20 characters: letters, numbers, underscore"
            )
        return v

    @field_validator("email")
    @classmethod
    def _v_email(cls, v):
        v = v.strip().lower()
        if not EMAIL_RE.fullmatch(v):
            raise ValueError("enter a valid email address")
        return v

    @field_validator("password")
    @classmethod
    def _v_password(cls, v):
        if len(v) < 6:
            raise ValueError("password must be at least 6 characters")
        return v


class LoginBody(BaseModel):
    login: str
    password: str


def _set_session_cookie(response: Response, user_id: str, role: str):
    token = make_session_token(user_id, role)
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        max_age=SESSION_TTL,
        httponly=True,
        samesite="lax",
        secure=True,
    )


@router.post("/register")
async def register(body: RegisterBody, response: Response):
    if db.username_or_email_taken(body.username, body.email):
        return {
            "status": "error",
            "error": "that username or email is already registered",
        }

    pw_hash, pw_salt = hash_password(body.password)
    user = db.create_user(
        body.username,
        body.email,
        pw_hash,
        pw_salt,
    )

    _set_session_cookie(response, user["id"], user["role"])

    return {
        "status": "ok",
        "user": _public_user(user),
    }


@router.post("/login")
async def login(body: LoginBody, response: Response):
    row = db.get_user_auth_row(body.login.strip())

    if not row or not verify_password(
        body.password,
        row["password_hash"],
        row["password_salt"],
    ):
        return {
            "status": "error",
            "error": "invalid username/email or password",
        }

    db.touch_login(row["id"])
    _set_session_cookie(response, row["id"], row["role"])

    user = db.get_user_by_id(row["id"])

    return {
        "status": "ok",
        "user": _public_user(user),
    }


@router.post("/logout")
async def logout(response: Response):
    response.delete_cookie(COOKIE_NAME)
    return {"status": "ok"}


@router.get("/me")
async def me(user: dict = Depends(current_user)):
    return {
        "status": "ok",
        "user": _public_user(user),
    }


def _public_user(user: dict) -> dict:
    return {
        "id": user["id"],
        "username": user["username"],
        "email": user["email"],
        "balance_cents": user["balance_cents"],
        "balance": round(user["balance_cents"] / 100, 2),
        "role": user["role"],
    }
