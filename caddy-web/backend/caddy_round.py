"""
Round intelligence — extracts course mentions, hole scores, drive distances,
and end-of-round triggers from natural-language chat messages.

Ported from the Mac voice version (caddy_voice.py) and adapted to work with
per-request state (loaded from DB) rather than module-level globals.
"""
import json
import os
import re
from typing import Optional

import requests
import anthropic

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
GOLF_COURSE_API_KEY = os.environ.get("GOLF_COURSE_API_KEY")

anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None

GOLF_COURSE_API_BASE = "https://api.golfcourseapi.com/v1"
GOLF_COURSE_HEADERS = (
    {"Authorization": f"Key {GOLF_COURSE_API_KEY}"} if GOLF_COURSE_API_KEY else {}
)

TEE_COLORS = ["black", "blue", "gold", "white", "green", "red", "silver", "championship", "tips", "tournament"]

def _extract_json(text: str) -> Optional[dict]:
    """Strip optional markdown code fences and parse JSON. Returns None on failure."""
    if not text:
        return None
    cleaned = text.strip()
    # Remove ```json or ``` fences
    if cleaned.startswith("```"):
        # Find first newline to skip the opening fence (and optional 'json' tag)
        first_nl = cleaned.find("\n")
        if first_nl != -1:
            cleaned = cleaned[first_nl + 1:]
        # Strip trailing fence
        if cleaned.rstrip().endswith("```"):
            cleaned = cleaned.rstrip()[:-3]
    cleaned = cleaned.strip()
    # As a fallback, try to find the first { ... } block
    if not cleaned.startswith("{"):
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if match:
            cleaned = match.group(0)
    try:
        return json.loads(cleaned)
    except Exception:
        return None


END_ROUND_PHRASES = [
    "round complete", "round is complete", "we're finished", "we are finished",
    "round's done", "round is done", "that's a wrap", "end of round",
    "finished the round", "that's the round", "i'm done for the day",
    "we're done for the day", "save the round",
]

SCORE_TRIGGER_KEYWORDS = [
    "birdie", "eagle", "bogey", "double", "triple", "par",
    "made par", "got par", "i shot", "i made", "i got a",
    "hole in one", "ace",
]

COURSE_MENTION_KEYWORDS = ["at ", "playing", "arrived", "tee off", "course", "club", "we're at", "im at", "i'm at"]


# ────────────────────────────────────────────────────────────
# Course detection / loading
# ────────────────────────────────────────────────────────────
def search_course(query: str) -> list:
    if not GOLF_COURSE_API_KEY:
        return []
    try:
        r = requests.get(
            f"{GOLF_COURSE_API_BASE}/search",
            headers=GOLF_COURSE_HEADERS,
            params={"search_query": query},
            timeout=8,
        )
        return r.json().get("courses", []) if r.status_code == 200 else []
    except Exception:
        return []


def get_course(course_id: int) -> Optional[dict]:
    try:
        r = requests.get(
            f"{GOLF_COURSE_API_BASE}/courses/{course_id}",
            headers=GOLF_COURSE_HEADERS,
            timeout=8,
        )
        if r.status_code != 200:
            return None
        data = r.json()
        return data.get("course", data)
    except Exception:
        return None


def find_tee(course: dict, tee_color: Optional[str] = None) -> Optional[dict]:
    male_tees = course.get("tees", {}).get("male", [])
    if not male_tees:
        return None
    if tee_color:
        for tee in male_tees:
            if tee_color.lower() in tee["tee_name"].lower():
                return tee
    return male_tees[0]


def extract_tee_color(text: str) -> Optional[str]:
    for color in TEE_COLORS:
        if color in text.lower():
            return color
    return None


