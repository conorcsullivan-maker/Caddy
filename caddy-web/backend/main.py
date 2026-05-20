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

from fastapi import FastAPI, Form, HTTPException, Request, Response, Cookie, Depends, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, Response as FastAPIResponse
from pydantic import BaseModel, Field
from dotenv import load_dotenv
# Load .env from the project root (../../.env) for local dev.
# In production, env vars are injected by the host (Render), so missing .env is fine.
load_dotenv(Path(__file__).parent.parent.parent / ".env", override=True)

from caddy_engine import caddy_reply, extract_scorecard_from_image, transcribe_audio, synthesize_speech, anthropic_client, build_system_prompt
from caddy_round import (
    detect_and_load_course, detect_and_update_tee, detect_and_log_score,
    apply_score_to_round_state, infer_drive_distance, is_end_of_round,
    calculate_handicap, format_course_context, format_score_context,
    find_synthetic_course, build_course_from_synthetic, save_synthetic_course,
    find_tee, search_course, get_course, get_hole_par, compute_round_status,
    detect_course_note, save_hole_note,
)
from caddy_weather import fetch_weather, format_weather_context, has_critical_alert

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
# SameSite=Lax works because the frontend proxies /api/* through its own
# origin (vercel.json + next.config.ts rewrites), making cookies first-party.
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "false").lower() == "true"
COOKIE_SAMESITE = "lax"


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
            tendencies_summary TEXT,
            conversation_history TEXT,
            active_round_state TEXT
        )
    """)
    # Conversations archive — every chat preserved, never discarded
    c.execute("""
        CREATE TABLE IF NOT EXISTS conversations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            kind TEXT NOT NULL DEFAULT 'casual',
            course_name TEXT,
            total_score INTEGER,
            messages TEXT NOT NULL,
            round_metadata TEXT,
            started_at TEXT NOT NULL,
            ended_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        )
    """)
    # Migration safety: ALTER TABLE for any column added after the original schema
    for col_def in (
        "ALTER TABLE users ADD COLUMN referral TEXT",
        "ALTER TABLE users ADD COLUMN conversation_history TEXT",
        "ALTER TABLE users ADD COLUMN active_round_state TEXT",
    ):
        try:
            c.execute(col_def)
        except sqlite3.OperationalError:
            pass  # column already exists
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
    lat: Optional[float] = None
    lng: Optional[float] = None


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


def load_round_state(user_id: int) -> dict:
    with db() as conn:
        row = conn.execute(
            "SELECT active_round_state FROM users WHERE id = ?", (user_id,)
        ).fetchone()
    if not row or not row["active_round_state"]:
        return {"hole_scores": [], "current_hole": 1}
    try:
        state = json.loads(row["active_round_state"])
        state.setdefault("hole_scores", [])
        state.setdefault("current_hole", 1)
        return state
    except Exception:
        return {"hole_scores": [], "current_hole": 1}


def save_round_state(user_id: int, state: dict):
    with db() as conn:
        conn.execute(
            "UPDATE users SET active_round_state = ? WHERE id = ?",
            (json.dumps(state), user_id),
        )


def clear_round_state(user_id: int):
    with db() as conn:
        conn.execute("UPDATE users SET active_round_state = NULL WHERE id = ?", (user_id,))


def archive_conversation(user_id: int, kind: str = "casual",
                         course_name: Optional[str] = None,
                         total_score: Optional[int] = None,
                         round_metadata: Optional[dict] = None):
    """Move the user's current conversation_history into the conversations table."""
    history = load_conversation(user_id)
    if not history:
        return None
    started = history[0].get("timestamp") or now_iso()
    with db() as conn:
        cur = conn.execute(
            """INSERT INTO conversations
               (user_id, kind, course_name, total_score, messages, round_metadata, started_at, ended_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                user_id, kind, course_name, total_score,
                json.dumps(history),
                json.dumps(round_metadata) if round_metadata else None,
                started, now_iso(),
            ),
        )
        conv_id = cur.lastrowid
        conn.execute("UPDATE users SET conversation_history = NULL WHERE id = ?", (user_id,))
    return conv_id


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


@app.delete("/api/me/rounds/{round_index}")
def delete_round(round_index: int, user: dict = Depends(get_current_user)):
    """Remove a round from the user's history by its array index, then
    recalculate handicap from the remaining rounds."""
    rounds = user.get("rounds") or []
    if round_index < 0 or round_index >= len(rounds):
        raise HTTPException(404, "Round not found")
    removed = rounds.pop(round_index)
    from caddy_round import calculate_handicap
    new_handicap = calculate_handicap(rounds)
    with db() as conn:
        conn.execute(
            "UPDATE users SET rounds = ?, handicap_index = ? WHERE id = ?",
            (json.dumps(rounds), new_handicap, user["id"]),
        )
    return {
        "status": "deleted",
        "removed": removed,
        "rounds_remaining": len(rounds),
        "handicap_index": new_handicap,
    }


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


@app.post("/api/me/trackman")
async def upload_trackman(
    url: Optional[str] = Form(None),
    csv_file: Optional[UploadFile] = File(None),
    user: dict = Depends(get_current_user),
):
    """Ingest a Trackman session — from a web report URL or a CSV upload —
    and update the player's tendencies summary. New session data is MERGED
    with prior tendencies, not overwritten, so the summary builds up over time."""
    from caddy_trackman import (
        fetch_trackman_report, summarize_trackman_session,
        parse_trackman_csv_text, generate_tendencies_summary,
    )

    url_clean = (url or "").strip()
    if not url_clean and not csv_file:
        raise HTTPException(400, "Provide either a Trackman report URL or a CSV file.")

    # Parse the input into a flat string we can hand to Claude.
    session_data_str: Optional[str] = None
    shot_count = 0

    if url_clean:
        session = fetch_trackman_report(url_clean)
        if not session:
            raise HTTPException(
                400,
                "Couldn't load that Trackman report. Double-check the URL or paste the report ID instead.",
            )
        session_data_str, shot_count = summarize_trackman_session(session)
        if not session_data_str:
            raise HTTPException(400, "The Trackman report loaded but contained no shot data.")

    elif csv_file:
        raw = await csv_file.read()
        try:
            csv_text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            csv_text = raw.decode("latin-1", errors="ignore")
        session_data_str, shot_count = parse_trackman_csv_text(csv_text)
        if not session_data_str:
            raise HTTPException(400, "Couldn't parse that CSV — make sure it's a Trackman export.")

    # Ask Claude to merge the new session with existing tendencies.
    first_name = (user.get("full_name") or "Player").split()[0]
    new_summary = generate_tendencies_summary(
        first_name=first_name,
        existing_summary=user.get("tendencies_summary"),
        session_data_str=session_data_str,
    )
    if not new_summary:
        raise HTTPException(
            502,
            "Trackman data parsed, but the tendencies summary couldn't be generated. "
            "Anthropic API might be unavailable — try again in a moment.",
        )

    with db() as conn:
        conn.execute(
            "UPDATE users SET tendencies_summary = ? WHERE id = ?",
            (new_summary, user["id"]),
        )
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user["id"],)).fetchone()

    return {
        "user": user_dict(row),
        "shot_count": shot_count,
        "tendencies_summary": new_summary,
    }


# ────────────────────────────────────────────────────────────
# Caddy chat endpoints
# ────────────────────────────────────────────────────────────
@app.get("/api/caddy/history")
def get_history(user: dict = Depends(get_current_user)):
    return {
        "history": load_conversation(user["id"]),
        "round_state": load_round_state(user["id"]),
    }


@app.get("/api/caddy/weather")
def get_weather(lat: float, lng: float, user: dict = Depends(get_current_user)):
    """Standalone weather lookup so the weather strip can populate on page load
    without waiting for the user to send a chat message."""
    return {"weather": fetch_weather(lat, lng)}


class ScoreEditRequest(BaseModel):
    hole: int = Field(ge=1, le=18)
    score: Optional[int] = Field(default=None, ge=1, le=20)


@app.post("/api/caddy/edit-score")
def edit_score(payload: ScoreEditRequest, user: dict = Depends(get_current_user)):
    """Manually override or clear a hole score in the active round. Returns
    the updated round state so the UI re-renders immediately."""
    state = load_round_state(user["id"])
    if not state.get("tee"):
        raise HTTPException(400, "No active round")
    hole_scores = state.get("hole_scores") or []
    while len(hole_scores) < payload.hole:
        hole_scores.append(None)
    hole_scores[payload.hole - 1] = payload.score
    state["hole_scores"] = hole_scores
    save_round_state(user["id"], state)
    return {"round_state": state}


@app.post("/api/caddy/reset")
def reset_history(user: dict = Depends(get_current_user)):
    """Archive the current conversation as 'casual' and start fresh."""
    state = load_round_state(user["id"])
    course_name = (state.get("course") or {}).get("club_name")
    archived_id = archive_conversation(user["id"], kind="casual", course_name=course_name)
    clear_round_state(user["id"])
    return {"status": "reset", "archived_conversation_id": archived_id}


def process_user_message(user: dict, message: str,
                         lat: Optional[float] = None,
                         lng: Optional[float] = None) -> dict:
    """The full message processing pipeline:
    - Detect course mention → load it
    - Detect score report → log it
    - Detect drive distance → infer it
    - Detect end-of-round → trigger save
    - Fetch live weather (if location provided)
    - Build dynamic system context (player + course + score + weather)
    - Get Claude reply
    - Save state
    Returns dict with reply, round_state, weather, alerts, and any events that fired.
    """
    history = load_conversation(user["id"])
    round_state = load_round_state(user["id"])
    events = []
    weather = None
    if lat is not None and lng is not None:
        weather = fetch_weather(lat, lng)
        if weather and has_critical_alert(weather):
            events.append({
                "type": "weather_alert",
                "alerts": [a.get("event") for a in weather.get("alerts") or []],
            })

    # 1. End-of-round detection (highest priority — short-circuits other processing)
    if is_end_of_round(message) and round_state.get("hole_scores"):
        return handle_round_complete(user, history, message, round_state)

    # 1b. Course rejection — if the player just had a course loaded and pushes back on it,
    # unload it so they can rename / send a scorecard / just play. Tight phrase list
    # to avoid false positives like "no, going for it" or "wrong club".
    course_rejected = False
    if round_state.get("course_confirmed") is False:
        _msg_lower = message.lower()
        _rejection_phrases = [
            "wrong course", "not the right course", "not that course",
            "different course", "that's not it", "that's not the one",
            "not this course",
        ]
        _short_rejections = {"no", "nope", "nah", "wrong", "incorrect"}
        _is_short_reject = (
            len(message.split()) <= 3
            and any(w in _msg_lower.split() for w in _short_rejections)
        )
        if any(p in _msg_lower for p in _rejection_phrases) or _is_short_reject:
            round_state.pop("course", None)
            round_state.pop("tee", None)
            round_state.pop("course_confirmed", None)
            events.append({"type": "course_unloaded"})
            course_rejected = True

    # 2. Course detection (only if no course loaded). Returns either a loaded course,
    # a "not_found" signal so Caddy can offer alternatives, or None.
    course_load = detect_and_load_course(message, round_state, player_lat=lat, player_lng=lng)
    course_loaded_now = False
    course_not_found_query: Optional[str] = None
    course_load_distance: Optional[float] = None
    if course_load and course_load.get("status") in ("loaded", "switched"):
        is_switch = course_load.get("status") == "switched"
        round_state["course"] = course_load["course"]
        round_state["tee"] = course_load["tee"]
        round_state["course_confirmed"] = False
        if is_switch:
            # Player explicitly named a different course — wipe the old
            # scorecard since the old course's pars no longer apply.
            round_state["hole_scores"] = []
            round_state["current_hole"] = 1
            round_state["started_at"] = now_iso()
        else:
            round_state["started_at"] = round_state.get("started_at") or now_iso()
        course_loaded_now = True
        course_load_distance = course_load.get("distance_miles")
        events.append({
            "type": "course_loaded",
            "course_name": course_load["course"].get("club_name"),
            "tee_name": course_load["tee"].get("tee_name"),
        })
    elif course_load and course_load.get("status") == "not_found":
        course_not_found_query = course_load.get("query")
        events.append({"type": "course_not_found", "query": course_not_found_query})

    # 2b. Tee change detection (course already loaded, player mentions a different tee color)
    new_tee = detect_and_update_tee(message, round_state)
    if new_tee:
        round_state["tee"] = new_tee
        events.append({"type": "tee_changed", "tee_name": new_tee.get("tee_name")})

    # 3. Score detection
    score_result = detect_and_log_score(message, round_state)
    score_just_logged: Optional[dict] = None
    if score_result:
        apply_score_to_round_state(round_state, score_result["hole"], score_result["score"])
        events.append({"type": "score_logged", **score_result})
        score_just_logged = score_result

    # 4. Drive distance inference
    drive_result = infer_drive_distance(message, round_state)
    if drive_result:
        events.append({"type": "drive_inferred", **drive_result})

    # 4b. Passive course note extraction (silent — player never sees this)
    note_result = detect_course_note(message, round_state)
    if note_result:
        try:
            save_hole_note(round_state["course"], note_result["hole"], note_result["note"])
        except Exception as e:
            print(f"[notes] save failed: {e}")

    # 5. Build dynamic context for Claude
    course_ctx = format_course_context(round_state)
    score_ctx = format_score_context(round_state)
    weather_ctx = format_weather_context(weather) if weather else ""
    round_context = course_ctx + score_ctx + weather_ctx

    # Course context handling — never block on confirmation. Three cases:
    if course_loaded_now:
        # Casually acknowledge the course in passing while answering whatever else was asked.
        _course = round_state["course"]
        _raw_loc = _course.get("location")
        if isinstance(_raw_loc, dict):
            # API shape: {address, city, state, country}
            _parts = [_raw_loc.get("city"), _raw_loc.get("state")]
            _loc = ", ".join(p for p in _parts if p)
        else:
            _loc = (_raw_loc or "").strip()
        _loc_str = f" in {_loc}" if _loc else ""

        # Trust level based on GPS distance: close = confident, far/unknown = sanity-check
        if course_load_distance is not None and course_load_distance < 3:
            _trust_note = (
                f"GPS confirms you're within {course_load_distance:.1f} miles — high confidence this is the right course. "
                f"Acknowledge casually in one short phrase (e.g. 'Got {_course.get('club_name')}{_loc_str} loaded') and keep moving."
            )
        elif course_load_distance is not None and course_load_distance > 50:
            _trust_note = (
                f"GPS shows the player is {course_load_distance:.0f} miles from this course — that's suspicious. "
                f"Mention the city/state ({_loc or 'the location'}) and ask the player to confirm this is the right course — "
                f"there may be another course with the same name closer to them."
            )
        else:
            _trust_note = (
                f"No GPS confirmation available. Mention the course AND the city/state in one short sentence "
                f"(e.g. 'Got {_course.get('club_name')}{_loc_str} loaded — that the right one?') so they can correct if needed. "
                f"Keep moving regardless — don't wait to be told yes."
            )
        round_context += f"\n\nNOTE: Course just auto-loaded: {_course.get('club_name')}{_loc_str}. {_trust_note}"
    elif course_not_found_query:
        # Course was mentioned but lookup failed — offer the two escape hatches casually.
        round_context += (
            f"\n\nNOTE: Player mentioned a course (\"{course_not_found_query}\") but I couldn't find it in the database. "
            f"Briefly let them know in one sentence, then offer two options casually: snap a photo of the scorecard "
            f"(camera button in chat), or just play and tell you yardages as you go. Don't make a big deal of it — "
            f"the course is a nice-to-have, not a blocker."
        )
    elif course_rejected:
        # Player pushed back on the auto-loaded course — clear it and let them choose what's next.
        round_context += (
            f"\n\nNOTE: Player just rejected the course I auto-loaded. It's now cleared. "
            f"Acknowledge briefly and offer them options: tell you the right course name, snap a scorecard photo, "
            f"or just play and call out yardages as you go. Keep it short — don't push."
        )
    elif round_state.get("course_confirmed") is False:
        # Player responded to a course load without rejecting — treat as implicit confirmation.
        round_state["course_confirmed"] = True

    # Score logging hint — pre-compute the status so Caddy can never invent one.
    # Two principles: (1) default response is just a reaction to the hole, no
    # running total. (2) IF Caddy mentions any progress vs par, it MUST quote
    # the exact computed status verbatim — never recompute or paraphrase. This
    # eliminates the "back to even" hallucination after birdie+par+double.
    if score_just_logged:
        _diff = (score_just_logged.get("score") or 0) - (score_just_logged.get("par") or 0)
        _result_label = {
            -3: "albatross", -2: "eagle", -1: "birdie", 0: "par",
            1: "bogey", 2: "double bogey", 3: "triple bogey", 4: "quad",
        }.get(_diff, f"{score_just_logged.get('score')}")
        _status = compute_round_status(round_state) or ""
        # Detect any holes that got skipped — Caddy should ask about them.
        _scores = round_state.get("hole_scores") or []
        _max_logged = max((i + 1 for i, s in enumerate(_scores) if s is not None), default=0)
        _missing = [i + 1 for i, s in enumerate(_scores[:_max_logged]) if s is None]

        # Build the response rules. If there are gaps in the scorecard, force
        # a second sentence asking about the missing hole(s) — without this,
        # Caddy reliably skips the follow-up and the gap persists silently.
        rules = [
            "Open with ONE short sentence reacting to THIS hole only. "
            "Examples: 'Nice par.' / 'Great birdie.' / 'Eagle — clutch.' / "
            "'Tough one, onto the next.' / 'Shake it off.'",
            "Do NOT compute or invent the running score. The 'Round status' "
            "above is the only correct value.",
            f"If you mention overall round progress, quote it exactly as: "
            f"\"{_status}\". Never paraphrase, recompute, or approximate.",
            "Prefer rule 1 alone. Only mention running total if the player asks.",
        ]
        if _missing:
            _missing_str = ", ".join(str(h) for h in _missing)
            rules.append(
                f"REQUIRED: hole(s) {_missing_str} were never logged. After your reaction sentence, "
                f"ADD a second sentence asking what they made on the missing hole. Ask them to include "
                f"the hole number in the answer so it lands on the right row. "
                f"Example two-sentence reply: 'Tough one, shake it off. By the way, what did you make "
                f"on hole {_missing[0]}? Say it like \"birdie on {_missing[0]}\" so I log it right.' "
                f"This second sentence is mandatory — don't skip it."
            )

        rules_str = "\n".join(f"{i}. {r}" for i, r in enumerate(rules, 1))
        round_context += (
            f"\n\nSCORE JUST LOGGED:\n"
            f"  Hole:           {score_just_logged.get('hole')}\n"
            f"  Strokes:        {score_just_logged.get('score')}\n"
            f"  Result:         {_result_label}\n"
            f"  Round status:   {_status}\n"
            f"\nRESPONSE RULES (follow strictly):\n{rules_str}"
        )

    # 6. Get Claude's reply
    reply = caddy_reply(user, history, message, round_context=round_context)

    # 7. Save state
    history.append({"role": "user", "content": message})
    history.append({"role": "assistant", "content": reply})
    save_conversation(user["id"], history)
    save_round_state(user["id"], round_state)

    return {
        "reply": reply,
        "user_message": message,
        "round_state": round_state,
        "weather": weather,
        "events": events,
    }


def handle_round_complete(user: dict, history: list, message: str, round_state: dict) -> dict:
    """End-of-round flow: generate tendencies summary, save round to profile,
    update handicap, archive conversation, clear active round state."""
    hole_scores = [s for s in (round_state.get("hole_scores") or []) if s is not None]
    total_score = sum(hole_scores) if hole_scores else None
    course = round_state.get("course") or {}
    tee = round_state.get("tee") or {}
    course_name = course.get("club_name", "Unknown course")
    course_rating = tee.get("course_rating")
    slope_rating = tee.get("slope_rating")

    differential = None
    if total_score and course_rating and slope_rating:
        differential = round((total_score - course_rating) * 113 / slope_rating, 1)

    # 1. Build a tendencies summary via Claude using the round transcript
    if hole_scores and anthropic_client:
        summary_prompt = (
            "The round is complete. Based on everything we discussed and logged today — "
            "shot results, misses, what was working — write a concise updated tendencies "
            "summary for my player profile. Write in second person, factual, under 150 words. "
            "This will inform your recommendations in future rounds."
        )
        try:
            summary_messages = history + [{"role": "user", "content": summary_prompt}]
            r = anthropic_client.messages.create(
                model="claude-opus-4-7",
                max_tokens=300,
                system=build_system_prompt(user) + format_course_context(round_state) + format_score_context(round_state),
                messages=summary_messages,
            )
            new_tendencies = r.content[0].text
        except Exception:
            new_tendencies = user.get("tendencies_summary")
    else:
        new_tendencies = user.get("tendencies_summary")

    # 2. Save round to user's rounds list
    rounds = user.get("rounds") or []
    if total_score:
        round_record = {
            "date": now_iso()[:10],
            "course": course_name,
            "score": total_score,
            "holes": len(hole_scores),
            "hole_scores": round_state.get("hole_scores"),
            "course_rating": course_rating,
            "slope_rating": slope_rating,
            "differential": differential,
        }
        rounds.append(round_record)

    # 3. Recompute handicap
    handicap = calculate_handicap(rounds) if total_score else user.get("handicap_index")

    # 4. Generate Caddy's spoken sign-off
    if total_score and differential is not None:
        sign_off = f"Good round. Final: {total_score} at {course_name}. Differential {differential}. " + \
                   (f"Handicap index updated to {handicap}." if handicap is not None else "Logged.")
    elif total_score:
        sign_off = f"Round logged: {total_score} at {course_name}. Saved."
    else:
        sign_off = "Round complete. No scores logged this time, so nothing to save."

    # 5. Persist user profile updates
    with db() as conn:
        conn.execute(
            """UPDATE users SET rounds = ?, handicap_index = ?, tendencies_summary = ?
               WHERE id = ?""",
            (json.dumps(rounds), handicap, new_tendencies, user["id"]),
        )

    # 6. Add the user's "round complete" message and Caddy's sign-off to history
    history.append({"role": "user", "content": message})
    history.append({"role": "assistant", "content": sign_off})
    save_conversation(user["id"], history)

    # 7. Archive the now-complete conversation as a 'round'
    archive_conversation(
        user["id"],
        kind="round",
        course_name=course_name,
        total_score=total_score,
        round_metadata={
            "hole_scores": round_state.get("hole_scores"),
            "course_rating": course_rating,
            "slope_rating": slope_rating,
            "differential": differential,
            "handicap_after": handicap,
        },
    )

    # 8. Clear active round state
    clear_round_state(user["id"])

    return {
        "reply": sign_off,
        "user_message": message,
        "round_state": {"hole_scores": [], "current_hole": 1},
        "events": [{
            "type": "round_complete",
            "course_name": course_name,
            "total_score": total_score,
            "differential": differential,
            "handicap": handicap,
        }],
    }


@app.post("/api/caddy/message")
def caddy_message(payload: CaddyMessageRequest, user: dict = Depends(get_current_user)):
    """Text message → full processing pipeline → response + events."""
    return process_user_message(user, payload.message, lat=payload.lat, lng=payload.lng)


@app.post("/api/caddy/voice")
async def caddy_voice(
    audio: UploadFile = File(...),
    lat: Optional[float] = None,
    lng: Optional[float] = None,
    user: dict = Depends(get_current_user),
):
    """Audio in → Whisper transcript → full processing pipeline → transcript + response + events.
    When Whisper returns a hallucination or silent audio, we respond as if Caddy
    itself politely asked for a repeat — that's friendlier than a red error banner
    and matches how a real caddy would handle a missed line."""
    audio_bytes = await audio.read()
    if not audio_bytes:
        raise HTTPException(400, "Empty audio")
    transcript = transcribe_audio(audio_bytes, audio.filename or "audio.webm")
    if not transcript:
        return {
            "transcript": "",
            "reply": "Didn't catch that — say it again?",
            "round_state": load_round_state(user["id"]),
            "events": [{"type": "transcript_unclear"}],
            "weather": None,
        }
    result = process_user_message(user, transcript, lat=lat, lng=lng)
    return {"transcript": transcript, **result}


@app.post("/api/caddy/photo")
async def caddy_photo(
    image: UploadFile = File(...),
    message: Optional[str] = Form(None),
    lat: Optional[float] = None,
    lng: Optional[float] = None,
    user: dict = Depends(get_current_user),
):
    """Scorecard photo → Claude vision extraction → course load + caddy reply."""
    content_type = image.content_type or "image/jpeg"
    print(f"[photo] user={user['username']} filename={image.filename!r} content_type={content_type!r}")
    if not content_type.startswith("image/"):
        raise HTTPException(400, f"File must be an image, got {content_type!r}")
    image_bytes = await image.read()
    print(f"[photo] image size: {len(image_bytes)} bytes ({len(image_bytes)/1024:.1f} KB)")
    if len(image_bytes) > 10 * 1024 * 1024:
        raise HTTPException(400, "Image too large (max 10MB)")

    extracted = extract_scorecard_from_image(image_bytes, content_type)

    # Not a recognizable scorecard — return a direct error, don't pollute conversation history
    if not extracted:
        round_state = load_round_state(user["id"])
        return {
            "reply": "Couldn't read a scorecard from that photo. Try laying the card flat with even lighting and shoot straight down, or just tell me the course name and I'll look it up.",
            "user_message": message or "📷 Photo",
            "round_state": round_state,
            "weather": None,
            "events": [],
        }

    course_name = extracted["course_name"]
    city = extracted.get("city") or ""
    state = extracted.get("state") or ""
    loc_str = f" in {city}, {state}".rstrip(", ") if city else ""

    history = load_conversation(user["id"])
    round_state = load_round_state(user["id"])
    events = []
    weather = None
    if lat is not None and lng is not None:
        weather = fetch_weather(lat, lng)

    # Only load a course if one isn't already active
    if not round_state.get("course"):
        courses = search_course(course_name)
        if courses:
            course_data = get_course(courses[0]["id"])
            tee = find_tee(course_data) if course_data else None
        else:
            syn = find_synthetic_course(course_name) or save_synthetic_course(extracted)
            course_data = build_course_from_synthetic(syn)
            tee = find_tee(course_data)

        if course_data and tee:
            round_state["course"] = course_data
            round_state["tee"] = tee
            round_state["started_at"] = round_state.get("started_at") or now_iso()
            events.append({
                "type": "course_loaded",
                "course_name": course_data.get("club_name"),
                "tee_name": tee.get("tee_name"),
            })

    tee_name = (round_state.get("tee") or {}).get("tee_name", "WHITE")
    club_name = (round_state.get("course") or {}).get("club_name", course_name)

    # Internal prompt to Caddy — not shown in history verbatim, but the user bubble shows a clean label
    caddy_prompt = (
        f"The player just uploaded their scorecard. Course loaded: {club_name}{loc_str}, "
        f"{tee_name} tees. Confirm you have the course and you're ready to caddy. "
        "Keep it to one or two sentences — you're on the first tee."
    )
    if message:
        caddy_prompt += f' Player also said: "{message}"'

    course_ctx = format_course_context(round_state)
    score_ctx = format_score_context(round_state)
    weather_ctx = format_weather_context(weather) if weather else ""
    reply = caddy_reply(user, history, caddy_prompt, round_context=course_ctx + score_ctx + weather_ctx)

    user_message = message or f"[Scorecard: {club_name}]"
    history.append({"role": "user", "content": user_message})
    history.append({"role": "assistant", "content": reply})
    save_conversation(user["id"], history)
    save_round_state(user["id"], round_state)

    return {
        "reply": reply,
        "user_message": user_message,
        "round_state": round_state,
        "weather": weather,
        "events": events,
    }


@app.get("/api/caddy/conversations")
def list_conversations(user: dict = Depends(get_current_user), limit: int = 50):
    """List the current user's archived conversations, most recent first."""
    with db() as conn:
        rows = conn.execute(
            """SELECT id, kind, course_name, total_score, started_at, ended_at, round_metadata
               FROM conversations WHERE user_id = ? ORDER BY ended_at DESC LIMIT ?""",
            (user["id"], limit),
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        if d.get("round_metadata"):
            try:
                d["round_metadata"] = json.loads(d["round_metadata"])
            except Exception:
                d["round_metadata"] = None
        out.append(d)
    return {"conversations": out}


@app.get("/api/caddy/conversations/{conv_id}")
def get_conversation(conv_id: int, user: dict = Depends(get_current_user)):
    """Get a single archived conversation with all messages."""
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM conversations WHERE id = ? AND user_id = ?",
            (conv_id, user["id"]),
        ).fetchone()
    if not row:
        raise HTTPException(404, "Conversation not found")
    d = dict(row)
    try:
        d["messages"] = json.loads(d["messages"])
    except Exception:
        d["messages"] = []
    if d.get("round_metadata"):
        try:
            d["round_metadata"] = json.loads(d["round_metadata"])
        except Exception:
            d["round_metadata"] = None
    return d


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


class CreateUserDirectRequest(BaseModel):
    username: str
    pin_hash: str  # already hashed (sha256 hex)
    full_name: str
    is_admin: bool = False
    onboarded: bool = False
    bag: Optional[dict] = None
    driver_miss: Optional[str] = None
    iron_miss: Optional[str] = None
    home_course: Optional[str] = None
    rounds: Optional[list] = None
    handicap_index: Optional[float] = None
    tendencies_summary: Optional[str] = None


@app.post("/api/admin/create-user-directly")
def create_user_directly(payload: CreateUserDirectRequest, admin: dict = Depends(require_admin)):
    """Admin: create a user with a pre-hashed PIN. Bypasses signup/approval.
    Used for one-time migration of existing accounts from local dev DB."""
    username = payload.username.lower().strip()
    with db() as conn:
        existing = conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
        if existing:
            raise HTTPException(400, f"User '{username}' already exists")
        bag_json = json.dumps(payload.bag) if payload.bag else None
        rounds_json = json.dumps(payload.rounds) if payload.rounds else None
        conn.execute("""
            INSERT INTO users
              (username, pin_hash, full_name, status, is_admin, onboarded,
               created_at, approved_at, bag, driver_miss, iron_miss, home_course,
               rounds, handicap_index, tendencies_summary)
            VALUES (?, ?, ?, 'approved', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            username, payload.pin_hash, payload.full_name,
            1 if payload.is_admin else 0,
            1 if payload.onboarded else 0,
            now_iso(), now_iso(),
            bag_json, payload.driver_miss, payload.iron_miss, payload.home_course,
            rounds_json, payload.handicap_index, payload.tendencies_summary,
        ))
    return {"status": "created", "username": username}


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
