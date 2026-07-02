"""The chat pipeline — every player message flows through
process_user_message; completed rounds route through handle_round_complete.

Order of operations matters and is documented inline. Anything numeric or
authoritative (running score, shot stats, confidence tiers, relative wind,
GPS yardage) is computed HERE and injected into the prompt — never left to
the model.
"""
import json
import threading
from typing import Optional

from caddy_engine import anthropic_client, build_system_prompt, caddy_reply
from caddy_round import (
    apply_score_to_round_state, calculate_handicap, compute_round_status,
    detect_and_load_course, detect_and_log_score, detect_and_update_tee,
    detect_course_note, format_course_context, format_score_context,
    infer_drive_distance, is_end_of_round, save_hole_note,
)
from caddy_weather import fetch_weather, format_weather_context, has_critical_alert
from caddy_geo import format_gps_yardage_context, format_relative_wind_context
from db import db, now_iso
from store import (
    archive_conversation, clear_round_state, ensure_course_geometry_async,
    gps_yardage_for_current_hole, load_conversation, load_round_state,
    record_on_course_shot, relative_wind_for_current_hole,
    save_conversation, save_round_state,
)


def process_user_message(user: dict, message: str,
                         lat: Optional[float] = None,
                         lng: Optional[float] = None) -> dict:
    """The full message processing pipeline:
    - Detect course mention → load it
    - Detect score report → log it
    - Detect drive distance → infer it
    - Detect end-of-round → trigger save
    - Fetch live weather (if location provided)
    - Build dynamic system context (player + course + score + weather + wind + yardage)
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
        # Kick off OSM geometry fetch in the background so auto-wind is
        # available from the second message onward (Overpass is too slow
        # to block this reply).
        ensure_course_geometry_async(round_state["course"])
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

    # 4. Drive distance inference. Each inferred drive also accumulates into the
    # player's on-course log so it counts toward the confidence tier alongside
    # any Trackman data — confidence comes from any combination of session +
    # course data, not Trackman alone.
    drive_result = infer_drive_distance(message, round_state)
    if drive_result:
        events.append({"type": "drive_inferred", **drive_result})
        try:
            inferred = drive_result.get("inferred_drive")
            if isinstance(inferred, (int, float)) and 50 <= inferred <= 400:
                # "Driver" (capitalized) matches the label Trackman uses in
                # shot_stats so on-course and Trackman buckets merge cleanly.
                record_on_course_shot(user["id"], "Driver", int(inferred))
        except Exception as e:
            print(f"[on-course] driver log failed: {e}")

    # 4b. Passive course note extraction (silent — player never sees this).
    # Runs in a background thread: the note only benefits FUTURE course loads,
    # so there's no reason to hold this reply hostage to a Haiku call. Snapshot
    # the state it needs — the request thread keeps mutating round_state.
    if round_state.get("course"):
        _note_state = {
            "course": round_state["course"],
            "current_hole": round_state.get("current_hole", 1),
        }

        def _detect_and_save_note(msg: str, state: dict) -> None:
            try:
                note_result = detect_course_note(msg, state)
                if note_result:
                    save_hole_note(state["course"], note_result["hole"], note_result["note"])
            except Exception as e:
                print(f"[notes] background save failed: {e}")

        threading.Thread(
            target=_detect_and_save_note, args=(message, _note_state), daemon=True
        ).start()

    # 5. Build dynamic context for Claude
    course_ctx = format_course_context(round_state)
    score_ctx = format_score_context(round_state)
    weather_ctx = format_weather_context(weather) if weather else ""
    # Auto-computed relative wind for the current hole, when we have cached
    # OSM geometry. When not available, Claude falls back to the prompt rule
    # ("ask once per hole, then reuse the player's answer").
    relative_wind = relative_wind_for_current_hole(round_state, weather)
    wind_ctx = format_relative_wind_context(relative_wind, round_state.get("current_hole"))
    # Auto-rangefinder: GPS distance to the current hole's green center,
    # from the same cached geometry auto-wind uses.
    gps_yardage = gps_yardage_for_current_hole(round_state, lat, lng)
    yardage_ctx = format_gps_yardage_context(gps_yardage)
    round_context = course_ctx + score_ctx + weather_ctx + wind_ctx + yardage_ctx
    if relative_wind:
        events.append({"type": "relative_wind", **relative_wind})
    if gps_yardage:
        events.append({"type": "gps_yardage", **gps_yardage})

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
        # Course was mentioned but lookup failed — make the scorecard-photo
        # option crystal clear because it unlocks all the course intelligence
        # (yardages, hazards, par per hole). Player should still be able to
        # decline and play without it.
        round_context += (
            f"\n\nNOTE: Player mentioned a course (\"{course_not_found_query}\") but I couldn't find it in the database. "
            f"Say so in one sentence and EXPLICITLY OFFER the scorecard photo path: \"If you snap a photo of the "
            f"scorecard with the camera button below, I can pull the hole yardages, par, and hazards from it and give "
            f"you much better advice the rest of the round. Otherwise, no worries — just call out your distances as "
            f"we go and we'll play it by feel.\" Make the value prop of the photo upload clear (better advice with "
            f"the data, fine without it). Don't beg — say it once, let them choose."
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
