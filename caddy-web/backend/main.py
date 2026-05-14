"""
Caddy Web Backend — FastAPI + SQLite
Beta-gated authentication with admin approval flow.
"""
import os
import json
import sqlite3
import secrets
import hashlib
from datetime import datetime, timezone
from typing import Optional
from contextlib import contextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response, Cookie, Depends, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, Response as FastAPIResponse
from pydantic import BaseModel, Field
from dotenv import load_dotenv
# Load .env from the project root (../../.env) for local dev.
# In production, env vars are injected by the host (Render), so missing .env is fine.
load_dotenv(Path(__file__).parent.parent.parent / ".env", override=True)

from caddy_engine import caddy_reply, transcribe_audio, synthesize_speech

# ────────────────────────────────────────────────────────────
# Setup
# ────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
# Allow DB to live on a persistent disk in production (Render mounts at /data)
DATA_DIR = Path(os.environ.get("DATA_DIR", BASE_DIR))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "caddy.db"

app = FastAPI(title="Caddy API", version="0.1.0")

# Production: set FRONTEND_ORIGIN env var to the deployed Vercel URL.
# Dev: regex allows localhost + any local network IP.
PROD_FRONTEND_ORIGIN = os.environ.get("FRONTEND_ORIGIN", "").strip()
ALLOWED_ORIGINS = [PROD_FRONTEND_ORIGIN] if PROD_FRONTEND_ORIGIN else []
LOCAL_ORIGIN_REGEX = r"http://(localhost|127\.0\.0\.1|10\.\d+\.\d+\.\d+|192\.168\.\d+\.\d+|172\.(1[6-9]|2\d|3[01])\.\d+\.\d+):\d+"

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_origin_regex=LOCAL_ORIGIN_REGEX,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Cookie security: secure HTTPS-only in production, plain in local dev.
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "false").lower() == "true"
COOKIE_SAMESITE = "none" if COOKIE_SECURE else "lax"


# ────────────────────────────────────────────────────────────
# Database
# ────────────────────────────────────────────────────────────
def seed_initial_admin():
    """If BOOTSTRAP_ADMIN_* env vars are set and no admins exist yet,
    create that user as an approved admin. Lets you bootstrap a fresh
    production database without using a shell."""
    username = os.environ.get("BOOTSTRAP_ADMIN_USERNAME", "").strip().lower()
    pin = os.environ.get("BOOTSTRAP_ADMIN_PIN", "").strip()
    full_name = os.environ.get("BOOTSTRAP_ADMIN_NAME", "Admin").strip()
    if not username or not pin:
        return
    with db() as conn:
        admin_count = conn.execute(
            "SELECT COUNT(*) FROM users WHERE is_admin = 1"
        ).fetchone()[0]
        if admin_count > 0:
            return
        existing = conn.execute(
            "SELECT id FROM users WHERE username = ?", (username,)
        ).fetchone()
        if existing:
            # Promote existing user to admin
            conn.execute(
                "UPDATE users SET is_admin = 1, status = 'approved' WHERE username = ?",
                (username,),
            )
            print(f"Promoted existing user '{username}' to admin")
        else:
            conn.execute(
                """INSERT INTO users
                   (username, pin_hash, full_name, status, is_admin, onboarded, created_at, approved_at)
                   VALUES (?, ?, ?, 'approved', 1, 0, ?, ?)""",
                (username, hash_pin(pin), full_name, now_iso(), now_iso()),
            )
            print(f"Bootstrapped admin user '{username}'")


def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            pin_hash TEXT,
            full_name TEXT NOT NULL,
            email TEXT,
            phone TEXT,
            reason TEXT,
            referral TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            is_admin INTEGER DEFAULT 0,
            onboarded INTEGER DEFAULT 0,
            created_at TEXT NOT NULL,
            approved_at TEXT,
            bag TEXT,
            driver_miss TEXT,
            iron_miss TEXT,
            home_course TEXT,
            rounds TEXT,
            handicap_index REAL,
            tendencies_summary TEXT
        )
    """)
    # Add referral column to existing tables (migration safety)
    try:
        c.execute("ALTER TABLE users ADD COLUMN referral TEXT")
    except sqlite3.OperationalError:
        pass  # already exists
    c.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            token TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        )
    """)
    conn.commit()
    conn.close()


