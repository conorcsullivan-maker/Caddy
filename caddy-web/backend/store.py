"""SQLite-backed application state: user serialization, conversations,
round state, unified shot statistics, and the course-geometry cache.

Everything here is a thin data-access layer — no HTTP, no LLM calls.
"""
import json
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Optional

from db import db, now_iso
from caddy_geo import (
    compute_relative_wind,
    fetch_course_geometry,
    gps_yards_to_green,
)

# Conversation export allowlist. Only these usernames can download their own
# chats as .docx during beta. Keep this hardcoded (not gated on is_admin) so
# future admins don't silently inherit export rights — every grant is explicit.
EXPORT_ALLOWED_USERNAMES = {"sullydakid", "smiley"}


def user_dict(row: sqlite3.Row) -> dict:
    """Convert DB row to safe dict (no pin_hash, no session join columns)."""
    d = dict(row)
    d.pop("pin_hash", None)
    d.pop("_session_created", None)
    for json_field in ("bag", "rounds", "on_course_shots", "shot_stats", "trackman_session_ids"):
        if d.get(json_field):
            try:
                d[json_field] = json.loads(d[json_field])
            except Exception:
                pass
    d["is_admin"] = bool(d.get("is_admin"))
    d["onboarded"] = bool(d.get("onboarded"))
    d["can_export_conversations"] = (d.get("username") or "") in EXPORT_ALLOWED_USERNAMES
    return d


# ────────────────────────────────────────────────────────────
# Conversation + round state persistence
# ────────────────────────────────────────────────────────────
def load_conversation(user_id: int) -> list:
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


