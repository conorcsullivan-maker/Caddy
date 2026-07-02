"""Admin endpoints: beta approval flow, user management, engagement views."""
import json
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response as FastAPIResponse
from pydantic import BaseModel

from db import db, now_iso
from deps import require_admin
from security import generate_pin, hash_pin_secure

router = APIRouter()

# Content-sniffing signals for Trackman uploads that predate the
# trackman_session_ids column — round-completion summaries never produce
# these phrases, so their presence in a tendencies_summary means at least
# one legacy session was uploaded.
_LEGACY_TRACKMAN_SIGNALS = (
    "trackman", "smash factor", "smash 1.", "spin rate",
    "face-to-path", "face to path", "launch angle", "club path",
    "carry: avg", "consistency ±",
)


@router.get("/api/admin/pending")
def list_pending(user: dict = Depends(require_admin)):
    with db() as conn:
        rows = conn.execute("""
            SELECT id, username, full_name, email, phone, reason, referral, created_at
            FROM users WHERE status = 'pending' ORDER BY created_at ASC
        """).fetchall()
    return {"pending": [dict(r) for r in rows]}


@router.get("/api/admin/users")
def list_all_users(user: dict = Depends(require_admin)):
    """Returns each user plus engagement metrics: how many rounds they've
    played, whether Trackman data has been uploaded, when they were last
    active. Numbers only — no personal stats like scores or tendencies."""
    with db() as conn:
        rows = conn.execute("""
            SELECT id, username, full_name, email, phone, status, is_admin, onboarded,
                   created_at, approved_at, handicap_index,
                   bag, rounds, tendencies_summary, trackman_session_ids
            FROM users ORDER BY created_at DESC
        """).fetchall()
        # Pull last-activity date per user from the conversations archive
        # (the most recent conversation any user has ended).
        activity_rows = conn.execute("""
            SELECT user_id, MAX(ended_at) AS last_activity
            FROM conversations GROUP BY user_id
        """).fetchall()
    last_activity_by_user = {r["user_id"]: r["last_activity"] for r in activity_rows}

    out = []
    for r in rows:
        d = dict(r)
        # Compute engagement metrics — keep this strictly numerical/boolean,
        # never expose individual scores or the player's tendencies prose.
        try:
            bag = json.loads(d.get("bag") or "{}")
            clubs_with_distance = sum(1 for v in bag.values() if v)
        except Exception:
            clubs_with_distance = 0
        try:
            rounds = json.loads(d.get("rounds") or "[]")
            rounds_count = len(rounds)
        except Exception:
            rounds_count = 0
        try:
            tm_ids = json.loads(d.get("trackman_session_ids") or "[]")
            trackman_sessions = len(tm_ids)
        except Exception:
            trackman_sessions = 0

        # Backfill detection for pre-dedupe-column uploads. When the
        # heuristic fires, count as ≥1 legacy session and flag so the admin
        # UI can show "uploaded" instead of a misleading exact count.
        trackman_backfilled = False
        if trackman_sessions == 0:
            summary = (d.get("tendencies_summary") or "").lower()
            if summary and any(s in summary for s in _LEGACY_TRACKMAN_SIGNALS):
                trackman_sessions = 1
                trackman_backfilled = True

        engagement = {
            "clubs_with_distance": clubs_with_distance,
            "rounds_count": rounds_count,
            "trackman_sessions": trackman_sessions,
            "trackman_backfilled": trackman_backfilled,
            "has_tendencies": bool(d.get("tendencies_summary")),
            "last_activity": last_activity_by_user.get(d["id"]),
        }
        # Strip the raw fields we used to compute engagement so they don't
        # leak personal content (rounds list, tendencies prose) to the admin view.
        d.pop("bag", None)
        d.pop("rounds", None)
        d.pop("tendencies_summary", None)
        d.pop("trackman_session_ids", None)
        d["engagement"] = engagement
        out.append(d)
    return {"users": out}