@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# ────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────
def hash_pin(pin: str) -> str:
    return hashlib.sha256(pin.encode()).hexdigest()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def generate_pin() -> str:
    """Generate a random 4-digit PIN."""
    return f"{secrets.randbelow(10000):04d}"


def generate_token() -> str:
    return secrets.token_urlsafe(32)


def user_dict(row: sqlite3.Row) -> dict:
    """Convert DB row to safe dict (no pin_hash)."""
    d = dict(row)
    d.pop("pin_hash", None)
    for json_field in ("bag", "rounds"):
        if d.get(json_field):
            try:
                d[json_field] = json.loads(d[json_field])
            except Exception:
                pass
    d["is_admin"] = bool(d.get("is_admin"))
    d["onboarded"] = bool(d.get("onboarded"))
    return d


# ────────────────────────────────────────────────────────────
# Auth dependency
# ────────────────────────────────────────────────────────────
def get_current_user(session: Optional[str] = Cookie(None)) -> dict:
    if not session:
        raise HTTPException(401, "Not signed in")
    with db() as conn:
        row = conn.execute("""
            SELECT u.* FROM users u
            JOIN sessions s ON s.user_id = u.id
            WHERE s.token = ?
        """, (session,)).fetchone()
    if not row:
        raise HTTPException(401, "Invalid session")
    if row["status"] != "approved":
        # Account was deactivated/rejected — kill this session
        with db() as conn2:
            conn2.execute("DELETE FROM sessions WHERE token = ?", (session,))
        raise HTTPException(403, "Account is no longer active")
    return user_dict(row)


def require_admin(user: dict = Depends(get_current_user)) -> dict:
    if not user.get("is_admin"):
        raise HTTPException(403, "Admin access required")
    return user


# ────────────────────────────────────────────────────────────
# Models
# ────────────────────────────────────────────────────────────
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


class BagSetupRequest(BaseModel):
    bag: dict[str, Optional[int]]
    driver_miss: Optional[str] = Field(None, max_length=300)
    iron_miss: Optional[str] = Field(None, max_length=300)
    home_course: Optional[str] = Field(None, max_length=120)


class CaddyMessageRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)


# Limit conversation history to last N messages to keep Claude context manageable.
MAX_HISTORY = 60


def load_conversation(user_id: int) -> list[dict]:
    with db() as conn:
        row = conn.execute(
            "SELECT conversation_history FROM users WHERE id = ?", (user_id,)
        ).fetchone()
    if not row or not row["conversation_history"]:
        return []
    try:
        return json.loads(row["conversation_history"])
    except Exception:
        return []


def save_conversation(user_id: int, history: list[dict]):
    trimmed = history[-MAX_HISTORY:]
    with db() as conn:
        conn.execute(
            "UPDATE users SET conversation_history = ? WHERE id = ?",
            (json.dumps(trimmed), user_id),
        )


# ────────────────────────────────────────────────────────────
# Public endpoints
# ────────────────────────────────────────────────────────────
@app.get("/api/health")
def health():
    return {"status": "ok", "service": "caddy-api"}


@app.post("/api/signup")
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


@app.post("/api/login")
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
        if not row["pin_hash"] or row["pin_hash"] != hash_pin(payload.pin):
            raise HTTPException(401, "Incorrect PIN")
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
        max_age=60 * 60 * 24 * 30,  # 30 days
        secure=COOKIE_SECURE,
    )
    return {"user": user}


@app.post("/api/logout")
def logout(response: Response, session: Optional[str] = Cookie(None)):
    if session:
        with db() as conn:
            conn.execute("DELETE FROM sessions WHERE token = ?", (session,))
    response.delete_cookie("session")
    return {"status": "ok"}


@app.get("/api/me")
def me(user: dict = Depends(get_current_user)):
    return {"user": user}


@app.post("/api/me/setup")
def complete_setup(payload: BagSetupRequest, user: dict = Depends(get_current_user)):
    """Save bag distances + miss tendencies + home course. Marks user as onboarded."""
    bag_json = json.dumps(payload.bag)
    with db() as conn:
        conn.execute("""
            UPDATE users SET
                bag = ?, driver_miss = ?, iron_miss = ?, home_course = ?, onboarded = 1
            WHERE id = ?
        """, (
            bag_json,
            payload.driver_miss.strip() if payload.driver_miss else None,
            payload.iron_miss.strip() if payload.iron_miss else None,
            payload.home_course.strip() if payload.home_course else None,
            user["id"],
        ))
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user["id"],)).fetchone()
    return {"user": user_dict(row)}


