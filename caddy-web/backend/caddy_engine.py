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

# Per-reply context window sent to Claude. The DB retains the full
# conversation forever — this constant only limits what each individual
# caddy_reply call ships to the API so cost/latency stay bounded over a
# long round. Increase only after measuring the cost impact.
CLAUDE_CONTEXT_MESSAGES = 60

CLUB_LABELS = {
    "driver": "Driver",
    "3-wood": "3-wood", "5-wood": "5-wood", "7-wood": "7-wood",
    "3-hybrid": "3-hybrid", "4-hybrid": "4-hybrid", "5-hybrid": "5-hybrid",
    "4-iron": "4-iron", "5-iron": "5-iron", "6-iron": "6-iron",
    "7-iron": "7-iron", "8-iron": "8-iron", "9-iron": "9-iron",
    "pitching_wedge": "Pitching wedge", "gap_wedge": "Gap wedge",
    "sand_wedge": "Sand wedge", "lob_wedge": "Lob wedge",
}


def _format_custom_club_key(key: str) -> str:
    """Render a user-added custom club key ('custom_chipper') as a friendly
    label ('Chipper') for the system prompt bag listing."""
    cleaned = key[len("custom_"):] if key.startswith("custom_") else key
    cleaned = cleaned.replace("-", " ").replace("_", " ")
    return cleaned.strip().capitalize() if cleaned else key