@router.get("/api/admin/export.csv")
def export_beta_data(admin: dict = Depends(require_admin)):
    """One-row-per-user CSV dump of beta engagement data — signup dates,
    activation funnel status, round/Trackman counts, last activity. Lets
    Conor or Drew pull a snapshot for pitch decks or cohort analysis
    without re-engineering or running ad-hoc SQL.

    Everything in here is already shown live in /admin; this endpoint just
    serializes it so it can be downloaded as a spreadsheet. Personal
    content (scores, tendencies prose) is still excluded — only the
    engagement metrics are exported."""
    import csv as _csv
    import io as _io

    with db() as conn:
        rows = conn.execute("""
            SELECT id, username, full_name, email, phone, status, is_admin, onboarded,
                   created_at, approved_at, handicap_index,
                   bag, rounds, tendencies_summary, trackman_session_ids
            FROM users ORDER BY created_at ASC
        """).fetchall()
        activity_rows = conn.execute("""
            SELECT user_id, MAX(ended_at) AS last_activity, COUNT(*) AS conversation_count
            FROM conversations GROUP BY user_id
        """).fetchall()
    activity_by_user = {
        r["user_id"]: {"last": r["last_activity"], "count": r["conversation_count"]}
        for r in activity_rows
    }

    buf = _io.StringIO()
    writer = _csv.writer(buf)
    writer.writerow([
        "user_id", "username", "full_name", "email", "status", "is_admin",
        "onboarded", "created_at", "approved_at", "handicap_index",
        "clubs_in_bag", "rounds_played", "trackman_sessions",
        "trackman_uploaded", "has_tendencies",
        "total_conversations", "last_activity",
    ])
    for r in rows:
        d = dict(r)
        try:
            bag = json.loads(d.get("bag") or "{}")
            clubs_in_bag = sum(1 for v in bag.values() if v)
        except Exception:
            clubs_in_bag = 0
        try:
            rounds = json.loads(d.get("rounds") or "[]")
            rounds_count = len(rounds)
        except Exception:
            rounds_count = 0
        try:
            tm_ids = json.loads(d.get("trackman_session_ids") or "[]")
            tm_count = len(tm_ids)
        except Exception:
            tm_count = 0
        # Same backfill heuristic as the engagement endpoint — pre-dedupe
        # uploads detected via tendencies_summary content.
        summary = (d.get("tendencies_summary") or "").lower()
        legacy_uploaded = bool(summary and any(s in summary for s in _LEGACY_TRACKMAN_SIGNALS))
        trackman_uploaded = tm_count > 0 or legacy_uploaded

        activity = activity_by_user.get(d["id"], {"last": "", "count": 0})
        writer.writerow([
            d["id"], d["username"], d["full_name"], d.get("email", ""),
            d["status"], 1 if d.get("is_admin") else 0,
            1 if d.get("onboarded") else 0,
            d.get("created_at", ""), d.get("approved_at", ""),
            d.get("handicap_index") or "",
            clubs_in_bag, rounds_count, tm_count,
            1 if trackman_uploaded else 0,
            1 if d.get("tendencies_summary") else 0,
            activity["count"], activity["last"] or "",
        ])

    filename = f"caddy_beta_{now_iso()[:10]}.csv"
    return FastAPIResponse(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/api/admin/approve/{user_id}")
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
        """, (now_iso(), hash_pin_secure(new_pin), user_id))
    return {"username": row["username"], "pin": new_pin}


@router.post("/api/admin/reject/{user_id}")
def reject_user(user_id: int, admin: dict = Depends(require_admin)):
    with db() as conn:
        row = conn.execute("SELECT id FROM users WHERE id = ?", (user_id,)).fetchone()
        if not row:
            raise HTTPException(404, "User not found")
        conn.execute("UPDATE users SET status = 'rejected' WHERE id = ?", (user_id,))
    return {"status": "rejected"}


class CreateUserDirectRequest(BaseModel):
    username: str
    pin_hash: str  # already hashed — legacy sha256 hex OR pbkdf2$salt$hash
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


@router.post("/api/admin/create-user-directly")
def create_user_directly(payload: CreateUserDirectRequest, admin: dict = Depends(require_admin)):
    """Admin: create a user with a pre-hashed PIN. Bypasses signup/approval.
    Used for one-time migration of existing accounts from local dev DB.
    Legacy sha256 hashes are accepted and upgrade themselves on the user's
    first login."""
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


@router.post("/api/admin/import-my-profile")
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


@router.post("/api/admin/reset_pin/{user_id}")
def reset_pin(user_id: int, admin: dict = Depends(require_admin)):
    new_pin = generate_pin()
    with db() as conn:
        row = conn.execute("SELECT username FROM users WHERE id = ?", (user_id,)).fetchone()
        if not row:
            raise HTTPException(404, "User not found")
        conn.execute("UPDATE users SET pin_hash = ? WHERE id = ?", (hash_pin_secure(new_pin), user_id))
    return {"username": row["username"], "pin": new_pin}


@router.post("/api/admin/deactivate/{user_id}")
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


@router.post("/api/admin/reactivate/{user_id}")
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


@router.delete("/api/admin/delete/{user_id}")
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
