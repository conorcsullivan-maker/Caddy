"""Public auth endpoints: signup, login, logout, current user."""
from typing import Optional

from fastapi import APIRouter, Cookie, Depends, HTTPException, Response
from pydantic import BaseModel, Field

from db import db, now_iso
from deps import get_current_user
from security import (
    COOKIE_SAMESITE, COOKIE_SECURE, SESSION_MAX_AGE_DAYS,
    generate_token, hash_pin_secure, is_legacy_hash, verify_pin,
)
from store import user_dict

router = APIRouter()


class SignupRequest(BaseModel):
    full_name: str = Field(min_length=1, max_length=80)
    username: str = Field(min_length=3, max_length=30, pattern=r"^[a-zA-Z0-9_]+$")
    email: str = Field(min_length=3, max_length=120)
    phone: Optional[str] = None
    reason: Optional[str] = Field(None, max_length=500)
    referral: Optional[str] = Field(None, max_length=200)


class LoginRequest(BaseModel):
    username: str
    pin: str


@router.post("/api/signup")
def signup(payload: SignupRequest):
    username = payload.username.lower().strip()
    with db() as conn:
        existing = conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
        if existing:
            raise HTTPException(400, "That username is already taken")
        conn.execute("""
            INSERT INTO users (username, full_name, email, phone, reason, referral, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)
        """, (
            username, payload.full_name.strip(), payload.email.strip(),
            payload.phone, payload.reason, payload.referral, now_iso()
        ))
    return {"status": "pending", "message": "Your request has been received. You'll hear back soon."}


@router.post("/api/login")
def login(payload: LoginRequest, response: Response):
    username = payload.username.lower().strip()
    with db() as conn:
        row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        if not row:
            raise HTTPException(401, "Username not found")
        if row["status"] == "pending":
            raise HTTPException(403, "Your request is still pending approval")
        if row["status"] == "rejected":
            raise HTTPException(403, "This account is not active")
        if not verify_pin(payload.pin, row["pin_hash"] or ""):
            raise HTTPException(401, "Incorrect PIN")
        # Transparent hash upgrade: users created before salted PBKDF2 get
        # their stored hash rewritten on first successful login.
        if is_legacy_hash(row["pin_hash"]):
            conn.execute(
                "UPDATE users SET pin_hash = ? WHERE id = ?",
                (hash_pin_secure(payload.pin), row["id"]),
            )
        # create session
        token = generate_token()
        conn.execute("INSERT INTO sessions (token, user_id, created_at) VALUES (?, ?, ?)",
                     (token, row["id"], now_iso()))
        user = user_dict(row)

    response.set_cookie(
        key="session",
        value=token,
        httponly=True,
        samesite=COOKIE_SAMESITE,
        max_age=60 * 60 * 24 * SESSION_MAX_AGE_DAYS,
        secure=COOKIE_SECURE,
    )
    # The token also rides in the body for the native app, which stores it in
    # the keychain and authenticates with "Authorization: Bearer <token>"
    # instead of cookies. The web client ignores this field.
    return {"user": user, "token": token}


@router.post("/api/logout")
def logout(response: Response, session: Optional[str] = Cookie(None)):
    if session:
        with db() as conn:
            conn.execute("DELETE FROM sessions WHERE token = ?", (session,))
    response.delete_cookie("session")
    return {"status": "ok"}


@router.get("/api/me")
def me(user: dict = Depends(get_current_user)):
    return {"user": user}
