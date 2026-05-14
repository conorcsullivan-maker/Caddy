import random
import json
import anthropic
import speech_recognition as sr
import subprocess
import tempfile
import os
import time
import struct
import math
from datetime import date
from openai import OpenAI
from dotenv import load_dotenv
load_dotenv(override=True)
from caddy_auth import login, run_onboarding, build_system_prompt, save_profile
from caddy_course import search_course, get_course, find_tee, extract_tee_color, format_course_for_prompt

_anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
_openai_key = os.environ.get("OPENAI_API_KEY")
if not _anthropic_key or not _openai_key:
    raise RuntimeError("Set ANTHROPIC_API_KEY and OPENAI_API_KEY environment variables before running.")
anthropic_client = anthropic.Anthropic(api_key=_anthropic_key)
openai_client = OpenAI(api_key=_openai_key)

BASE_PROMPT = """=== CADDY PERSONALITY ===
You are an expert golf caddy with PGA Tour experience.
You speak like a real caddy - brief, calm, authoritative. Never overly chatty.
Always give one clear club recommendation with a short reason why.
Never make the player feel bad about their swing or tendencies.
Frame all decisions around course management and scoring, not swing flaws.
After giving a club recommendation, stop talking. Do not ask follow-up questions.
Do not ask how the shot went. Do not check in. Wait silently for the player to speak next.
Always sanity check your recommendation against the player's known distances. Never recommend
a club that is physically incapable of reaching the yardage given. If something seems off,
ask the player to confirm the yardage before recommending.

=== BETWEEN CLUBS ===
When the distance falls between two clubs, always specify:
- Which club to take
- Whether to hit full, 80%, or take something off
- A specific swing thought if relevant
Example: "Take the 7-iron and smooth it at 85% - you don't want to be long here"
Example: "Full 8-iron, perfect yardage, just commit to it"
Example: "Take the 6 and choke down an inch, nice easy swing"

=== PRE-SHOT INFORMATION ===
Before making a club recommendation make sure you have all of the following.
If any is missing, ask for it naturally in one question - never as a checklist.
Do not recommend a club until you have:
- Distance to pin
- Elevation (uphill, downhill, or flat)
- Wind (speed and direction)
- Lie (fairway, rough, bunker, hardpan)
- Any trouble to carry (water, bunkers, OB)

If you already have all of this, go straight to the recommendation.
Ask naturally like a real caddy would.
Example: "What's the wind doing?" or "Are you above or below the pin?"

=== COURSE MANAGEMENT RULES ===
Adjust tone and risk tolerance based on the situation:

Scoring goals:
- If player mentions a scoring target (breaking 80, 90, etc.) protect that score above all else
- Conservative gets more conservative as the target gets closer

Competition vs casual:
- Casual round: can be more aggressive, encourage attacking pins
- Competition or money on the line: favor the safe play, take bunkers and water out of play

Position in round:
- Early holes: slightly more aggressive, mistakes can be recovered
- Back nine: tighten up, course management over ego
- Final 3 holes: protect the score, never make a double bogey hole

Player confidence that day:
- If player is playing well, factor that in and allow more aggressive plays
- If player has mentioned struggling, favor higher percentage shots
- Always read how the player is feeling before recommending a risky play

=== SCORE TRACKING ===
The player's live scorecard will appear in this prompt below when scores have been logged.
When the player reports a hole score — whether exact ("I shot a 5") or relative ("birdie", "bogey", "double") — acknowledge it in one natural sentence and mention their running total vs par.
Example: "Birdie on 5. Two under through five."
Example: "Bogey on 12. You're sitting at plus three for the round."
If the player asks "what's my score?" or "where do I stand?", give a clear summary.
Never ask the player to confirm the score — just log it and move on.

=== COURSE TRACKING ===
When course data is loaded it will appear in this prompt below.
If no course data is loaded and you have given 2 or more club recommendations, ask naturally:
"By the way, what course are you at? I'll log it in your history."
Ask this only once — do not repeat it.
If course data IS loaded, use it actively:
- Reference hole yardage to infer how far the player hit when they report remaining distance
- Use the course rating and slope automatically — never ask for them
- Factor in the hole's handicap rating when advising risk tolerance

=== HANDLING CONTEXT AND EXPLANATIONS ===
The player will sometimes give you context that affects how a shot should be weighed.
Examples:
- "Loud noise messed up my backswing"
- "Cart drove by mid-swing"
- "I'm exhausted, played 36 today"
- "I was trying to hit a low fade on purpose"
- "I slipped at the top of the swing"
- "That was just a warmup swing, doesn't count"
- "I was distracted, my buddy was talking"

When the player gives this kind of context, treat it the way a real caddy would:
- Acknowledge briefly ("Yeah, that one doesn't count" / "Reset, no worries")
- Do NOT factor that shot into their pattern of tendencies
- Do NOT use that shot as evidence of a swing problem when recommending the next club
- If the explanation reflects a state (tired, played a lot today), DO factor that state into upcoming recommendations — favor higher-percentage plays, less risk
- If they're attempting something on purpose (intentional shape, deliberate club-up), respect their intent

The player's explanations are part of the data. A great caddy hears "loud noise threw me off"
and immediately resets. A bad caddy treats that miss as a swing flaw.

=== SHOT RESULT LOGGING ===
After giving a club recommendation, the player may return and report what happened.
Recognize shot results by phrases like: "hit it", "that went", "came up", "pulled it",
"pushed it", "flushed it", "chunked it", "thin", "fat", "in the bunker", "on the green",
"made the putt", "missed it", "great shot", "bad shot", "short", "long", "in the rough", etc.
When you recognize a shot result:
- Respond in one or two sentences maximum
- Match tone to outcome — brief encouragement for mishits, affirmation for good shots
- End naturally in a way that signals you've registered it
- Do NOT ask follow-up questions about the result
- Do NOT immediately prompt for the next shot — wait for the player to initiate
Example responses:
"Tough break. Noted."
"Flushed it — that's the swing right there."
"We'll factor that left miss in going forward."
"Good par save. Logged."
"Wind got it. We'll play for that next time."
"""

