"""
Caddy intelligence layer — system prompts, Claude reasoning, voice in/out.
Shared by all clients (web for now, native later).
"""
import base64
import io
import json
import os
import re
from typing import Optional

import anthropic
from openai import OpenAI

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")

if not ANTHROPIC_API_KEY:
    raise RuntimeError("ANTHROPIC_API_KEY environment variable is not set")
if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY environment variable is not set")

anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
openai_client = OpenAI(api_key=OPENAI_API_KEY)

CLUB_LABELS = {
    "driver": "Driver", "3-wood": "3-wood", "5-wood": "5-wood",
    "4-iron": "4-iron", "5-iron": "5-iron", "6-iron": "6-iron",
    "7-iron": "7-iron", "8-iron": "8-iron", "9-iron": "9-iron",
    "pitching_wedge": "Pitching wedge", "gap_wedge": "Gap wedge",
    "sand_wedge": "Sand wedge", "lob_wedge": "Lob wedge",
}

BASE_PROMPT = """=== COURSE GROUNDING ===
Only speak in specifics about a course's holes, hazards, yardages, fescue, layout, etc. when an ACTIVE COURSE section appears below in this prompt. If no course is loaded, do not invent or recall details from training — keep advice general (lie, wind, club distances) and ask the player about specifics.

=== CADDY PERSONALITY ===
You are an expert golf caddy with PGA Tour experience.
You speak like a real caddy — brief, calm, authoritative. Never overly chatty.
Always give one clear club recommendation with a short reason why.
Never make the player feel bad about their swing or tendencies.
Frame all decisions around course management and scoring, not swing flaws.
After giving a club recommendation, stop talking. Do not ask follow-up questions.
Do not check in. Wait silently for the player to speak next.
Always sanity check your recommendation against the player's known distances. Never recommend
a club that is physically incapable of reaching the yardage given. If something seems off,
ask the player to confirm the yardage before recommending.

=== BETWEEN CLUBS ===
When the distance falls between two clubs, always specify:
- Which club to take
- Whether to hit full, 80%, or take something off
- A specific swing thought if relevant

=== PRE-SHOT INFORMATION ===
Before making a club recommendation, make sure you have all of the following.
If any is missing, ask for it naturally in one question — never as a checklist.
- Distance to pin
- Elevation (uphill, downhill, flat)
- Wind (speed and direction)
- Lie (fairway, rough, bunker, hardpan)
- Any trouble to carry (water, bunkers, OB)

If you already have all of this, go straight to the recommendation.

=== HANDLING CONTEXT AND EXPLANATIONS ===
The player will sometimes give you context that affects how a shot should be weighed.
Examples: "Loud noise messed up my backswing" / "Cart drove by mid-swing" / "I'm exhausted, played 36 today" / "I was trying to hit a low fade on purpose" / "I slipped at the top of the swing"
When the player gives this kind of context:
- Acknowledge briefly ("Yeah, that one doesn't count" / "Reset, no worries")
- Do NOT factor that shot into their pattern of tendencies
- Do NOT use that shot as evidence of a swing problem when recommending the next club
- If the explanation reflects a state (tired, played a lot today), DO factor it into upcoming recommendations — favor higher-percentage plays
- If they're attempting something on purpose, respect their intent

=== SHOT RESULT LOGGING ===
After giving a club recommendation, the player may report what happened.
When you recognize a shot result:
- Respond in one or two sentences maximum
- Match tone to outcome — brief encouragement for mishits, affirmation for good shots
- Do NOT ask follow-up questions about the result
- Do NOT immediately prompt for the next shot — wait for the player

=== SCORE TRACKING ===
The player's live scorecard appears in this prompt below when scores have been logged.
When the player reports a hole score, react briefly to THAT hole only — short, conversational, tone matched to the result:
- Par: "Nice par." / "Solid par." / "Good 4."
- Birdie: "Great birdie." / "Way to roll one in."
- Eagle/Albatross: "Eagle — clutch." / "Are you kidding? Albatross."
- Bogey: "Onto the next." / "Shake it off."
- Double or worse: "Tough hole, forget it." / "Reset on the tee."

Do NOT recite the running total or score-vs-par on every hole — that gets repetitive. The scorecard above has the running total for when the player explicitly asks ("where am I?", "what am I shooting?", etc.). Otherwise just acknowledge the hole and let the round breathe.

=== COURSE MANAGEMENT ===
Adjust risk tolerance based on the situation:
- Scoring goals: protect the score above all else if mentioned
- Competition vs. casual: more aggressive in casual rounds
- Position in round: tighten up on the back nine, never make a double-bogey hole in the final 3
- Player confidence today: factor it in — playing well allows aggressive plays, struggling means safer choices

Keep responses brief and natural — like a real caddy walking next to the player. Never use bullet points or headers in your responses. Speak in flowing sentences.
"""


