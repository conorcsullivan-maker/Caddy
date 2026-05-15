"""
Round intelligence — extracts course mentions, hole scores, drive distances,
and end-of-round triggers from natural-language chat messages.

Ported from the Mac voice version (caddy_voice.py) and adapted to work with
per-request state (loaded from DB) rather than module-level globals.
"""
import json
import os
import re
from pathlib import Path
from typing import Optional

import requests
import anthropic

# ────────────────────────────────────────────────────────────
# Course overrides — augment API data with hole nicknames,
# corrected yardages, and hazard notes for specific courses.
# Files live in course_overrides/*.json and are loaded once at import.
# ────────────────────────────────────────────────────────────
_OVERRIDES_DIR = Path(__file__).parent / "course_overrides"
_COURSE_OVERRIDES: list = []   # patch-style: add nicknames/notes/yardages to API courses
_SYNTHETIC_COURSES: list = []  # full scorecard: used when course isn't in the Golf Course API


def _load_overrides():
    if not _OVERRIDES_DIR.exists():
        return
    for f in _OVERRIDES_DIR.glob("*.json"):
        try:
            with open(f) as fh:
                data = json.load(fh)
            if data.get("synthetic"):
                _SYNTHETIC_COURSES.append(data)
            else:
                _COURSE_OVERRIDES.append(data)
        except Exception as e:
            print(f"Failed to load override {f.name}: {e}")


_load_overrides()


def find_synthetic_course(name: str) -> Optional[dict]:
    """Return the synthetic override whose name_contains matches, or None."""
    name_lower = name.lower().strip()
    for syn in _SYNTHETIC_COURSES:
        needle = (syn.get("course_match") or {}).get("name_contains", "").lower()
        if needle and (needle in name_lower or name_lower in needle):
            return syn
    return None


def build_course_from_synthetic(syn: dict) -> dict:
    """Build a course dict matching Golf Course API shape from a synthetic override."""
    male_tees = []
    for td in syn.get("tees") or []:
        holes = [
            {
                "par": h.get("par"),
                "yardage": h.get("yardage"),
                "handicap": h.get("handicap"),
            }
            for h in td.get("holes") or []
        ]
        male_tees.append({
            "tee_name": (td.get("tee_name") or "WHITE").upper(),
            "course_rating": td.get("course_rating"),
            "slope_rating": td.get("slope_rating"),
            "total_yards": td.get("total_yards") or sum(h.get("yardage") or 0 for h in holes),
            "holes": holes,
        })
    course_name = syn.get("course_name") or (syn.get("course_match") or {}).get("name_contains", "Unknown Course")
    return {
        "id": None,
        "club_name": course_name,
        "location": syn.get("location"),
        "_synthetic": True,
        "_override_notes": syn.get("course_notes"),
        "tees": {"male": male_tees, "female": []},
    }


def save_synthetic_course(extracted: dict) -> dict:
    """Persist extracted scorecard data as a synthetic course override JSON and cache in memory."""
    course_name = extracted.get("course_name", "unknown_course")
    slug = re.sub(r"[^a-z0-9]+", "_", course_name.lower()).strip("_")
    _OVERRIDES_DIR.mkdir(exist_ok=True)
    filepath = _OVERRIDES_DIR / f"{slug}.json"

    city = extracted.get("city") or ""
    state = extracted.get("state") or ""
    location = f"{city}, {state}".strip(", ") if city or state else ""

    override = {
        "synthetic": True,
        "course_match": {"name_contains": course_name},
        "course_name": course_name,
        "location": location,
        "course_notes": f"Scorecard uploaded by player",
        "tees": extracted.get("tees") or [],
    }
    with open(filepath, "w") as f:
        json.dump(override, f, indent=2)
    _SYNTHETIC_COURSES.append(override)
    return override