WHISPER_HALLUCINATIONS = [
    "if you like my video", "please subscribe", "like and subscribe",
    "thanks for watching", "thank you for watching", "don't forget to subscribe",
    "see you in the next video", "hit the like button"
]

END_ROUND_PHRASES = [
    "round complete", "round is complete", "we're finished", "we are finished",
    "round's done", "round is done", "that's a wrap", "end of round",
    "finished the round", "that's the round", "i'm done for the day",
    "we're done for the day"
]

TRIGGER_PHRASES = [
    "what club should i hit", "which club should i hit", "what club do i hit",
    "what club should i use", "what club do you recommend", "what club do you think",
    "what club are we hitting", "what club am i hitting",
    "what should i hit", "what do i hit", "what do i use", "what do i take",
    "what should i use", "what should i take", "what should i pull",
    "what do you think", "what do you think i should hit", "what do you think i should use",
    "what would you hit", "what would you do", "what would you recommend",
    "what are you thinking", "what are we thinking",
    "what's the play", "what's the call", "what's the move", "what's the number",
    "what's your call", "what's your recommendation", "what's your thought",
    "what's the club", "what's my club",
    "which club", "which iron", "which one do i hit", "which one should i hit",
    "give me a club", "give me a number", "give me your recommendation",
    "help me out", "lay it on me", "what do you got", "what do you suggest",
    "your call", "your thoughts", "what do you recommend",
    "what am i hitting", "what are we hitting",
]

# --- Login ---
profile = None
while not profile:
    profile = login()
    if not profile:
        print("Please try again.\n")

if not profile.get("onboarded"):
    profile = run_onboarding(profile)

first_name = profile["name"].split()[0]
CADDY_PROMPT = build_system_prompt(profile, BASE_PROMPT)

print("\nSelect mode:")
print("  play  — club recommendations only")
print("  train — recommendations + shot logging\n")
mode_input = input("Mode (play/train): ").strip().lower()
train_mode = mode_input in ("train", "t", "2")

if train_mode:
    CADDY_PROMPT += """

=== TRAIN MODE ACTIVE ===
You are in Train Mode today. Your job is both to recommend clubs AND to actively build the player's profile.
After every recommendation, the player will report back what happened. When they do:
- Acknowledge in one or two sentences — brief, natural, caddy-like
- Match tone to outcome: encouraging for mishits, affirming for good shots
- Signal that it's been registered without sounding robotic
- Use what you're learning throughout the round to sharpen your recommendations
The player will log on their own pace — never prompt them. Just be ready to receive and acknowledge.
"""

conversation_history = []
current_course = None
course_context = ""
hole_scores = []
current_hole = 1

recognizer = sr.Recognizer()
recognizer.pause_threshold = 2.5
mic = sr.Microphone()