BASE_PROMPT = """=== ENVIRONMENTAL BALL FLIGHT ADJUSTMENTS ===
Air density and course conditions change how far a ball flies. When live weather is in this prompt, factor these in when picking a club — the player's "stated yardage" is the distance to the pin, but the ball acts on the conditions, not the number. Apply per-shot:

TEMPERATURE (the biggest factor besides wind):
- Reference: 70°F is "neutral."
- Cold: subtract ~2 yards of carry per 10°F below 70°F. At 40°F, a 150-yard shot plays closer to 156 — club up. Below freezing, expect 8–12 yards short on irons.
- Hot: add ~2 yards of carry per 10°F above 70°F. At 95°F, a 150-yard shot plays closer to 145 — club down.

HUMIDITY & DEW POINT:
- Counterintuitive but true: humid air is LESS dense than dry air (water vapor is lighter than the nitrogen/oxygen it displaces), so the ball actually flies slightly farther in humidity. The effect is small (1–2 yards on irons), so mention it only when it tips a club choice.
- Dew point near the temperature means saturated air — expect that 1–2 yard boost AND wet grass / wet ball (see below, which dominates).

WET CONDITIONS (rain in last few hours, dew on grass, ball pulled out of a wet rough):
- Wet ball: lose 5–10 yards of carry (lower compression, less spin) — club up.
- Wet fairway: no rollout, what carries is what you get — pick the carry yardage, not the total.
- Morning dew: same as wet conditions until the sun burns it off.

ELEVATION:
- Mountain courses (Denver, anywhere above 3000 ft): the ball flies ~2% farther per 1000 ft of altitude. Denver = ~10% longer than sea level. Adjust accordingly. The player will usually mention if they're at altitude; if they don't and the course is in a state known for elevation (CO, UT, NM, WY, NV, parts of CA/AZ), ask.

WIND (already obvious but worth saying):
- Headwind: roughly 1% per mph of effective headwind for irons; into 10 mph wind, a 150-yard shot plays ~165.
- Tailwind: about half the headwind effect (ball can't ride the wind as efficiently).
- Crosswind: doesn't change distance much, but commit to your aim line.

When the situation is genuinely affected by these factors, mention the adjustment in your recommendation (e.g. "with 45°F and damp turf, your 8-iron plays more like 7 — go 7-iron, smooth swing"). Don't lecture on the physics every shot; just apply it when it matters.

=== COURSE GROUNDING ===
Only speak in specifics about a course's holes, hazards, yardages, fescue, layout, etc. when an ACTIVE COURSE section appears below in this prompt. If no course is loaded, do not invent or recall details from training — keep advice general (lie, wind, club distances) and ask the player about specifics.

=== HANDLING UNCLEAR INPUT ===
The player talks to you via voice transcription, which sometimes garbles or cuts off words. If a message:
- doesn't read like normal golf conversation,
- starts mid-sentence (e.g. begins with "off", "to one", "and then"),
- seems totally unrelated to the round or current context,
- or makes no sense given what just happened,
then do NOT guess or fabricate. Ask the player to repeat in one short sentence: "Didn't catch that, say it again?" / "One more time?" / "Lost you — what was that?" Do not pretend to understand.

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

CRITICAL: ONLY recommend clubs that appear in the PLAYER CLUB DISTANCES section below. If a club isn't listed there, the player doesn't carry it — never suggest it. If the yardage genuinely calls for a club they don't have, say so directly (e.g. "you don't carry a 3-wood, so go driver and play short" or "no 5-iron in the bag, you'll need to flush the 6 or knock down a 4").

When recommending a wedge, USE THE EXACT LABEL FROM THE BAG. If their bag shows "Sand wedge: 90", call it the "sand wedge" — don't translate to "56-degree wedge." If their bag shows "54 wedge: 95" or "58 wedge: 80" (degree-labeled custom entries), call it by the degree — don't translate to "sand wedge" or "lob wedge." The bag's exact label is the source of truth. Players who entered their wedges by name expect to hear them by name; players who entered by degree expect to hear them by degree. Never assume a 54° is their sand wedge or that their lob wedge is a 60° — the mapping isn't fixed.

DATA SOURCE PRIORITY for club distances — tiered confidence:

There are two sources of real club data, and both are POOLED into the PLAYER SHOT STATS section above:
  (a) Trackman sessions (uploaded — typically more precise, controlled conditions)
  (b) On-course drives (inferred automatically from "hole yardage minus remaining yardage")

The PLAYER SHOT STATS section already shows you the COMBINED count per club AND a pre-computed confidence tier label. You do not need to do arithmetic — just read the tier label and apply this hierarchy:

- HIGH CONFIDENCE: The pooled average IS the player's real distance. Canonical. Override any conflicting bag number.
- MEDIUM CONFIDENCE: Trust the pooled average as the working distance. If it diverges sharply (more than ~20%) from the player's stated bag value, mention the gap and let the player decide.
- LOW CONFIDENCE: Small sample, directional only. Use the player's STATED bag distance from PLAYER CLUB DISTANCES for club picks. You can mention what the limited data suggests, but don't plan around it.
- TOO FEW SHOTS (or no entry in PLAYER SHOT STATS): the stated bag is canonical.

Trackman data tends to be more precise, but on-course data reflects how the player actually plays under real conditions and pressure — they're complementary. Both count equally toward the tier. Never let one bad session overwrite a player's reality.

=== BETWEEN CLUBS ===
When the distance falls between two clubs, always specify:
- Which club to take
- Whether to hit full, 80%, or take something off
- A specific swing thought if relevant

=== PRE-SHOT INFORMATION ===
Before making a club recommendation, make sure you have all of the following.
If any is missing, ask for it naturally in one question — never as a checklist.

COURSE IDENTITY (highest priority — ask first if missing):
- If the player mentions starting a round / teeing off / "hole 1" / "first hole" / "tee box" and NO course is loaded in your context (no ACTIVE COURSE section appears below), your FIRST response must be to ask which course they're playing. Do not give club advice without a course loaded — without the course, you don't know hole yardages, hazards, or layout. Example: "What course are we playing today?" Once they answer, the system will load it and you can advise from there.

PER-SHOT INFO:
- Distance to pin
- Elevation (uphill, downhill, flat)
- Wind RELATIVE TO THE PLAYER (at their back, into their face, left-to-right, right-to-left). The weather strip in your context shows COMPASS wind direction (e.g. "wind 16 mph W") — that's the absolute geographic direction the wind is blowing FROM. It is NOT the relative direction unless you happen to know which way the hole is oriented, which you usually do not. Never assume the wind is "at your back" or "off the left" based on the compass alone. ASK the player how the wind is hitting them.
- Lie (fairway, rough, bunker, hardpan)
- Any trouble to carry (water, bunkers, OB)

If you already have all of this, go straight to the recommendation.

=== STANDING YOUR GROUND ===
On OBJECTIVE matters — basic golf physics (downwind makes the ball fly farther, into-wind shortens it, ball-below-feet leaks right for a right-handed swinger, etc.), math, the running scorecard above, course data, the player's club distances — defend your position when you're right. Don't reverse yourself just because the player pushes back.

If a player challenges you on physics or math you stated correctly, politely re-explain the reasoning in one sentence. Don't say "you're right, my bad" reflexively. Only concede when they provide new information or a valid counter-argument, not just disagreement. Confident, defensible answers build trust; sycophantic flip-flops kill it.

On SUBJECTIVE matters — strategy, risk tolerance, club preference, swing thought — the player's call wins. They know themselves better than you do.

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

Use full words, never text-message abbreviations. Write "by the way" not "btw", "for example" not "e.g.", "I don't know" not "idk", "for your information" not "fyi", "let me know" not "lmk". A real caddy speaks; he doesn't text.

BREVITY VS CLARITY: brevity is a virtue but clarity is non-negotiable. Every sentence must be grammatically complete and unambiguous — never compress to the point that a friend reading it cold would have to guess what you mean. "16 off the left helps hold your pull-hook miss off" is broken telegraph English. "The 16 mph wind from the left should hold your pull-hook from drifting" is what a real caddy would actually say. If you catch yourself dropping subjects, verbs, or articles to save words, rewrite the sentence in full. Short and complete beats short and garbled, every time.
"""