# ────────────────────────────────────────────────────────────
# Caddy chat endpoints
# ────────────────────────────────────────────────────────────
@app.get("/api/caddy/history")
def get_history(user: dict = Depends(get_current_user)):
    return {"history": load_conversation(user["id"])}


@app.post("/api/caddy/reset")
def reset_history(user: dict = Depends(get_current_user)):
    save_conversation(user["id"], [])
    return {"status": "reset"}


@app.post("/api/caddy/message")
def caddy_message(payload: CaddyMessageRequest, user: dict = Depends(get_current_user)):
    """Text message → Claude → returns response text."""
    history = load_conversation(user["id"])
    reply = caddy_reply(user, history, payload.message)
    history.append({"role": "user", "content": payload.message})
    history.append({"role": "assistant", "content": reply})
    save_conversation(user["id"], history)
    return {"reply": reply, "user_message": payload.message}


@app.post("/api/caddy/voice")
async def caddy_voice(
    audio: UploadFile = File(...),
    user: dict = Depends(get_current_user),
):
    """Audio in → Whisper transcript → Claude → returns transcript + reply text."""
    audio_bytes = await audio.read()
    if not audio_bytes:
        raise HTTPException(400, "Empty audio")
    transcript = transcribe_audio(audio_bytes, audio.filename or "audio.webm")
    if not transcript:
        raise HTTPException(400, "Could not understand audio")
    history = load_conversation(user["id"])
    reply = caddy_reply(user, history, transcript)
    history.append({"role": "user", "content": transcript})
    history.append({"role": "assistant", "content": reply})
    save_conversation(user["id"], history)
    return {"transcript": transcript, "reply": reply}


@app.post("/api/caddy/speak")
def caddy_speak(payload: CaddyMessageRequest, user: dict = Depends(get_current_user)):
    """Generate TTS audio for the given text (used for replaying responses)."""
    audio = synthesize_speech(payload.message)
    return FastAPIResponse(content=audio, media_type="audio/mpeg")


# ────────────────────────────────────────────────────────────
# Admin endpoints
# ────────────────────────────────────────────────────────────
@app.get("/api/admin/pending")
def list_pending(user: dict = Depends(require_admin)):
    with db() as conn:
        rows = conn.execute("""
            SELECT id, username, full_name, email, phone, reason, referral, created_at
            FROM users WHERE status = 'pending' ORDER BY created_at ASC
        """).fetchall()
    return {"pending": [dict(r) for r in rows]}


@app.get("/api/admin/users")
def list_all_users(user: dict = Depends(require_admin)):
    with db() as conn:
        rows = conn.execute("""
            SELECT id, username, full_name, email, phone, status, is_admin, onboarded,
                   created_at, approved_at, handicap_index
            FROM users ORDER BY created_at DESC
        """).fetchall()
    return {"users": [dict(r) for r in rows]}


@app.post("/api/admin/approve/{user_id}")
def approve_user(user_id: int, admin: dict = Depends(require_admin)):
    new_pin = generate_pin()
    with db() as conn:
        row = conn.execute("SELECT username, status FROM users WHERE id = ?", (user_id,)).fetchone()
        if not row:
            raise HTTPException(404, "User not found")
        if row["status"] != "pending":
            raise HTTPException(400, f"User is currently {row['status']}, not pending")
        conn.execute("""
            UPDATE users SET status = 'approved', approved_at = ?, pin_hash = ?
            WHERE id = ?
        """, (now_iso(), hash_pin(new_pin), user_id))
    return {"username": row["username"], "pin": new_pin}


@app.post("/api/admin/reject/{user_id}")
def reject_user(user_id: int, admin: dict = Depends(require_admin)):
    with db() as conn:
        row = conn.execute("SELECT id FROM users WHERE id = ?", (user_id,)).fetchone()
        if not row:
            raise HTTPException(404, "User not found")
        conn.execute("UPDATE users SET status = 'rejected' WHERE id = ?", (user_id,))
    return {"status": "rejected"}


class ProfileImportRequest(BaseModel):
    bag: Optional[dict] = None
    driver_miss: Optional[str] = None
    iron_miss: Optional[str] = None
    home_course: Optional[str] = None
    rounds: Optional[list] = None
    handicap_index: Optional[float] = None
    tendencies_summary: Optional[str] = None
    onboarded: Optional[bool] = None


