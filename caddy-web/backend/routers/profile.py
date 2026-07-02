"""Player profile endpoints: bag setup, Trackman ingestion, round deletion."""
import json
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field

from caddy_round import calculate_handicap
from db import db
from deps import get_current_user
from store import record_trackman_session_stats, user_dict

router = APIRouter()


class BagSetupRequest(BaseModel):
    bag: dict[str, Optional[int]]
    driver_miss: Optional[str] = Field(None, max_length=300)
    iron_miss: Optional[str] = Field(None, max_length=300)
    home_course: Optional[str] = Field(None, max_length=120)


@router.post("/api/me/setup")
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


@router.delete("/api/me/rounds/{round_index}")
def delete_round(round_index: int, user: dict = Depends(get_current_user)):
    """Remove a round from the user's history by its array index, then
    recalculate handicap from the remaining rounds."""
    rounds = user.get("rounds") or []
    if round_index < 0 or round_index >= len(rounds):
        raise HTTPException(404, "Round not found")
    removed = rounds.pop(round_index)
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


@router.post("/api/me/trackman")
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
        extract_report_id,
    )

    url_clean = (url or "").strip()
    if not url_clean and not csv_file:
        raise HTTPException(400, "Provide either a Trackman report URL or a CSV file.")

    # Parse the input into (text, count, per_club_stats). Both the URL and the
    # CSV path return the same 3-tuple shape so the merge into shot_stats is uniform.
    session_data_str: Optional[str] = None
    shot_count = 0
    per_club_stats: dict = {}
    report_id: Optional[str] = None  # only set for URL uploads
    is_duplicate = False

    if url_clean:
        session = fetch_trackman_report(url_clean)
        if not session:
            raise HTTPException(
                400,
                "Couldn't load that Trackman report. Double-check the URL or paste the report ID instead.",
            )
        report_id = extract_report_id(url_clean)
        session_data_str, shot_count, per_club_stats = summarize_trackman_session(session)
        if not session_data_str:
            raise HTTPException(400, "The Trackman report loaded but contained no shot data.")

    elif csv_file:
        raw = await csv_file.read()
        try:
            csv_text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            csv_text = raw.decode("latin-1", errors="ignore")
        session_data_str, shot_count, per_club_stats = parse_trackman_csv_text(csv_text)
        if not session_data_str:
            raise HTTPException(400, "Couldn't parse that CSV — make sure it's a Trackman export.")

    # Dedupe by Trackman session ID. If this URL's session is already on file,
    # skip the structured-stats merge — re-uploading the same session must not
    # double-count. The narrative still gets re-generated so the player can
    # safely re-process if Claude was unavailable last time. CSV uploads have
    # no canonical ID so they always merge (we trust the player not to re-upload
    # the same CSV).
    if report_id:
        with db(immediate=True) as conn:
            row = conn.execute(
                "SELECT trackman_session_ids FROM users WHERE id = ?",
                (user["id"],),
            ).fetchone()
            existing_ids = []
            if row and row["trackman_session_ids"]:
                try:
                    existing_ids = json.loads(row["trackman_session_ids"]) or []
                except Exception:
                    existing_ids = []
            if report_id in existing_ids:
                is_duplicate = True
            else:
                existing_ids.append(report_id)
                conn.execute(
                    "UPDATE users SET trackman_session_ids = ? WHERE id = ?",
                    (json.dumps(existing_ids), user["id"]),
                )

    # Only merge structured stats if this isn't a duplicate session.
    if not is_duplicate:
        record_trackman_session_stats(user["id"], per_club_stats)

    # Ask Claude to update the qualitative tendencies narrative (patterns,
    # miss shapes, swing observations — NOT numeric averages).
    first_name = (user.get("full_name") or "Player").split()[0]
    new_summary = generate_tendencies_summary(
        first_name=first_name,
        existing_summary=user.get("tendencies_summary"),
        session_data_str=session_data_str,
    )
    if not new_summary:
        # Don't fail the request if Claude is unavailable — the structured stats
        # already saved. The narrative is best-effort and will update next time.
        new_summary = user.get("tendencies_summary") or "(Tendencies narrative will populate after the next Trackman upload when Anthropic is available.)"

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
        "duplicate": is_duplicate,
    }
