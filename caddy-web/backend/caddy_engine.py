"""
Caddy intelligence layer — system prompts, Claude reasoning, voice in/out.
Shared by all clients (web for now, native later).
"""
import io
import os
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

BASE_PROMPT = """=== CADDY PERSONALITY ===
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
When the player reports a hole score (exact "I shot a 5" or relative "birdie"), acknowledge in one natural sentence and mention their running total vs par.

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


WHISPER_HALLUCINATIONS = [
    # English YouTube-style sign-offs (Whisper trained on lots of YouTube)
    "if you like my video", "please subscribe", "like and subscribe",
    "thanks for watching", "thank you for watching", "don't forget to subscribe",
    "see you in the next video", "hit the like button", "subscribe to my channel",
    "smash that like button", "ring the bell", "see you next time",
    "thanks for listening", "let me know in the comments",
    # Japanese YouTube hallucinations
    "チャンネル登録をお願いします", "ご視聴ありがとうございました",
    "登録お願いします", "高評価",
    # Korean / Spanish / French / German common ones
    "구독", "suscríbete", "abonnez-vous", "abonniert",
    # Misc filler hallucinations
    "thank you.", "thanks.", "you", ".", "...",
]


def is_likely_hallucination(text: str) -> bool:
    """Detect transcripts that are almost certainly Whisper noise/hallucination."""
    if not text or not text.strip():
        return True
    lower = text.lower().strip().rstrip(".!?,。")
    if not lower:
        return True
    # Exact match or substring match against known hallucinations
    for h in WHISPER_HALLUCINATIONS:
        if h in lower or lower in h:
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
