"""
Trackman ingestion — pulls a player's Trackman session from a web report URL
or a pasted/uploaded CSV, formats it for Claude, and asks Claude to write
(or update) the player's tendencies summary.

Web-app adaptation of the original Mac CLI script. Same parsing logic, but
hooks into the FastAPI backend's user profile instead of local JSON files.
"""
import csv
import io
import os
import re
from typing import Optional

import requests
import anthropic

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None

TRACKMAN_API = "https://golf-player-activities.trackmangolf.com/api/reports/getreport"
TRACKMAN_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json",
    "Origin": "https://web-dynamic-reports.trackmangolf.com",
    "Referer": "https://web-dynamic-reports.trackmangolf.com/",
    "User-Agent": "Mozilla/5.0",
}

# Trackman API returns metric — convert for our prompts
M_TO_YARDS = 1.0936
MPS_TO_MPH = 2.2369

CLUB_LABEL_MAP = {
    "Driver": "Driver", "3Wood": "3-wood", "5Wood": "5-wood", "7Wood": "7-wood",
    "2Iron": "2-iron", "3Iron": "3-iron", "4Iron": "4-iron", "5Iron": "5-iron",
    "6Iron": "6-iron", "7Iron": "7-iron", "8Iron": "8-iron", "9Iron": "9-iron",
    "PitchingWedge": "Pitching wedge", "GapWedge": "Gap wedge",
    "SandWedge": "Sand wedge", "LobWedge": "Lob wedge",
    "Hybrid": "Hybrid", "3Hybrid": "3-hybrid", "4Hybrid": "4-hybrid", "5Hybrid": "5-hybrid",
}

# Tiered shot-count confidence. Trust in a club's Trackman-derived distance
# is a gradient, not a switch. These thresholds count CUMULATIVE shots across
# all uploaded sessions, not just the latest one.
#
#   < SHOT_TIER_SMALL   → ignore. Too few to mean anything.
#   < SHOT_TIER_MEDIUM  → small sample. Note the data but keep using stated bag.
#   < SHOT_TIER_HIGH    → medium confidence. Trust Trackman unless it diverges
#                         wildly from the stated value (then flag it).
#   ≥ SHOT_TIER_HIGH    → high confidence. Trackman number is canonical truth.
SHOT_TIER_SMALL = 10    # below this = too few
SHOT_TIER_MEDIUM = 50   # below this = small sample
SHOT_TIER_HIGH = 250    # at/above this = full confidence


def shot_count_tier(n: int) -> str:
    """Classify a per-club shot count into a confidence tier label."""
    if n >= SHOT_TIER_HIGH:
        return "HIGH CONFIDENCE"
    if n >= SHOT_TIER_MEDIUM:
        return "MEDIUM CONFIDENCE"
    if n >= SHOT_TIER_SMALL:
        return "LOW CONFIDENCE — small sample"
    return "TOO FEW SHOTS"


# ────────────────────────────────────────────────────────────
# URL → report ID → JSON
# ────────────────────────────────────────────────────────────
def extract_report_id(text: str) -> Optional[str]:
    """Pull a Trackman report UUID out of a URL or pasted string."""
    if not text:
        return None
    m = re.search(r"r=([0-9a-fA-F\-]{36})", text)
    if m:
        return m.group(1)
    m = re.search(r"\b([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\b", text)
    if m:
        return m.group(1)
    return None


def resolve_short_url(url: str) -> Optional[str]:
    """Follow Trackman short links and pull the report ID off the resolved URL."""
    try:
        r = requests.get(url, headers=TRACKMAN_HEADERS, allow_redirects=True, timeout=10)
        return extract_report_id(r.url)
    except Exception:
        return None


def fetch_trackman_report(url_or_id: str) -> Optional[dict]:
    """Given a Trackman URL or report ID, return the full session JSON."""
    report_id = extract_report_id(url_or_id) if "-" in url_or_id else url_or_id
    if not report_id and url_or_id.startswith("http"):
        report_id = resolve_short_url(url_or_id)
    if not report_id:
        return None
    try:
        r = requests.post(
            TRACKMAN_API,
            headers=TRACKMAN_HEADERS,
            json={"reportId": report_id},
            timeout=20,
        )
        if r.status_code != 200:
            return None
        return r.json()
    except Exception:
        return None