def get_hole_par(hole_number):
    if not current_course:
        return None
    tee = find_tee(current_course)
    if tee and len(tee.get("holes", [])) >= hole_number:
        return tee["holes"][hole_number - 1]["par"]
    return None


def score_state_string():
    logged = [(i + 1, s) for i, s in enumerate(hole_scores) if s is not None]
    if not logged:
        return ""
    total = sum(s for _, s in logged)
    lines = ["\n=== CURRENT SCORECARD ==="]
    par_total = 0
    for hole_num, score in logged:
        par = get_hole_par(hole_num)
        if par:
            par_total += par
            diff = score - par
            label = {-2: "eagle", -1: "birdie", 0: "par", 1: "bogey", 2: "double bogey", 3: "triple bogey"}.get(diff, f"+{diff}" if diff > 0 else str(diff))
            lines.append(f"  Hole {hole_num}: {score} ({label})")
        else:
            lines.append(f"  Hole {hole_num}: {score}")
    lines.append(f"Total: {total} through {len(logged)} holes")
    if par_total:
        vs = total - par_total
        lines.append(f"Vs par: {'even' if vs == 0 else ('+' + str(vs) if vs > 0 else str(vs))}")
    lines.append(f"Current hole: {current_hole}")
    return "\n".join(lines)


def detect_and_log_score(text):
    global current_hole, hole_scores
    score_keywords = ["birdie", "eagle", "bogey", "double", "triple", "par",
                      "made par", "got par", "i shot", "i made", "i got a",
                      "hole in one", "ace"]
    if not any(k in text.lower() for k in score_keywords):
        # Also check for "a [number]" patterns loosely
        if not any(f"a {n}" in text.lower() for n in ["two", "three", "four", "five", "six", "seven", "eight", "nine", "2", "3", "4", "5", "6", "7", "8", "9"]):
            return
    par = get_hole_par(current_hole)
    par_info = f"Current hole: {current_hole}, par: {par}." if par else f"Current hole: {current_hole}, par unknown."
    response = anthropic_client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=60,
        messages=[{"role": "user", "content": (
            f'Golfer said: "{text}"\n{par_info}\n'
            'Is the player reporting their score for a hole? '
            'Return JSON only: {"score": integer_or_null, "hole": integer_or_null}\n'
            'birdie=par-1, eagle=par-2, bogey=par+1, double=par+2, triple=par+3, hole in one=1.\n'
            'If not a score report return {"score": null, "hole": null}'
        )}]
    )
    try:
        data = json.loads(response.content[0].text.strip())
        score = data.get("score")
        hole = data.get("hole") or current_hole
        if not score:
            return
        while len(hole_scores) < hole:
            hole_scores.append(None)
        hole_scores[hole - 1] = score
        current_hole = max(hole + 1, current_hole + 1)
    except Exception:
        pass


def detect_and_load_course(text):
    global current_course, course_context
    if current_course:
        return
    course_keywords = ["at ", "playing", "arrived", "tee off", "course", "club", "hole"]
    if not any(k in text.lower() for k in course_keywords):
        return
    response = anthropic_client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=30,
        messages=[{"role": "user", "content": (
            f'Does this text mention a specific golf course or golf club by name? "{text}"\n'
            'If yes, return only the course or club name. If no, return "none".'
        )}]
    )
    result = response.content[0].text.strip()
    if result.lower() in ("none", "no", "") or len(result) < 4:
        return
    courses = search_course(result)
    if not courses:
        return
    course_data = get_course(courses[0]["id"])
    if not course_data:
        return
    tee_color = extract_tee_color(text)
    tee = find_tee(course_data, tee_color)
    if not tee:
        return
    current_course = course_data
    course_context = format_course_for_prompt(course_data, tee)
    club_name = course_data.get("club_name", result)
    confirm = f"Got it — {club_name} loaded, {tee['tee_name']} tees. I've got the full scorecard."
    speak(confirm)
    print(f"Caddy: {confirm}\n")


def speak(text):
    response = openai_client.audio.speech.create(
        model="tts-1",
        voice="onyx",
        input=text
    )
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
        f.write(response.content)
        temp_path = f.name
    subprocess.run(["afplay", temp_path])
    os.unlink(temp_path)


def calculate_handicap(rounds):
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


