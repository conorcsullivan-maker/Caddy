"""FastAPI auth dependencies shared by all routers."""
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import Cookie, Depends, Header, HTTPException

from db import db
from security import SESSION_MAX_AGE_DAYS
from store import user_dict


def get_current_user(
    session: Optional[str] = Cookie(None),
    authorization: Optional[str] = Header(None),
) -> dict:
    # Two ways to present the same session token: the browser sends it as a
    # first-party cookie (via the Vercel proxy); the native app stores it and
    # sends "Authorization: Bearer <token>" straight to this API. Cookie wins
    # when both are present.
    if not session and authorization and authorization.lower().startswith("bearer "):
        session = authorization[7:].strip()
    if not session:
        raise HTTPException(401, "Not signed in")
    with db() as conn:
        row = conn.execute("""
            SELECT u.*, s.created_at AS _session_created FROM users u
            JOIN sessions s ON s.user_id = u.id
            WHERE s.token = ?
        """, (session,)).fetchone()
    if not row:
        raise HTTPException(401, "Invalid session")

    # Enforce the session lifetime server-side, matching the cookie max-age.
    # Cookies expire client-side, but the token row used to live forever.
    created = row["_session_created"] or ""
    try:
        created_dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
        expired = datetime.now(timezone.utc) - created_dt > timedelta(days=SESSION_MAX_AGE_DAYS)
    except ValueError:
        expired = True  # unparseable timestamp — treat as stale
    if expired:
        with db() as conn2:
            conn2.execute("DELETE FROM sessions WHERE token = ?", (session,))
        raise HTTPException(401, "Session expired — please sign in again")

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