def save_conversation(user_id: int, history: list):
    # Persist the full conversation — never truncate on save. The per-turn
    # Claude context window is limited separately inside caddy_reply
    # (CLAUDE_CONTEXT_MESSAGES). Keeping the full record on disk is what
    # makes round-end tendencies summaries, archives, and downloads complete.
    with db() as conn:
        conn.execute(
            "UPDATE users SET conversation_history = ? WHERE id = ?",
            (json.dumps(history), user_id),
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
# Unified shot statistics
#
# Every shot — whether from a Trackman session upload or inferred during an
# on-course round — lands in one place: user.shot_stats. Per club, per source.
# The cumulative count across BOTH sources determines the confidence tier
# (see caddy_trackman.SHOT_TIER_* and shot_count_tier), and the tier is
# computed in code, not in Claude's prompt. Claude just reads a pre-computed
# label like "MEDIUM CONFIDENCE" and uses it.
#
# Bucket shape:
#   {count, total_carry, sum_sq, best, worst, left, right, center}
# Left/right/center only meaningful for on-course shots. Trackman doesn't tag
# direction so those stay zero in the trackman bucket.
#
# All read-modify-write sequences run inside a single BEGIN IMMEDIATE
# transaction so a concurrent request (or background thread) can't slot a
# write between our read and our write and get clobbered.
# ────────────────────────────────────────────────────────────

EMPTY_BUCKET = {
    "count": 0, "total_carry": 0, "sum_sq": 0,
    "best": 0, "worst": 0,
    "left": 0, "right": 0, "center": 0,
}


def _new_bucket() -> dict:
    return dict(EMPTY_BUCKET)


def _merge_one_shot_into_bucket(bucket: dict, distance: int, direction: Optional[str] = None) -> dict:
    """Add a single shot's distance into a running bucket. Returns the updated bucket."""
    n_before = bucket.get("count", 0)
    bucket["count"] = n_before + 1
    bucket["total_carry"] = bucket.get("total_carry", 0) + distance
    bucket["sum_sq"] = bucket.get("sum_sq", 0) + distance * distance
    bucket["best"] = max(bucket.get("best", 0), distance)
    if n_before == 0:
        bucket["worst"] = distance
    else:
        bucket["worst"] = min(bucket.get("worst", distance), distance)
    if direction in ("left", "right", "center"):
        bucket[direction] = bucket.get(direction, 0) + 1
    return bucket


def _merge_bucket_into_bucket(target: dict, addend: dict) -> dict:
    """Merge two stat buckets (used when bulk-loading a Trackman session)."""
    n_target = target.get("count", 0)
    n_addend = addend.get("count", 0)
    target["count"] = n_target + n_addend
    target["total_carry"] = target.get("total_carry", 0) + addend.get("total_carry", 0)
    target["sum_sq"] = target.get("sum_sq", 0) + addend.get("sum_sq", 0)
    target["best"] = max(target.get("best", 0), addend.get("best", 0))
    if n_target == 0:
        target["worst"] = addend.get("worst", 0)
    elif n_addend > 0:
        target["worst"] = min(target.get("worst", 9999), addend.get("worst", 9999))
    for d in ("left", "right", "center"):
        target[d] = target.get(d, 0) + addend.get(d, 0)
    return target


def _read_shot_stats(conn, user_id: int) -> dict:
    """Read + parse shot_stats within the caller's transaction, migrating
    the legacy on_course_shots column if shot_stats is empty."""
    row = conn.execute(
        "SELECT shot_stats, on_course_shots FROM users WHERE id = ?",
        (user_id,),
    ).fetchone()
    if not row:
        return {}
    try:
        stats = json.loads(row["shot_stats"]) if row["shot_stats"] else {}
    except Exception:
        stats = {}
    if not stats and row["on_course_shots"]:
        # Legacy migration — port the old per-club flat shape into shot_stats
        # under the "course" sub-bucket.
        try:
            legacy = json.loads(row["on_course_shots"])
            for club, b in (legacy or {}).items():
                if not isinstance(b, dict):
                    continue
                stats[club] = {"trackman": _new_bucket(), "course": {
                    "count": b.get("count", 0),
                    "total_carry": b.get("total_carry", 0),
                    "sum_sq": b.get("sum_sq", 0),
                    "best": b.get("best", 0),
                    "worst": b.get("worst", 0),
                    "left": b.get("left", 0),
                    "right": b.get("right", 0),
                    "center": b.get("center", 0),
                }}
        except Exception as e:
            print(f"[shot_stats] legacy migration failed: {e}")
    return stats


def record_on_course_shot(
    user_id: int,
    club: str,
    distance: int,
    direction: Optional[str] = None,
) -> None:
    """Log a single on-course shot under shot_stats[club].course."""
    with db(immediate=True) as conn:
        stats = _read_shot_stats(conn, user_id)
        club_data = stats.get(club) or {"trackman": _new_bucket(), "course": _new_bucket()}
        if "course" not in club_data:
            club_data["course"] = _new_bucket()
        _merge_one_shot_into_bucket(club_data["course"], distance, direction)
        stats[club] = club_data
        conn.execute(
            "UPDATE users SET shot_stats = ? WHERE id = ?",
            (json.dumps(stats), user_id),
        )


def record_trackman_session_stats(user_id: int, per_club_stats: dict) -> None:
    """Merge a freshly-parsed Trackman session into shot_stats[club].trackman
    for each club in the session. per_club_stats keys are club labels matching
    CLUB_LABELS, values are bucket-shaped dicts from the session parser."""
    if not per_club_stats:
        return
    with db(immediate=True) as conn:
        stats = _read_shot_stats(conn, user_id)
        for club, session_bucket in per_club_stats.items():
            if not isinstance(session_bucket, dict) or not session_bucket.get("count"):
                continue
            club_data = stats.get(club) or {"trackman": _new_bucket(), "course": _new_bucket()}
            if "trackman" not in club_data:
                club_data["trackman"] = _new_bucket()
            _merge_bucket_into_bucket(club_data["trackman"], session_bucket)
            stats[club] = club_data
        conn.execute(
            "UPDATE users SET shot_stats = ? WHERE id = ?",
            (json.dumps(stats), user_id),
        )


# ────────────────────────────────────────────────────────────
# Course geometry cache (OSM hole layouts for auto-wind / auto-yardage)
#
# Courses don't move, so once we have hole-level geometry it lives forever.
# If a fetch returned no_data, we hold off on retry for a week — OSM
# contributors may have added the course in the meantime.
# ────────────────────────────────────────────────────────────
GEO_RETRY_AFTER_DAYS = 7


def _course_cache_key(course: dict) -> Optional[tuple]:
    """Stable (source, id) key for caching geometry per course. Returns None
    if the course doesn't have a usable id."""
    if not course:
        return None
    cid = course.get("id")
    if cid is None:
        return None
    # Synthetic courses (from scorecard photos) get string ids like 'syn_*';
    # API-sourced courses get integer ids. Normalize both to text for the PK.
    source = "syn" if isinstance(cid, str) and cid.startswith("syn") else "gca"
    return (source, str(cid))


def load_course_geometry(course: dict) -> Optional[dict]:
    """Look up cached geometry. Returns the parsed dict if available
    (even when has_data=False, the cache entry itself signals 'we tried').
    Returns None when there's no cache row yet."""
    key = _course_cache_key(course)
    if key is None:
        return None
    source, cid = key
    with db() as conn:
        row = conn.execute(
            "SELECT has_data, geometry_json, fetched_at FROM course_geometry "
            "WHERE source = ? AND course_id = ?",
            (source, cid),
        ).fetchone()
    if not row:
        return None
    try:
        geo = json.loads(row["geometry_json"]) if row["geometry_json"] else {"has_data": False, "holes": {}}
    except Exception:
        geo = {"has_data": False, "holes": {}}
    geo["_fetched_at"] = row["fetched_at"]
    geo["_has_data"] = bool(row["has_data"])
    return geo


def save_course_geometry(course: dict, geometry: dict) -> None:
    key = _course_cache_key(course)
    if key is None:
        return
    source, cid = key
    name = course.get("club_name") or course.get("course_name") or ""
    with db() as conn:
        conn.execute(
            """INSERT INTO course_geometry (source, course_id, club_name, has_data, geometry_json, fetched_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(source, course_id) DO UPDATE SET
                 club_name = excluded.club_name,
                 has_data = excluded.has_data,
                 geometry_json = excluded.geometry_json,
                 fetched_at = excluded.fetched_at""",
            (source, cid, name, 1 if geometry.get("has_data") else 0,
             json.dumps(geometry), now_iso()),
        )


def _should_retry_geometry_fetch(geo: Optional[dict]) -> bool:
    """If we have nothing cached, fetch. If the cache says no_data but the
    entry is older than GEO_RETRY_AFTER_DAYS, try again (OSM may have been
    edited)."""
    if geo is None:
        return True
    if geo.get("_has_data"):
        return False
    fetched = geo.get("_fetched_at") or ""
    try:
        dt = datetime.fromisoformat(fetched.replace("Z", "+00:00"))
        age_days = (datetime.now(timezone.utc) - dt).total_seconds() / 86400
        return age_days > GEO_RETRY_AFTER_DAYS
    except Exception:
        return True


def _fetch_and_cache_geometry(course: dict) -> None:
    """Synchronous geometry fetch + persist. Designed to be called from a
    background thread so the user's request isn't blocked by Overpass."""
    try:
        loc = course.get("location") or {}
        lat = loc.get("latitude")
        lng = loc.get("longitude")
        if lat is None or lng is None:
            # Without coords, mark as no_data so we don't keep retrying
            save_course_geometry(course, {"has_data": False, "hole_count": 0, "holes": {}})
            return
        geo = fetch_course_geometry(float(lat), float(lng))
        save_course_geometry(course, geo)
        if geo.get("has_data"):
            print(f"[geo] cached geometry for {course.get('club_name')}: {geo['hole_count']} holes")
        else:
            print(f"[geo] no OSM data for {course.get('club_name')} — will retry in {GEO_RETRY_AFTER_DAYS}d")
    except Exception as e:
        print(f"[geo] fetch failed for {course.get('club_name')}: {e}")


def ensure_course_geometry_async(course: dict) -> None:
    """If geometry isn't cached (or is stale no_data), kick off a background
    fetch. Doesn't block. The first message of a round won't have computed
    wind yet, but every subsequent message will."""
    geo = load_course_geometry(course)
    if not _should_retry_geometry_fetch(geo):
        return
    threading.Thread(
        target=_fetch_and_cache_geometry, args=(course,), daemon=True
    ).start()


def relative_wind_for_current_hole(round_state: dict, weather: Optional[dict]) -> Optional[dict]:
    """Look up the current hole's bearing in the cached geometry, combine
    with the NWS wind, and return the computed relative-wind dict. Returns
    None when we don't have enough data (geometry missing, no wind, etc.)."""
    course = round_state.get("course") or {}
    geo = load_course_geometry(course)
    if not geo or not geo.get("_has_data"):
        return None
    current_hole = round_state.get("current_hole") or 1
    hole_data = (geo.get("holes") or {}).get(str(current_hole))
    if not hole_data:
        return None
    bearing = hole_data.get("bearing_deg")
    cur = (weather or {}).get("current") or {}
    return compute_relative_wind(
        bearing,
        cur.get("wind_direction"),
        cur.get("wind_speed"),
    )


def gps_yardage_for_current_hole(
    round_state: dict,
    lat: Optional[float],
    lng: Optional[float],
) -> Optional[dict]:
    """Auto-rangefinder: distance from the player's GPS fix to the current
    hole's green center, using the same cached OSM geometry as auto-wind.
    Returns {hole, yards_to_green} or None (no fix, no geometry, or the
    player isn't plausibly on the hole we think they're on)."""
    if lat is None or lng is None:
        return None
    course = round_state.get("course") or {}
    geo = load_course_geometry(course)
    if not geo or not geo.get("_has_data"):
        return None
    current_hole = round_state.get("current_hole") or 1
    hole_data = (geo.get("holes") or {}).get(str(current_hole))
    if not hole_data:
        return None
    yards = gps_yards_to_green(lat, lng, hole_data.get("green"))
    if yards is None:
        return None
    return {"hole": current_hole, "yards_to_green": yards}