def parse_score_from_speech(speech):
    response = anthropic_client.messages.create(
        model="claude-opus-4-7",
        max_tokens=150,
        messages=[{"role": "user", "content": (
            f'The golfer said: "{speech}"\n\n'
            "Extract their golf score. Return only a JSON object with:\n"
            '- "total_score": integer or null\n'
            '- "hole_scores": array of integers if read hole by hole, or null\n'
            '- "holes_played": integer (18 unless stated otherwise)\n'
            '- "course_rating": float or null\n'
            '- "slope_rating": integer or null'
        )}]
    )
    try:
        return json.loads(response.content[0].text)
    except Exception:
        return {}


def submit_score(profile):
    logged = [s for s in hole_scores if s is not None]
    total_score = None
    hole_scores_final = None

    if logged:
        total_score = sum(logged)
        hole_scores_final = hole_scores[:]
        speak(f"I've got your scorecard from the round — {total_score} total.")
        print(f"Caddy: I've got your scorecard — {total_score} total.\n")
    else:
        speak("Before we wrap up — want to log your score? Give me your total or read me the card hole by hole. Say skip to move on.")
        print("Caddy: Before we wrap up — want to log your score? Give me your total or read me the card hole by hole.\n")
        score_input = listen(timeout=20)
        if not score_input or any(w in score_input for w in ["skip", "no", "not now", "pass"]):
            return
        print(f"You: {score_input}\n")
        score_data = parse_score_from_speech(score_input)
        total_score = score_data.get("total_score")
        hole_scores_final = score_data.get("hole_scores")

    if not total_score:
        speak("Didn't catch a score — we'll log it next time.")
        print("Caddy: Didn't catch a score — we'll log it next time.\n")
        return

    course_rating = score_data.get("course_rating")
    slope_rating = score_data.get("slope_rating")

    # Use loaded course data if available
    if current_course and (not course_rating or not slope_rating):
        from caddy_course import find_tee
        tee = find_tee(current_course)
        if tee:
            course_rating = course_rating or tee.get("course_rating")
            slope_rating = slope_rating or tee.get("slope_rating")

    if not course_rating or not slope_rating:
        speak("What's the course rating and slope? It's on the scorecard. Say skip if you don't have it.")
        print("Caddy: What's the course rating and slope?\n")
        rating_input = listen(timeout=15)
        if rating_input and "skip" not in rating_input:
            print(f"You: {rating_input}\n")
            rating_data = parse_score_from_speech(rating_input)
            course_rating = rating_data.get("course_rating")
            slope_rating = rating_data.get("slope_rating")

    differential = None
    if course_rating and slope_rating:
        differential = round((total_score - course_rating) * 113 / slope_rating, 1)

    round_record = {
        "date": date.today().strftime("%Y-%m-%d"),
        "course": profile.get("home_course") or "Unknown",
        "score": total_score,
        "holes": len([s for s in hole_scores_final if s is not None]) if hole_scores_final else 18,
        "hole_scores": hole_scores_final,
        "course_rating": course_rating,
        "slope_rating": slope_rating,
        "differential": differential,
    }
    profile["rounds"].append(round_record)

    handicap = calculate_handicap(profile["rounds"])
    if handicap is not None:
        profile["handicap_index"] = handicap

    rounds_count = len(profile["rounds"])
    if handicap is not None:
        msg = f"Shot {total_score} today. Handicap index is now {handicap}, based on your last {min(rounds_count, 20)} rounds."
    else:
        needed = 3 - rounds_count
        msg = f"Shot {total_score} logged. {needed} more round{'s' if needed != 1 else ''} before I can calculate your index."

    speak(msg)
    print(f"Caddy: {msg}\n")


def end_round(profile):
    print("\nGenerating round summary...")
    summary_messages = conversation_history + [{
        "role": "user",
        "content": (
            "The round is complete. Based on everything we discussed and logged today — "
            "shot results, misses, what was working — write a concise updated tendencies "
            "summary for my player profile. Write in second person, factual, under 150 words. "
            "This will inform your recommendations in future rounds."
        )
    }]
    response = anthropic_client.messages.create(
        model="claude-opus-4-7",
        max_tokens=300,
        system=CADDY_PROMPT,
        messages=summary_messages
    )
    summary = response.content[0].text
    profile["tendencies_summary"] = summary

    submit_score(profile)
    save_profile(profile)

    sign_off = "Good round. Profile's been updated — I'll carry everything into the next one."
    speak(sign_off)
    print(f"\nCaddy: {sign_off}\n")
    print(f"--- Tendencies Updated ---\n{summary}\n")


def listen_for_wake():
    recognizer.pause_threshold = 0.6
    while True:
        try:
            with mic as source:
                audio = recognizer.listen(source, timeout=None, phrase_time_limit=4)
            text = recognizer.recognize_google(audio).lower()
            print(f"(wake heard: '{text}')")
            return text
        except KeyboardInterrupt:
            raise
        except Exception:
            time.sleep(0.2)
            continue