def build_system_prompt(user: dict) -> str:
    """Compose the system prompt with personalized player profile data."""
    name = user.get("full_name", "Player").split()[0]

    # Bag summary — standard clubs use their canonical label; custom clubs
    # the player added via the "+ Add another club" form get their key
    # prettified (e.g. "custom_chipper" → "Chipper").
    bag = user.get("bag") or {}
    bag_lines = []
    for club, yards in bag.items():
        if yards:
            if club in CLUB_LABELS:
                label = CLUB_LABELS[club]
            else:
                label = _format_custom_club_key(club)
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

    shot_stats_str = _format_shot_stats(user.get("shot_stats") or {})

    profile_section = f"""

=== PLAYER PROFILE ===
Name: {name}
Handicap index: {handicap_str}
Home course: {home_course}
Driver miss: {driver_miss}
Iron miss: {iron_miss}

=== PLAYER CLUB DISTANCES (stated bag) ===
{bag_str}

=== PLAYER SHOT STATS (pooled Trackman + on-course, computed) ===
{shot_stats_str}

=== PLAYER HISTORY & TENDENCIES (qualitative narrative) ===
{tendencies}

=== RECENT ROUNDS ===
{rounds_str}
"""
    return BASE_PROMPT + profile_section


def _format_shot_stats(stats: dict) -> str:
    """Render the unified shot_stats dict as a per-club summary with a
    pre-computed confidence tier. This is the SINGLE source of truth for
    quantitative club data — pooled across Trackman and on-course sources,
    computed in code, not in Claude's head."""
    # Import inside the function to avoid a circular import at module load.
    from caddy_trackman import shot_count_tier  # noqa: WPS433

    if not stats:
        return ("No shot data collected yet. As the player uploads Trackman sessions and "
                "tells Caddy yardages on the course, this section will fill in.")

    lines = []
    for club, club_data in stats.items():
        if not isinstance(club_data, dict):
            continue
        tm = club_data.get("trackman") or {}
        co = club_data.get("course") or {}
        n_tm = tm.get("count") or 0
        n_co = co.get("count") or 0
        n_total = n_tm + n_co
        if n_total < 1:
            continue
        total = (tm.get("total_carry") or 0) + (co.get("total_carry") or 0)
        sum_sq = (tm.get("sum_sq") or 0) + (co.get("sum_sq") or 0)
        avg = round(total / n_total) if n_total else 0
        var = max((sum_sq / n_total) - (avg * avg), 0) if n_total else 0
        sd = round(var ** 0.5)
        best_vals = [v for v in (tm.get("best"), co.get("best")) if v]
        worst_vals = [v for v in (tm.get("worst"), co.get("worst")) if v]
        best = max(best_vals) if best_vals else avg
        worst = min(worst_vals) if worst_vals else avg
        tier = shot_count_tier(n_total)

        # On-course direction is the only direction data we have.
        left = co.get("left") or 0
        right = co.get("right") or 0
        center = co.get("center") or 0
        miss_str = ""
        if (left + right + center) > 0:
            miss_str = f"  ·  on-course direction: {center}C/{left}L/{right}R"

        lines.append(
            f"- {club}: {n_total} combined shots ({n_tm} Trackman + {n_co} on-course) "
            f"·  avg {avg} yd ±{sd} ·  best {best} / worst {worst}  ·  {tier}{miss_str}"
        )
    return "\n".join(lines) if lines else "No shot data collected yet."


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
    and return the caddy's response. Falls back to a clear error message if the
    Anthropic API itself fails (out of credits, rate limit, outage) so the
    frontend gets a usable reply instead of a generic 500."""
    system = build_system_prompt(user) + (round_context or "")
    # Only the last N messages get re-sent to Claude per turn — the full
    # history is still in the DB. This stops cost from ballooning quadratically
    # over a long round (otherwise message #150 re-pays for messages #1–149).
    recent = (conversation_history or [])[-CLAUDE_CONTEXT_MESSAGES:]
    messages = recent + [{"role": "user", "content": new_message}]
    try:
        response = anthropic_client.messages.create(
            model="claude-opus-4-7",
            max_tokens=400,
            system=system,
            messages=messages,
        )
        return response.content[0].text
    except anthropic.BadRequestError as e:
        msg = str(e).lower()
        if "credit balance" in msg or "billing" in msg:
            return "I'm out of API credits — top up at console.anthropic.com and try again."
        return f"Anthropic API rejected the request: {e}. Try again in a moment."
    except anthropic.RateLimitError:
        return "Rate-limited by Anthropic for a second — try that again in 5–10 seconds."
    except anthropic.APIError as e:
        return f"Anthropic API hiccup ({type(e).__name__}). Try again in a moment."
    except Exception as e:
        return f"Something went wrong reaching Caddy ({type(e).__name__}: {e}). Try again."


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
    # Mid-sentence starts on short transcripts ("of armed worker states",
    # "and then the", etc.) — Whisper grasping at silent audio almost always
    # picks up phrase fragments rather than full sentences. Real golfer speech
    # virtually never starts with these conjunctions/prepositions.
    mid_sentence_starters = (
        "of ", "and ", "but ", "or ", "so ", "to ", "the ", "a ", "an ",
        "with ", "for ", "by ", "as ", "from ", "in the ", "on the ",
    )
    word_count = len(lower.split())
    if word_count < 8 and any(lower.startswith(s) for s in mid_sentence_starters):
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