# ────────────────────────────────────────────────────────────
# Session JSON → per-club summary text for Claude
# ────────────────────────────────────────────────────────────
def summarize_trackman_session(session: dict) -> tuple[Optional[str], int]:
    """Build a per-club summary string from a Trackman session payload.
    Returns (summary_text, total_shot_count). summary_text is None if no shots."""
    groups = session.get("StrokeGroups", [])
    if not groups:
        return None, 0

    lines = []
    total_shots = 0
    player_name = (groups[0].get("Player") or {}).get("Name", "Player")
    date = groups[0].get("Date", "unknown")
    lines.append(f"Player: {player_name}  |  Session date: {date}")
    lines.append(f"Total shots: {sum(len(g.get('Strokes', [])) for g in groups)}")
    lines.append("")
    lines.append("PER-CLUB SUMMARY (averages, all distances in YARDS, speeds in MPH):")
    lines.append("")

    for g in groups:
        club_raw = g.get("Club", "?")
        club_label = CLUB_LABEL_MAP.get(club_raw, club_raw)
        strokes = [s for s in g.get("Strokes", []) if s.get("Measurement")]
        if not strokes:
            continue

        n = len(strokes)
        total_shots += n

        def avg(field: str, conv: float = 1.0) -> Optional[float]:
            vals = [s["Measurement"].get(field) for s in strokes if s["Measurement"].get(field) is not None]
            return (sum(vals) / len(vals) * conv) if vals else None

        carry        = avg("Carry", M_TO_YARDS)
        total        = avg("Total", M_TO_YARDS)
        carry_side   = avg("CarrySide", M_TO_YARDS)
        ball_speed   = avg("BallSpeed", MPS_TO_MPH)
        club_speed   = avg("ClubSpeed", MPS_TO_MPH)
        smash        = avg("SmashFactor")
        launch       = avg("LaunchAngle")
        spin         = avg("SpinRate")
        face_to_path = avg("FaceToPath")
        club_path    = avg("ClubPath")
        max_height   = avg("MaxHeight", M_TO_YARDS)

        best_carry = max((s["Measurement"].get("Carry", 0) or 0) for s in strokes) * M_TO_YARDS
        carries = [(s["Measurement"].get("Carry", 0) or 0) * M_TO_YARDS for s in strokes]
        mean = sum(carries) / len(carries) if carries else 0
        stddev = (sum((c - mean) ** 2 for c in carries) / len(carries)) ** 0.5 if carries else 0

        tier = shot_count_tier(n)
        lines.append(f"{club_label} ({n} shots this session — {tier})")
        if carry is not None:
            lines.append(f"  Carry:       avg {carry:.0f} yd  |  best {best_carry:.0f} yd  |  consistency ±{stddev:.0f} yd")
        if total is not None:
            lines.append(f"  Total:       avg {total:.0f} yd")
        if carry_side is not None:
            lines.append(f"  Side miss:   avg {carry_side:+.1f} yd ({'right' if carry_side > 0 else 'left'} of target)")
        if ball_speed is not None and club_speed is not None and smash is not None:
            lines.append(f"  Ball speed:  {ball_speed:.1f} mph  |  Club speed: {club_speed:.1f} mph  |  Smash: {smash:.2f}")
        if launch is not None and spin is not None and max_height is not None:
            lines.append(f"  Launch:      {launch:.1f}°  |  Spin: {spin:.0f} rpm  |  Apex: {max_height:.0f} yd")
        if face_to_path is not None and club_path is not None:
            lines.append(f"  Face-to-path: {face_to_path:+.1f}°  |  Club path: {club_path:+.1f}°")
        lines.append("")

    return "\n".join(lines), total_shots