def listen(timeout=15):
    recognizer.pause_threshold = 1.2
    audio_chunks = []
    sample_rate = None
    sample_width = None

    with mic as source:
        while True:
            try:
                audio = recognizer.listen(source, timeout=timeout, phrase_time_limit=20)
                audio_chunks.append(audio)
                if sample_rate is None:
                    sample_rate = audio.sample_rate
                    sample_width = audio.sample_width

                try:
                    quick = recognizer.recognize_google(audio).lower()
                    if any(p in quick for p in TRIGGER_PHRASES):
                        break
                except Exception:
                    pass

                timeout = 3

            except sr.WaitTimeoutError:
                break

    if not audio_chunks:
        return ""

    # Chime immediately when recording ends — before Whisper call
    subprocess.Popen(["afplay", "/System/Library/Sounds/Tink.aiff"])

    combined_raw = b"".join(chunk.get_raw_data() for chunk in audio_chunks)

    shorts = struct.unpack(f"{len(combined_raw) // 2}h", combined_raw)
    rms = math.sqrt(sum(s ** 2 for s in shorts) / len(shorts))
    if rms < 300:
        return ""

    combined = sr.AudioData(combined_raw, sample_rate, sample_width)

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        f.write(combined.get_wav_data())
        temp_path = f.name

    try:
        with open(temp_path, "rb") as f:
            transcript = openai_client.audio.transcriptions.create(
                model="whisper-1",
                file=f
            )
        os.unlink(temp_path)
        text = transcript.text.lower().strip()

        if any(h in text for h in WHISPER_HALLUCINATIONS):
            return ""

        return text
    except Exception:
        try:
            os.unlink(temp_path)
        except Exception:
            pass
        return ""


print("Calibrating microphone...")
with mic as source:
    recognizer.adjust_for_ambient_noise(source, duration=1.5)
recognizer.dynamic_energy_threshold = False
print("Done.\n")

if train_mode:
    greeting = f"Train mode active, {first_name}. Every shot gets logged today — let's build that profile. Say Hey Caddy when you're ready."
else:
    greeting = f"Welcome back {first_name}. Say Hey Caddy whenever you're ready."

speak(greeting)
print(f"{greeting}\n")

try:
    while True:
        heard = listen_for_wake()
        if "caddy" not in heard and "caddie" not in heard:
            continue

        wake_responses = [
            "What do you got?",
            "Right here. What's the situation?",
            "Talk to me.",
            "Go ahead.",
            "I'm listening.",
            "What are we working with?",
        ]
        wake_reply = random.choice(wake_responses)
        speak(wake_reply)
        print(f"Caddy: {wake_reply}\n")

        while True:
            user_input = listen()

            if not user_input:
                break

            user_input = user_input.replace("hey caddy", "").replace("hey caddie", "").strip()

            if any(p in user_input for p in END_ROUND_PHRASES):
                end_round(profile)
                break

            # Filter ambient noise. Single-word inputs are normally dropped, except for
            # golf-specific score words (birdie/eagle/bogey/etc) that are nearly always
            # intentional. English-ambiguous words like "par" or "double" require more
            # context to avoid corrupting the scorecard.
            SAFE_SINGLE_SCORE_WORDS = {"birdie", "eagle", "bogey", "ace"}
            words_lower = [w.lower().strip(".,!?") for w in user_input.split()]
            is_safe_score = any(w in SAFE_SINGLE_SCORE_WORDS for w in words_lower)
            if not user_input or (len(words_lower) < 2 and not is_safe_score):
                continue

            print(f"You: {user_input}\n")
            detect_and_load_course(user_input)
            detect_and_log_score(user_input)
            conversation_history.append({"role": "user", "content": user_input})

            response = anthropic_client.messages.create(
                model="claude-opus-4-7",
                max_tokens=300,
                system=CADDY_PROMPT + course_context + score_state_string(),
                messages=conversation_history
            )

            caddy_response = response.content[0].text
            conversation_history.append({"role": "assistant", "content": caddy_response})

            print(f"Caddy: {caddy_response}\n")
            speak(caddy_response)
            time.sleep(0.8)

except KeyboardInterrupt:
    print("\nSee you out there.")
except Exception as e:
    print(f"\nSomething went wrong: {e}")
finally:
    print("Caddy signing off.")
