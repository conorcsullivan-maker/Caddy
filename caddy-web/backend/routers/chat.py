"""Caddy chat endpoints: message/voice/photo, history, weather, scorecard
edits, conversation archive + owner-only .docx downloads, and TTS."""
import json
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import Response as FastAPIResponse
from pydantic import BaseModel, Field

from caddy_engine import (
    caddy_reply, classify_photo_subject, extract_scorecard_from_image,
    synthesize_speech, transcribe_audio,
)
from caddy_round import (
    build_course_from_synthetic, find_synthetic_course, find_tee,
    format_course_context, format_score_context, get_course,
    save_synthetic_course, search_course,
)
from caddy_weather import fetch_weather, format_weather_context
from db import db, now_iso
from deps import get_current_user
from pipeline import process_user_message
from store import (
    EXPORT_ALLOWED_USERNAMES, archive_conversation, clear_round_state,
    ensure_course_geometry_async, load_conversation, load_round_state,
    save_conversation, save_round_state,
)

router = APIRouter()


class CaddyMessageRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    lat: Optional[float] = None
    lng: Optional[float] = None


class ScoreEditRequest(BaseModel):
    hole: int = Field(ge=1, le=18)
    score: Optional[int] = Field(default=None, ge=1, le=20)


@router.get("/api/caddy/history")
def get_history(user: dict = Depends(get_current_user)):
    return {
        "history": load_conversation(user["id"]),
        "round_state": load_round_state(user["id"]),
    }


@router.get("/api/caddy/weather")
def get_weather(lat: float, lng: float, user: dict = Depends(get_current_user)):
    """Standalone weather lookup so the weather strip can populate on page load
    without waiting for the user to send a chat message."""
    return {"weather": fetch_weather(lat, lng)}


@router.post("/api/caddy/edit-score")
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


@router.post("/api/caddy/reset")
def reset_history(user: dict = Depends(get_current_user)):
    """Archive the current conversation as 'casual' and start fresh."""
    state = load_round_state(user["id"])
    course_name = (state.get("course") or {}).get("club_name")
    archived_id = archive_conversation(user["id"], kind="casual", course_name=course_name)
    clear_round_state(user["id"])
    return {"status": "reset", "archived_conversation_id": archived_id}


@router.post("/api/caddy/message")
def caddy_message(payload: CaddyMessageRequest, user: dict = Depends(get_current_user)):
    """Text message → full processing pipeline → response + events."""
    return process_user_message(user, payload.message, lat=payload.lat, lng=payload.lng)


@router.post("/api/caddy/voice")
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


@router.post("/api/caddy/photo")
async def caddy_photo(
    image: UploadFile = File(...),
    message: Optional[str] = Form(None),
    lat: Optional[float] = None,
    lng: Optional[float] = None,
    user: dict = Depends(get_current_user),
):
    """Photo → Haiku classifies it → scorecard photos load the course, scene
    photos (the player's lie / shot situation) go through the chat pipeline
    with the image attached so Caddy folds what it sees into the advice."""
    content_type = image.content_type or "image/jpeg"
    print(f"[photo] user={user['username']} filename={image.filename!r} content_type={content_type!r}")
    if not content_type.startswith("image/"):
        raise HTTPException(400, f"File must be an image, got {content_type!r}")
    image_bytes = await image.read()
    print(f"[photo] image size: {len(image_bytes)} bytes ({len(image_bytes)/1024:.1f} KB)")
    if len(image_bytes) > 10 * 1024 * 1024:
        raise HTTPException(400, "Image too large (max 10MB)")

    photo_kind = classify_photo_subject(image_bytes, content_type)
    print(f"[photo] classified as: {photo_kind}")
    if photo_kind == "scene":
        return process_user_message(
            user, message or "", lat=lat, lng=lng,
            image_bytes=image_bytes, image_media_type=content_type,
        )

    extracted = extract_scorecard_from_image(image_bytes, content_type)

    # Classified as a scorecard but unreadable. Mid-round (course already
    # loaded) that usually means the classifier was wrong about a shot photo —
    # fall through to the scene pipeline and let Caddy say what it sees. With
    # no course loaded, the player is probably genuinely trying to load a
    # scorecard, so give retake guidance instead.
    if not extracted:
        round_state = load_round_state(user["id"])
        if round_state.get("course"):
            return process_user_message(
                user, message or "", lat=lat, lng=lng,
                image_bytes=image_bytes, image_media_type=content_type,
            )
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
            ensure_course_geometry_async(course_data)
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