# ────────────────────────────────────────────────────────────
# Crowdsourced hole notes
# ────────────────────────────────────────────────────────────
_HAZARD_KEYWORDS = [
    "water", "bunker", "sand trap", "out of bounds", "ob ",
    "tree", "trees", "rough", "hazard", "creek", "pond", "lake",
    "river", "waste area", "ravine", "ditch", "marsh", "cliff",
    "rocks", "railroad", "cart path",
]


def detect_course_note(text: str, round_state: dict) -> Optional[dict]:
    """If the message contains course-specific intel for the current hole, extract it.
    Returns {hole, note} or None. Runs a cheap Haiku call only when hazard keywords present."""
    if not anthropic_client or not round_state.get("course"):
        return None
    if len(text.split()) < 5:
        return None
    text_lower = text.lower()
    if not any(kw in text_lower for kw in _HAZARD_KEYWORDS):
        return None

    current_hole = round_state.get("current_hole", 1)
    response = anthropic_client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=60,
        messages=[{"role": "user", "content": (
            f'Golfer on hole {current_hole} said: "{text}"\n'
            'Does this describe a specific physical hazard or trouble area on this hole '
            '(e.g. water location, bunker position, OB, carry distance to hazard)? '
            'Return JSON only: {"note": "brief fact under 15 words"} or {"note": null}'
        )}],
    )
    data = _extract_json(response.content[0].text)
    if not data or not data.get("note"):
        return None
    return {"hole": current_hole, "note": data["note"]}


def save_hole_note(course: dict, hole_num: int, note_text: str):
    """Add a crowdsourced note to a hole. Confirms after 2 mentions. Saves to disk + updates cache."""
    course_name = course.get("club_name", "unknown")
    slug = re.sub(r"[^a-z0-9]+", "_", course_name.lower()).strip("_")
    _OVERRIDES_DIR.mkdir(exist_ok=True)
    filepath = _OVERRIDES_DIR / f"{slug}.json"

    if filepath.exists():
        with open(filepath) as f:
            override = json.load(f)
    else:
        override = {
            "synthetic": False,
            "course_match": {"name_contains": course_name},
            "holes": [],
        }

    holes = override.setdefault("holes", [])
    hole_entry = next((h for h in holes if h.get("hole") == hole_num), None)
    if not hole_entry:
        hole_entry = {"hole": hole_num}
        holes.append(hole_entry)

    crowdsourced = hole_entry.setdefault("crowdsourced", [])
    note_lower = note_text.lower().strip()
    existing = next(
        (n for n in crowdsourced if note_lower in n["text"].lower() or n["text"].lower() in note_lower),
        None,
    )
    if existing:
        existing["mentions"] = existing.get("mentions", 1) + 1
        if existing["mentions"] >= 2:
            existing["confirmed"] = True
    else:
        crowdsourced.append({"text": note_text, "mentions": 1, "confirmed": False})

    with open(filepath, "w") as f:
        json.dump(override, f, indent=2)

    # Update in-memory cache
    is_synthetic = override.get("synthetic", False)
    target = _SYNTHETIC_COURSES if is_synthetic else _COURSE_OVERRIDES
    name_key = course_name.lower()
    for i, ov in enumerate(target):
        if (ov.get("course_match") or {}).get("name_contains", "").lower() == name_key:
            target[i] = override
            return
    target.append(override)


def _find_override_for(course: dict) -> Optional[dict]:
    """Find an override that matches this course (by API id or name substring)."""
    course_id = course.get("id")
    course_name = (course.get("club_name") or "").lower()
    for ov in _COURSE_OVERRIDES:
        match = ov.get("course_match") or {}
        if match.get("api_id") and match["api_id"] == course_id:
            return ov
        name_contains = (match.get("name_contains") or "").lower()
        if name_contains and name_contains in course_name:
            return ov
    return None