@app.post("/api/admin/import-my-profile")
def import_my_profile(payload: ProfileImportRequest, admin: dict = Depends(require_admin)):
    """Bulk update the admin's own profile (bag, tendencies, rounds, etc.).
    Used for one-time migration of existing data from local dev DB."""
    updates = {}
    if payload.bag is not None:
        updates["bag"] = json.dumps(payload.bag)
    if payload.driver_miss is not None:
        updates["driver_miss"] = payload.driver_miss
    if payload.iron_miss is not None:
        updates["iron_miss"] = payload.iron_miss
    if payload.home_course is not None:
        updates["home_course"] = payload.home_course
    if payload.rounds is not None:
        updates["rounds"] = json.dumps(payload.rounds)
    if payload.handicap_index is not None:
        updates["handicap_index"] = payload.handicap_index
    if payload.tendencies_summary is not None:
        updates["tendencies_summary"] = payload.tendencies_summary
    if payload.onboarded is not None:
        updates["onboarded"] = 1 if payload.onboarded else 0

    if not updates:
        return {"status": "no_changes"}

    set_clause = ", ".join(f"{k} = ?" for k in updates.keys())
    values = list(updates.values()) + [admin["id"]]
    with db() as conn:
        conn.execute(f"UPDATE users SET {set_clause} WHERE id = ?", values)
    return {"status": "updated", "fields": list(updates.keys())}


@app.post("/api/admin/reset_pin/{user_id}")
def reset_pin(user_id: int, admin: dict = Depends(require_admin)):
    new_pin = generate_pin()
    with db() as conn:
        row = conn.execute("SELECT username FROM users WHERE id = ?", (user_id,)).fetchone()
        if not row:
            raise HTTPException(404, "User not found")
        conn.execute("UPDATE users SET pin_hash = ? WHERE id = ?", (hash_pin(new_pin), user_id))
    return {"username": row["username"], "pin": new_pin}


@app.post("/api/admin/deactivate/{user_id}")
def deactivate_user(user_id: int, admin: dict = Depends(require_admin)):
    """Move an approved user to rejected. Kills their active sessions."""
    if user_id == admin["id"]:
        raise HTTPException(400, "You can't deactivate your own account")
    with db() as conn:
        row = conn.execute("SELECT username, is_admin FROM users WHERE id = ?", (user_id,)).fetchone()
        if not row:
            raise HTTPException(404, "User not found")
        if row["is_admin"]:
            raise HTTPException(400, "Can't deactivate another admin")
        conn.execute("UPDATE users SET status = 'rejected' WHERE id = ?", (user_id,))
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
    return {"status": "deactivated", "username": row["username"]}


@app.post("/api/admin/reactivate/{user_id}")
def reactivate_user(user_id: int, admin: dict = Depends(require_admin)):
    """Move a rejected user back to approved."""
    with db() as conn:
        row = conn.execute("SELECT username, status FROM users WHERE id = ?", (user_id,)).fetchone()
        if not row:
            raise HTTPException(404, "User not found")
        if row["status"] == "approved":
            raise HTTPException(400, "User is already active")
        conn.execute("UPDATE users SET status = 'approved' WHERE id = ?", (user_id,))
    return {"status": "reactivated", "username": row["username"]}


@app.delete("/api/admin/delete/{user_id}")
def delete_user(user_id: int, admin: dict = Depends(require_admin)):
    """Permanently delete a user and all their data."""
    if user_id == admin["id"]:
        raise HTTPException(400, "You can't delete your own account")
    with db() as conn:
        row = conn.execute("SELECT username, is_admin FROM users WHERE id = ?", (user_id,)).fetchone()
        if not row:
            raise HTTPException(404, "User not found")
        if row["is_admin"]:
            raise HTTPException(400, "Can't delete another admin")
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
    return {"status": "deleted", "username": row["username"]}


# ────────────────────────────────────────────────────────────
# Startup
# ────────────────────────────────────────────────────────────
@app.on_event("startup")
def startup():
    init_db()
    seed_initial_admin()
    print(f"Database ready at {DB_PATH}")


if __name__ == "__main__":
    import uvicorn
    init_db()
    uvicorn.run(app, host="0.0.0.0", port=8000)