@router.get("/api/caddy/conversations")
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


def _require_export_access(user: dict) -> None:
    """Gate for the conversation-download endpoints. 403s any user not on
    the EXPORT_ALLOWED_USERNAMES allowlist."""
    if (user.get("username") or "") not in EXPORT_ALLOWED_USERNAMES:
        raise HTTPException(403, "Conversation export is not enabled for this account.")


# NOTE: this route must be declared before /conversations/{conv_id} —
# otherwise "active" would be captured as a conv_id path param.
@router.get("/api/caddy/conversations/active/download")
def download_active_conversation(user: dict = Depends(get_current_user)):
    """Stream the user's in-progress chat (the one still living in
    users.conversation_history, not yet archived) as a .docx."""
    _require_export_access(user)
    history = load_conversation(user["id"])
    if not history:
        raise HTTPException(404, "No active conversation to download.")
    round_state = load_round_state(user["id"])
    course = round_state.get("course") or {}
    course_name = course.get("club_name")
    hole_scores = round_state.get("hole_scores") or []
    total_score = sum(s for s in hole_scores if isinstance(s, int)) if any(hole_scores) else None
    round_metadata = {
        "hole_scores": hole_scores,
        "course_rating": (round_state.get("tee") or {}).get("course_rating"),
        "slope_rating": (round_state.get("tee") or {}).get("slope_rating"),
    } if hole_scores else None

    from caddy_export import conversation_to_docx_bytes, safe_filename
    blob = conversation_to_docx_bytes(
        full_name=user.get("full_name") or user.get("username") or "Player",
        username=user.get("username") or "",
        kind="active",
        course_name=course_name,
        total_score=total_score,
        started_at=round_state.get("started_at"),
        ended_at=None,
        round_metadata=round_metadata,
        messages=history,
        is_active=True,
    )
    filename = safe_filename("Caddy_Active", course_name, now_iso())
    return FastAPIResponse(
        content=blob,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/api/caddy/conversations/{conv_id}")
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


@router.get("/api/caddy/conversations/{conv_id}/download")
def download_conversation(conv_id: int, user: dict = Depends(get_current_user)):
    """Stream an archived conversation as a .docx file. Allowlist-gated;
    scoped strictly to the requesting user — a player can only download
    conversations they own, never anyone else's."""
    _require_export_access(user)
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM conversations WHERE id = ? AND user_id = ?",
            (conv_id, user["id"]),
        ).fetchone()
    if not row:
        raise HTTPException(404, "Conversation not found")
    d = dict(row)
    try:
        messages = json.loads(d["messages"]) if d.get("messages") else []
    except Exception:
        messages = []
    round_metadata = None
    if d.get("round_metadata"):
        try:
            round_metadata = json.loads(d["round_metadata"])
        except Exception:
            round_metadata = None

    from caddy_export import conversation_to_docx_bytes, safe_filename
    blob = conversation_to_docx_bytes(
        full_name=user.get("full_name") or user.get("username") or "Player",
        username=user.get("username") or "",
        kind=d.get("kind") or "casual",
        course_name=d.get("course_name"),
        total_score=d.get("total_score"),
        started_at=d.get("started_at"),
        ended_at=d.get("ended_at"),
        round_metadata=round_metadata,
        messages=messages,
        is_active=False,
    )
    prefix = "Caddy_Round" if (d.get("kind") == "round") else "Caddy_Chat"
    filename = safe_filename(prefix, d.get("course_name"), d.get("ended_at"))
    return FastAPIResponse(
        content=blob,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/api/caddy/speak")
def caddy_speak(payload: CaddyMessageRequest, user: dict = Depends(get_current_user)):
    """Generate TTS audio for the given text (used for replaying responses)."""
    audio = synthesize_speech(payload.message)
    return FastAPIResponse(content=audio, media_type="audio/mpeg")


@router.get("/api/caddy/speak")
def caddy_speak_get(message: str, user: dict = Depends(get_current_user)):
    """GET variant of TTS for native audio players that stream directly from
    a URL with auth headers (expo-audio's AudioSource). Same output as the
    POST route; the text rides in the query string."""
    audio = synthesize_speech(message[:2000])
    return FastAPIResponse(content=audio, media_type="audio/mpeg")
