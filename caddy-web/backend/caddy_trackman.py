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
def summarize_trackman_session(session: dict) -> tuple[Optional[str], int, dict]:
    """Build a per-club summary string from a Trackman session payload.
    Returns (summary_text, total_shot_count, per_club_stats).
      - summary_text: prose for Claude (qualitative + observations)
      - per_club_stats: structured per-club bucket dicts suitable for merging
        into the user's persistent shot_stats[club].trackman record.
    summary_text is None if no shots."""
    groups = session.get("StrokeGroups", [])
    if not groups:
        return None, 0, {}

    lines = []
    total_shots = 0
    per_club_stats: dict = {}
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

        carries = [(s["Measurement"].get("Carry", 0) or 0) * M_TO_YARDS for s in strokes]
        best_carry = max(carries) if carries else 0
        worst_carry = min(carries) if carries else 0
        mean = sum(carries) / len(carries) if carries else 0
        sum_sq = sum(c * c for c in carries)
        stddev = (sum((c - mean) ** 2 for c in carries) / len(carries)) ** 0.5 if carries else 0
        total_carry_int = int(round(sum(carries)))

        # Structured bucket for persistent shot_stats merge (Trackman doesn't
        # tag direction so left/right/center stay zero on this side).
        per_club_stats[club_label] = {
            "count": n,
            "total_carry": total_carry_int,
            "sum_sq": int(round(sum_sq)),
            "best": int(round(best_carry)),
            "worst": int(round(worst_carry)),
            "left": 0, "right": 0, "center": 0,
        }

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

    return "\n".join(lines), total_shots, per_club_stats


# ────────────────────────────────────────────────────────────
# CSV → flat text for Claude
# ────────────────────────────────────────────────────────────
def parse_trackman_csv_text(csv_text: str) -> tuple[Optional[str], int, dict]:
    """Parse pasted/uploaded CSV content into (flat_text, row_count, per_club_stats).
    The CSV format Trackman exports has shot-per-row data with a Club column; we
    aggregate Carry per club into the same bucket shape as the JSON parser so
    both upload paths feed shot_stats consistently. Returns per_club_stats={} if
    no recognizable Club + Carry columns are present."""
    if not csv_text or not csv_text.strip():
        return None, 0, {}
    try:
        if csv_text.startswith("﻿"):
            csv_text = csv_text[1:]
        reader = csv.DictReader(io.StringIO(csv_text))
        rows = list(reader)
    except Exception:
        return None, 0, {}
    if not rows:
        return None, 0, {}
    headers = list(rows[0].keys())

    # Find the Club and Carry columns (case-insensitive, tolerant of headers
    # like "Club Type", "Carry Distance", "Carry (yd)" etc.)
    def find_col(*needles: str) -> Optional[str]:
        for h in headers:
            hl = h.lower()
            if all(n in hl for n in needles):
                return h
        return None

    club_col  = find_col("club")
    carry_col = find_col("carry") or find_col("distance")

    per_club_stats: dict = {}
    if club_col and carry_col:
        # Aggregate per-club carries into bucket shape
        agg: dict = {}
        for row in rows:
            club_raw = (row.get(club_col) or "").strip()
            if not club_raw:
                continue
            club_label = CLUB_LABEL_MAP.get(club_raw, club_raw)
            try:
                # Carry may already be in yards in the CSV; assume so.
                carry = float(str(row.get(carry_col, "")).strip() or 0)
            except ValueError:
                continue
            if carry <= 0:
                continue
            agg.setdefault(club_label, []).append(carry)
        for club, carries in agg.items():
            n = len(carries)
            if n == 0:
                continue
            per_club_stats[club] = {
                "count": n,
                "total_carry": int(round(sum(carries))),
                "sum_sq": int(round(sum(c * c for c in carries))),
                "best": int(round(max(carries))),
                "worst": int(round(min(carries))),
                "left": 0, "right": 0, "center": 0,
            }

    lines = [", ".join(headers)]
    for row in rows:
        lines.append(", ".join(str(row.get(h, "")).strip() for h in headers))
    return "\n".join(lines), len(rows), per_club_stats


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
    prompt = f"""You are writing the QUALITATIVE narrative for {first_name}'s AI caddy profile based on a fresh Trackman session.

IMPORTANT: Quantitative data (per-club shot counts, average distances, spread, best/worst, confidence tier) is stored elsewhere in a structured database and computed automatically. DO NOT put shot counts or numeric averages in your summary. Focus only on the QUALITATIVE observations the structured data can't capture.

EXISTING NARRATIVE ON FILE:
{existing}

NEW TRACKMAN SESSION DATA:
{session_data_str}

WRITE AN UPDATED QUALITATIVE SUMMARY that covers:
- Miss patterns — direction (left/right) and shape (face-to-path tells you draw/fade tendency)
- Swing tendencies — smash factor reliability, spin rate concerns, launch angle quirks
- Which clubs feel strongest and which need work
- Notable changes from the prior narrative — improvements, regressions, new patterns
- Any context that affects in-round decisions (fatigue patterns late in sessions, recurring miss shapes, etc.)

DO NOT include:
- Specific yardage averages ("Driver averages 245 yards") — those are in the structured store and may differ session-to-session
- Shot counts ("over 18 shots") — also in the structured store
- Statements like "high confidence" or "trust this number" — the system handles tier classification separately

Write in second person, factual, useful for in-round decision making. Around 180 words. Think of this as the "scouting report" a Tour caddy would jot in a notebook — patterns and observations, not numbers."""

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