def build_system_prompt(user: dict) -> str:
    """Compose the system prompt with personalized player profile data."""
    name = user.get("full_name", "Player").split()[0]

    # Bag summary
    bag = user.get("bag") or {}
    bag_lines = []
    for club, yards in bag.items():
        if yards:
            label = CLUB_LABELS.get(club, club)
            bag_lines.append(f"{label}: {yards} yards")
    bag_str = "\n".join(bag_lines) if bag_lines else "Not yet entered"

    driver_miss = user.get("driver_miss") or "unknown"
    iron_miss = user.get("iron_miss") or "unknown"
    home_course = user.get("home_course") or "unknown"
    handicap = user.get("handicap_index")
    handicap_str = f"{handicap}" if handicap is not None else "Not yet calculated"
    tendencies = user.get("tendencies_summary") or "No round history yet."

    rounds = user.get("rounds") or []
    if rounds:
        last_three = sorted(rounds, key=lambda r: r.get("date", ""), reverse=True)[:3]
        rounds_str = "\n".join(
            f"- {r.get('course','?')} ({r.get('date','?')}): {r.get('score','?')}"
            for r in last_three
        )
    else:
        rounds_str = "No rounds logged."

    profile_section = f"""

=== PLAYER PROFILE ===
Name: {name}
Handicap index: {handicap_str}
Home course: {home_course}
Driver miss: {driver_miss}
Iron miss: {iron_miss}

=== PLAYER CLUB DISTANCES ===
{bag_str}

=== PLAYER HISTORY & TENDENCIES ===
{tendencies}

=== RECENT ROUNDS ===
{rounds_str}
"""
    return BASE_PROMPT + profile_section


def _extract_json(text: str):
    """Strip markdown code fences and parse JSON. Returns None on failure."""
    if not text:
        return None
    cleaned = text.strip()
    if cleaned.startswith("```"):
        first_nl = cleaned.find("\n")
        if first_nl != -1:
            cleaned = cleaned[first_nl + 1:]
        if cleaned.rstrip().endswith("```"):
            cleaned = cleaned.rstrip()[:-3]
    cleaned = cleaned.strip()
    if not (cleaned.startswith("{") or cleaned.startswith("[")):
        match = re.search(r"[\{\[].*[\}\]]", cleaned, re.DOTALL)
        if match:
            cleaned = match.group(0)
    try:
        return json.loads(cleaned)
    except Exception:
        return None