def detect_and_load_course(text: str, current_round_state: dict) -> Optional[dict]:
    """If the text mentions a golf course AND no course is currently loaded,
    look it up in the API and return the loaded course/tee. Otherwise None."""
    if current_round_state.get("course"):
        return None  # course already loaded, don't re-detect
    if not anthropic_client:
        return None
    if not any(k in text.lower() for k in COURSE_MENTION_KEYWORDS):
        return None

    response = anthropic_client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=30,
        messages=[{"role": "user", "content": (
            f'Does this text mention a specific golf course or golf club by name? "{text}"\n'
            'If yes, return only the course or club name. If no, return "none".'
        )}],
    )
    result = response.content[0].text.strip()
    if result.lower() in ("none", "no", "") or len(result) < 4:
        return None

    courses = search_course(result)
    if not courses:
        return None
    course_data = get_course(courses[0]["id"])
    if not course_data:
        return None
    tee_color = extract_tee_color(text)
    tee = find_tee(course_data, tee_color)
    if not tee:
        return None

    return {"course": course_data, "tee": tee}


# ────────────────────────────────────────────────────────────
# Score detection / logging
# ────────────────────────────────────────────────────────────
def get_hole_par(round_state: dict, hole_number: int) -> Optional[int]:
    tee = round_state.get("tee")
    if not tee:
        return None
    holes = tee.get("holes", [])
    if hole_number < 1 or hole_number > len(holes):
        return None
    return holes[hole_number - 1].get("par")


def detect_and_log_score(text: str, round_state: dict) -> Optional[dict]:
    """If the text reports a hole score, return {hole, score, par}.
    Doesn't mutate round_state — caller decides whether to apply."""
    if not anthropic_client:
        return None

    text_lower = text.lower()
    # Strict single-word filter: only golf-specific words allowed alone (no "par")
    safe_singles = {"birdie", "eagle", "bogey", "ace", "hole-in-one"}
    words = [w.lower().strip(".,!?") for w in text.split()]
    is_safe_single = any(w in safe_singles for w in words)

    matches_keyword = any(k in text_lower for k in SCORE_TRIGGER_KEYWORDS)
    matches_a_number = any(f"a {n}" in text_lower for n in
                            ["two", "three", "four", "five", "six", "seven", "eight", "nine",
                             "2", "3", "4", "5", "6", "7", "8", "9"])

    if not (is_safe_single or matches_keyword or matches_a_number):
        return None

    current_hole = round_state.get("current_hole", 1)
    par = get_hole_par(round_state, current_hole)
    par_info = f"Current hole: {current_hole}, par: {par}." if par else f"Current hole: {current_hole}, par unknown."

    response = anthropic_client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=80,
        messages=[{"role": "user", "content": (
            f'Golfer said: "{text}"\n{par_info}\n'
            'Is the player reporting their score for a hole? '
            'Return JSON only: {"score": integer_or_null, "hole": integer_or_null}\n'
            'birdie=par-1, eagle=par-2, bogey=par+1, double=par+2, triple=par+3, hole in one=1.\n'
            'If not a score report return {"score": null, "hole": null}'
        )}],
    )
    data = _extract_json(response.content[0].text)
    if not data:
        return None
    score = data.get("score")
    if not score:
        return None
    hole = data.get("hole") or current_hole
    return {"hole": hole, "score": score, "par": get_hole_par(round_state, hole)}


def apply_score_to_round_state(round_state: dict, hole: int, score: int) -> dict:
    """Mutate round_state with new score and advance the current hole."""
    hole_scores = round_state.get("hole_scores") or []
    while len(hole_scores) < hole:
        hole_scores.append(None)
    hole_scores[hole - 1] = score
    round_state["hole_scores"] = hole_scores
    round_state["current_hole"] = max(hole + 1, round_state.get("current_hole", 1) + 1)
    return round_state


# ────────────────────────────────────────────────────────────
# Drive distance inference
# ────────────────────────────────────────────────────────────
def detect_remaining_yardage(text: str) -> Optional[int]:
    """If the player mentions a remaining yardage, return it."""
    # Match patterns like "165 yards", "165 to the pin", "I have 145"
    matches = re.findall(r"\b(\d{2,3})\s*(?:yards?|yds?|to\s+the\s+pin|to\s+pin|out|left|away)?\b", text.lower())
    yardages = [int(m) for m in matches if 30 <= int(m) <= 600]
    if not yardages:
        return None
    # Take the smallest (probably the remaining yardage, not the hole length)
    return min(yardages)