def _apply_overrides_to_course(course: dict) -> dict:
    """Mutate the course dict in place with override data (nicknames, yardage fixes)."""
    ov = _find_override_for(course)
    if not ov:
        return course
    course["_override_notes"] = ov.get("course_notes")
    hole_overrides = {h.get("hole"): h for h in ov.get("holes") or []}
    for tee in (course.get("tees", {}).get("male") or []) + (course.get("tees", {}).get("female") or []):
        tee_name = (tee.get("tee_name") or "").upper()
        for i, hole in enumerate(tee.get("holes") or [], 1):
            override = hole_overrides.get(i)
            if not override:
                continue
            if override.get("nickname"):
                hole["nickname"] = override["nickname"]
            notes_parts = []
            if override.get("notes"):
                notes_parts.append(override["notes"])
            confirmed = [n["text"] for n in (override.get("crowdsourced") or []) if n.get("confirmed")]
            if confirmed:
                notes_parts.append(". ".join(confirmed))
            if notes_parts:
                hole["notes"] = " ".join(notes_parts)
            yardage_overrides = override.get("yardage_overrides") or {}
            if tee_name in yardage_overrides:
                hole["yardage"] = yardage_overrides[tee_name]
    return course

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
GOLF_COURSE_API_KEY = os.environ.get("GOLF_COURSE_API_KEY")

anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None

GOLF_COURSE_API_BASE = "https://api.golfcourseapi.com/v1"
GOLF_COURSE_HEADERS = (
    {"Authorization": f"Key {GOLF_COURSE_API_KEY}"} if GOLF_COURSE_API_KEY else {}
)

TEE_COLORS = ["black", "blue", "gold", "white", "green", "red", "silver", "championship", "tips", "tournament"]

def _extract_json(text: str):
    """Strip optional markdown code fences and parse JSON. Returns None on failure.
    Accepts both objects and arrays at the top level."""
    if not text:
        return None
    cleaned = text.strip()
    # Remove ```json or ``` fences
    if cleaned.startswith("```"):
        first_nl = cleaned.find("\n")
        if first_nl != -1:
            cleaned = cleaned[first_nl + 1:]
        if cleaned.rstrip().endswith("```"):
            cleaned = cleaned.rstrip()[:-3]
    cleaned = cleaned.strip()
    # If it doesn't start with { or [, try to find the first JSON-shaped block
    if not (cleaned.startswith("{") or cleaned.startswith("[")):
        match = re.search(r"[\{\[].*[\}\]]", cleaned, re.DOTALL)
        if match:
            cleaned = match.group(0)
    try:
        return json.loads(cleaned)
    except Exception:
        return None


def _get_alternative_spellings(name: str) -> list:
    """If a course name didn't match the API, ask Haiku for likely variations."""
    if not anthropic_client or not name:
        return []
    try:
        response = anthropic_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=120,
            messages=[{"role": "user", "content": (
                f'A user mentioned the golf course name "{name}" but searching a golf-course database returned no results. '
                'The course probably exists but is spelled slightly differently (extra/missing space, hyphen, alternate word like "Course" vs "Club" vs "Golf Course", different punctuation). '
                'Suggest up to 3 likely alternative spellings or canonical names.\n'
                'Return ONLY a JSON array of strings, no commentary. Example: ["Butter Brook", "Butter Brook Golf Club"]'
            )}],
        )
        data = _extract_json(response.content[0].text)
        if isinstance(data, list):
            return [s for s in data if isinstance(s, str) and s.strip() and s.strip().lower() != name.strip().lower()]
    except Exception:
        pass
    return []


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
def _raw_search(query: str) -> list:
    if not GOLF_COURSE_API_KEY or not query.strip():
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