def extract_scorecard_from_image(image_bytes: bytes, media_type: str = "image/jpeg") -> Optional[dict]:
    """Use Claude vision to extract course and hole data from a scorecard photo.
    Returns structured dict or None if this isn't a recognizable scorecard."""
    print(f"[scorecard] media_type={media_type} size={len(image_bytes)}b")
    try:
        b64 = base64.b64encode(image_bytes).decode()
        response = anthropic_client.messages.create(
            model="claude-opus-4-7",
            max_tokens=4096,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {"type": "base64", "media_type": media_type, "data": b64},
                    },
                    {
                        "type": "text",
                        "text": (
                            "Look at this image. If it is a golf scorecard, extract the data and return ONLY valid JSON:\n"
                            '{\n'
                            '  "course_name": "Full course name as printed on the scorecard",\n'
                            '  "city": "City or null",\n'
                            '  "state": "State abbreviation or null",\n'
                            '  "tees": [\n'
                            '    {\n'
                            '      "tee_name": "COLOR in ALL CAPS (e.g. BLACK, BLUE, WHITE, RED)",\n'
                            '      "course_rating": 71.2,\n'
                            '      "slope_rating": 128,\n'
                            '      "total_yards": 6400,\n'
                            '      "holes": [\n'
                            '        {"hole": 1, "par": 4, "yardage": 385, "handicap": 7},\n'
                            '        ... all 18 holes ...\n'
                            '      ]\n'
                            '    }\n'
                            '  ]\n'
                            '}\n'
                            "Include every tee color visible on the scorecard. Use null for fields not shown.\n"
                            'If this is NOT a golf scorecard, return {"error": "not a scorecard"}.'
                        ),
                    },
                ],
            }],
        )
        raw = response.content[0].text
        print(f"[scorecard] Claude response: {raw[:500]}")
        data = _extract_json(raw)
        if not data or data.get("error") or not data.get("tees"):
            print(f"[scorecard] extraction failed — parsed: {data}")
            return None
        if not data.get("course_name"):
            data["course_name"] = "Unknown Course"
        print(f"[scorecard] extracted: {data.get('course_name')} — {len(data.get('tees', []))} tee(s)")
        return data
    except Exception as e:
        print(f"[scorecard] API error: {e}")
        return None


def caddy_reply(user: dict, conversation_history: list[dict], new_message: str,
                round_context: str = "") -> str:
    """Send the latest user message to Claude with full context (player + active round)
    and return the caddy's response."""
    system = build_system_prompt(user) + (round_context or "")
    messages = conversation_history + [{"role": "user", "content": new_message}]
    response = anthropic_client.messages.create(
        model="claude-opus-4-7",
        max_tokens=400,
        system=system,
        messages=messages,
    )
    return response.content[0].text


# Short strings that are only hallucinations when they ARE the entire transcript
_EXACT_HALLUCINATIONS = {"you", ".", "..", "...", "um", "uh", "hmm", "hm", "thank you", "thanks"}

# Longer phrases — safe to match as substrings since they won't appear in real golf talk
_SUBSTRING_HALLUCINATIONS = [
    "if you like my video", "please subscribe", "like and subscribe",
    "thanks for watching", "thank you for watching", "don't forget to subscribe",
    "see you in the next video", "hit the like button", "subscribe to my channel",
    "smash that like button", "ring the bell", "see you next time",
    "thanks for listening", "let me know in the comments",
    "チャンネル登録をお願いします", "ご視聴ありがとうございました",
    "登録お願いします", "高評価",
    "구독", "suscríbete", "abonnez-vous", "abonniert",
]


def is_likely_hallucination(text: str) -> bool:
    """Detect transcripts that are almost certainly Whisper noise/hallucination."""
    if not text or not text.strip():
        return True
    lower = text.lower().strip().rstrip(".!?,。")
    if not lower:
        return True
    # Short strings: only block when the whole transcript matches
    if lower in _EXACT_HALLUCINATIONS:
        return True
    # Longer phrases: block if they appear anywhere in the transcript
    for h in _SUBSTRING_HALLUCINATIONS:
        if h in lower:
            return True
    # Mostly non-ASCII (likely a foreign-script hallucination from noise)
    non_ascii = sum(1 for c in text if ord(c) > 127)
    if non_ascii / max(len(text), 1) > 0.5:
        return True
    return False


def transcribe_audio(audio_bytes: bytes, filename: str = "audio.webm") -> str:
    """Send audio to Whisper, return transcript. Returns empty string for likely hallucinations."""
    audio_file = io.BytesIO(audio_bytes)
    audio_file.name = filename
    transcript = openai_client.audio.transcriptions.create(
        model="whisper-1",
        file=audio_file,
    )
    text = transcript.text.strip()
    if is_likely_hallucination(text):
        return ""
    return text


def synthesize_speech(text: str) -> bytes:
    """Generate spoken audio (MP3 bytes) for the given text."""
    response = openai_client.audio.speech.create(
        model="tts-1",
        voice="onyx",
        input=text,
    )
    return response.content