# ────────────────────────────────────────────────────────────
# CSV → flat text for Claude
# ────────────────────────────────────────────────────────────
def parse_trackman_csv_text(csv_text: str) -> tuple[Optional[str], int]:
    """Parse pasted/uploaded CSV content into a flat string + row count."""
    if not csv_text or not csv_text.strip():
        return None, 0
    try:
        # utf-8-sig strips BOM if Trackman exported one
        if csv_text.startswith("﻿"):
            csv_text = csv_text[1:]
        reader = csv.DictReader(io.StringIO(csv_text))
        rows = list(reader)
    except Exception:
        return None, 0
    if not rows:
        return None, 0
    headers = list(rows[0].keys())
    lines = [", ".join(headers)]
    for row in rows:
        lines.append(", ".join(str(row.get(h, "")).strip() for h in headers))
    return "\n".join(lines), len(rows)


# ────────────────────────────────────────────────────────────
# Claude prompt → tendencies summary
# ────────────────────────────────────────────────────────────
def generate_tendencies_summary(
    first_name: str,
    existing_summary: Optional[str],
    session_data_str: str,
) -> Optional[str]:
    """Ask Claude to merge a fresh Trackman session into the player's existing
    tendencies summary. Returns the new summary, or None if the API failed."""
    if not anthropic_client:
        return None

    existing = existing_summary or "No prior tendencies on file."
    prompt = f"""You are analyzing a Trackman simulator session for {first_name} to update their AI caddy profile.

EXISTING TENDENCIES ON FILE:
{existing}

NEW TRACKMAN SESSION DATA:
{session_data_str}

TIERED CONFIDENCE RULES (critical — follow exactly):

Trust in a Trackman-derived club distance is a gradient based on CUMULATIVE shots across ALL sessions (not just this one). When you write the updated summary, track a running shot total per club so future updates can keep accumulating.

Tiers:
  • Cumulative < {SHOT_TIER_SMALL} shots → TOO FEW. Don't even mention; ignore until more data.
  • Cumulative {SHOT_TIER_SMALL}–{SHOT_TIER_MEDIUM - 1} shots → LOW CONFIDENCE / small sample.
    Note the shot count and current average, but TELL THE CADDY to keep using the
    player's stated bag distance during play. One short session can produce
    misleading numbers (fatigue, mood, conditions).
  • Cumulative {SHOT_TIER_MEDIUM}–{SHOT_TIER_HIGH - 1} shots → MEDIUM CONFIDENCE.
    Trust the Trackman average as the working distance unless it diverges
    sharply (>20%) from the player's stated value — in that case, flag the
    discrepancy and let the player decide. Otherwise use the Trackman number.
  • Cumulative ≥ {SHOT_TIER_HIGH} shots → HIGH CONFIDENCE.
    The Trackman number IS the player's real distance. Use it as canonical.

Each club line in the NEW SESSION DATA above shows the count for THIS session
only, plus an in-session tier label. You must combine that with whatever shot
counts are already in the EXISTING TENDENCIES (look for parenthetical counts
like "(67 shots, MEDIUM)") to get the cumulative total. If the existing
summary has no prior count for a club, treat the cumulative as this session's
count alone.

WRITE THE UPDATED SUMMARY:
- For each significant club, include the format: "Driver: 245 yd avg over X shots cumulative (TIER). [observations]"
- Drop clubs that are still TOO FEW unless you want to flag them as "needs more reps."
- Miss patterns / shape / face-to-path / smash factor: only call out tendencies where the sample supports them (medium+ confidence).
- Compare against prior — note improvements ("driver consistency is tightening — was ±28 yd over 30 shots, now ±19 yd over 80 shots").
- Tell the caddy clearly which clubs to trust the Trackman numbers for vs. which to defer to the stated bag.

Write in second person, factual, useful for in-round decision making. Around 220 words. Always include the cumulative shot count in parentheses for every club so future updates can keep counting."""

    try:
        response = anthropic_client.messages.create(
            model="claude-opus-4-7",
            max_tokens=600,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text
    except Exception as e:
        print(f"[trackman] Claude summary failed: {e}")
        return None