def search_course(query: str) -> list:
    """Search the Golf Course API with fallback strategies for fuzzy matches.

    The API does substring-ish matching, so 'Butter Brook Golf Course' doesn't
    match a course stored as 'Butter Brook Golf Club'. We progressively trim
    common suffixes/words until we find something."""
    if not query:
        return []

    # 1. Exact query
    results = _raw_search(query)
    if results:
        return results

    # 2. Strip common golf-suffix words and try again
    stop_suffixes = ("course", "club", "golf", "country", "links", "association", "the")
    words = [w for w in query.strip().split() if w]
    while len(words) > 1:
        last = words[-1].lower().strip(".,!?'\"")
        if last in stop_suffixes:
            words = words[:-1]
            trimmed = " ".join(words)
            results = _raw_search(trimmed)
            if results:
                return results
        else:
            break

    # 3. Try just the first 2 words (often the unique part of the name)
    if len(query.split()) > 2:
        first_two = " ".join(query.split()[:2])
        results = _raw_search(first_two)
        if results:
            return results

    # 4. Last resort: ask Claude for alternative spellings (handles Whisper
    # transcription quirks like 'Butterbrook' vs 'Butter Brook')
    for alt in _get_alternative_spellings(query):
        results = _raw_search(alt)
        if results:
            return results

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
        course = data.get("course", data)
        # Apply any local overrides (nicknames, yardage corrections, notes)
        return _apply_overrides_to_course(course)
    except Exception:
        return None


def find_tee(course: dict, tee_color: Optional[str] = None) -> Optional[dict]:
    """Find a male tee matching the color, or fall back to a sensible default."""
    male_tees = course.get("tees", {}).get("male", [])
    if not male_tees:
        return None
    if tee_color:
        color = tee_color.lower().strip()
        # 1. Exact match (e.g. 'WHITE' matches 'WHITE', not 'WHITE/BLUE')
        for tee in male_tees:
            if tee["tee_name"].lower() == color:
                return tee
        # 2. Tee name STARTS with the color word
        for tee in male_tees:
            if tee["tee_name"].lower().startswith(color):
                return tee
        # 3. Loose substring match (handles combo tees like 'WHITE/BLUE')
        for tee in male_tees:
            if color in tee["tee_name"].lower():
                return tee
    # Default: sort by total yards descending, pick the SECOND longest
    # (longest tees are usually 'tips' / championship; most amateurs play one back)
    sorted_tees = sorted(
        male_tees, key=lambda t: t.get("total_yards") or 0, reverse=True
    )
    if len(sorted_tees) >= 2:
        return sorted_tees[1]
    return sorted_tees[0]


def extract_tee_color(text: str) -> Optional[str]:
    for color in TEE_COLORS:
        if color in text.lower():
            return color
    return None


def detect_and_update_tee(text: str, round_state: dict) -> Optional[dict]:
    """If the player mentions a tee color and a course is already loaded,
    switch to that tee. Returns the new tee dict if changed, otherwise None."""
    course = round_state.get("course")
    current_tee = round_state.get("tee") or {}
    if not course:
        return None
    color = extract_tee_color(text)
    if not color:
        return None
    new_tee = find_tee(course, color)
    if not new_tee:
        return None
    # Only fire an "update" event if it's actually a different tee
    if new_tee.get("tee_name") == current_tee.get("tee_name"):
        return None
    return new_tee


def detect_and_load_course(text: str, current_round_state: dict) -> Optional[dict]:
    """Try to detect and load a course from natural language. Returns one of:
    - None: no course mention detected
    - {"status": "loaded", "course": ..., "tee": ...}: course loaded successfully
    - {"status": "not_found", "query": "..."}: course mentioned but not in API
    """
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
        return {"status": "not_found", "query": result}
    course_data = get_course(courses[0]["id"])
    if not course_data:
        return {"status": "not_found", "query": result}
    tee_color = extract_tee_color(text)
    tee = find_tee(course_data, tee_color)
    if not tee:
        return {"status": "not_found", "query": result}

    return {"status": "loaded", "course": course_data, "tee": tee}


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
    """Mutate round_state with new score and advance the current hole.
    Backfilling an earlier hole (e.g. logging hole 4 while on hole 6) does NOT
    push the cursor forward — the cursor only moves when the new score is at
    or past the current hole."""
    hole_scores = round_state.get("hole_scores") or []
    while len(hole_scores) < hole:
        hole_scores.append(None)
    hole_scores[hole - 1] = score
    round_state["hole_scores"] = hole_scores
    current = round_state.get("current_hole", 1)
    if hole >= current:
        round_state["current_hole"] = hole + 1
    return round_state