def infer_drive_distance(text: str, round_state: dict) -> Optional[dict]:
    """If we know the current hole length and the player tells us their remaining
    yardage, infer how far they hit their tee shot."""
    course = round_state.get("course")
    tee = round_state.get("tee")
    current_hole = round_state.get("current_hole", 1)
    if not course or not tee:
        return None
    holes = tee.get("holes", [])
    if current_hole < 1 or current_hole > len(holes):
        return None
    hole_yardage = holes[current_hole - 1].get("yardage")
    if not hole_yardage:
        return None

    remaining = detect_remaining_yardage(text)
    if not remaining:
        return None

    # Sanity: drive distance has to be reasonable (50-400 yds)
    inferred = hole_yardage - remaining
    if inferred < 50 or inferred > 400:
        return None
    return {"hole": current_hole, "hole_yardage": hole_yardage, "remaining": remaining, "inferred_drive": inferred}


# ────────────────────────────────────────────────────────────
# End-of-round detection
# ────────────────────────────────────────────────────────────
def is_end_of_round(text: str) -> bool:
    text_lower = text.lower()
    return any(p in text_lower for p in END_ROUND_PHRASES)


# ────────────────────────────────────────────────────────────
# Handicap calculation (WHS formula)
# ────────────────────────────────────────────────────────────
def calculate_handicap(rounds: list) -> Optional[float]:
    differentials = [r["differential"] for r in rounds if r.get("differential") is not None]
    recent = differentials[-20:]
    n = len(recent)
    if n < 3:
        return None
    whs_table = {
        3: (1, -2.0), 4: (1, -1.0), 5: (1, 0.0),
        6: (2, -1.0), 7: (2, 0.0), 8: (2, 0.0),
        9: (3, 0.0), 10: (4, 0.0), 11: (4, 0.0),
        12: (4, 0.0), 13: (5, 0.0), 14: (5, 0.0),
        15: (6, 0.0), 16: (6, 0.0), 17: (7, 0.0),
        18: (7, 0.0), 19: (8, 0.0), 20: (8, 0.0),
    }
    count, adjustment = whs_table.get(n, (8, 0.0))
    best = sorted(recent)[:count]
    return round((sum(best) / count + adjustment) * 0.96, 1)


# ────────────────────────────────────────────────────────────
# Score state + course context for system prompt
# ────────────────────────────────────────────────────────────
def format_course_context(round_state: dict) -> str:
    course = round_state.get("course")
    tee = round_state.get("tee")
    if not course or not tee:
        return ""
    club_name = course.get("club_name", "Unknown Course")
    lines = [
        f"\n=== ACTIVE COURSE: {club_name} ===",
        f"Tee: {tee['tee_name']} | Rating: {tee.get('course_rating')} | Slope: {tee.get('slope_rating')} | Total: {tee.get('total_yards')} yards",
        "",
        "Hole-by-hole yardages:",
    ]
    for i, hole in enumerate(tee.get("holes", []), 1):
        lines.append(
            f"  Hole {i}: Par {hole.get('par')}, {hole.get('yardage')} yards (HCP {hole.get('handicap')})"
        )
    return "\n".join(lines)


def format_score_context(round_state: dict) -> str:
    hole_scores = round_state.get("hole_scores") or []
    logged = [(i + 1, s) for i, s in enumerate(hole_scores) if s is not None]
    if not logged:
        return ""
    lines = ["\n=== CURRENT SCORECARD ==="]
    par_total = 0
    total = 0
    for hole_num, score in logged:
        par = get_hole_par(round_state, hole_num)
        if par:
            par_total += par
            diff = score - par
            label = {-2: "eagle", -1: "birdie", 0: "par", 1: "bogey", 2: "double", 3: "triple"}.get(diff, f"+{diff}" if diff > 0 else str(diff))
            lines.append(f"  Hole {hole_num}: {score} ({label})")
        else:
            lines.append(f"  Hole {hole_num}: {score}")
        total += score
    lines.append(f"Total: {total} through {len(logged)} holes")
    if par_total:
        vs = total - par_total
        rel = "even" if vs == 0 else (f"+{vs}" if vs > 0 else str(vs))
        lines.append(f"Vs par: {rel}")
    lines.append(f"Current hole: {round_state.get('current_hole', len(logged) + 1)}")
    return "\n".join(lines)