def compute_round_status(round_state: dict) -> Optional[str]:
    """Return a natural-language round-status sentence, or None if no scores yet.
    Handles gaps in the scorecard explicitly so Caddy never silently treats a
    skipped hole as if it had been played."""
    hole_scores = round_state.get("hole_scores") or []
    logged = [(i + 1, s) for i, s in enumerate(hole_scores) if s is not None]
    if not logged:
        return None
    par_total = 0
    have_all_pars = True
    total = 0
    for hole_num, score in logged:
        par = get_hole_par(round_state, hole_num)
        if par is None:
            have_all_pars = False
        else:
            par_total += par
        total += score
    logged_set = {h for h, _ in logged}
    max_logged = max(logged_set)
    missing = [h for h in range(1, max_logged + 1) if h not in logged_set]

    if not have_all_pars or par_total == 0:
        base = f"{total} strokes across {len(logged)} holes"
    else:
        vs = total - par_total
        if vs == 0:
            label = "even par"
        elif vs > 0:
            label = f"{vs}-over par"
        else:
            label = f"{abs(vs)}-under par"
        if missing:
            base = f"{label} ({total} strokes across {len(logged)} holes)"
        else:
            base = f"{label} ({total} strokes through {len(logged)} holes)"
    if missing:
        missing_str = ", ".join(str(h) for h in missing)
        base += f" — still need to log hole(s) {missing_str}"
    return base


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
    ]
    if course.get("_override_notes"):
        lines.append(f"Course notes: {course['_override_notes']}")
    lines.append("")
    lines.append("Hole-by-hole yardages:")
    for i, hole in enumerate(tee.get("holes", []), 1):
        nickname = hole.get("nickname")
        nick_str = f' ("{nickname}")' if nickname else ""
        line = f"  Hole {i}{nick_str}: Par {hole.get('par')}, {hole.get('yardage')} yards (HCP {hole.get('handicap')})"
        if hole.get("notes"):
            line += f" — {hole['notes']}"
        lines.append(line)
    if any(h.get("nickname") for h in tee.get("holes", [])):
        lines.append("")
        lines.append("This course has nicknames for each hole. Use them naturally when referring to holes (e.g. 'You're on Eternity now, the par 5 at the end of the front nine').")
    return "\n".join(lines)


def format_score_context(round_state: dict) -> str:
    hole_scores = round_state.get("hole_scores") or []
    logged = [(i + 1, s) for i, s in enumerate(hole_scores) if s is not None]
    if not logged:
        return ""
    lines = ["\n=== CURRENT SCORECARD ==="]
    for hole_num, score in logged:
        par = get_hole_par(round_state, hole_num)
        if par:
            diff = score - par
            label = {-2: "eagle", -1: "birdie", 0: "par", 1: "bogey", 2: "double", 3: "triple"}.get(diff, f"+{diff}" if diff > 0 else str(diff))
            lines.append(f"  Hole {hole_num} (par {par}): {score} — {label}")
        else:
            lines.append(f"  Hole {hole_num}: {score}")
    status = compute_round_status(round_state)
    if status:
        lines.append("")
        lines.append(f"ROUND STATUS: {status}")
        lines.append("When the player asks about their score or you acknowledge a result, use this exact status — do not recompute it.")
    logged_set = {h for h, _ in logged}
    max_logged = max(logged_set)
    lines.append(f"Current hole: {round_state.get('current_hole', max_logged + 1)}")
    return "\n".join(lines)
