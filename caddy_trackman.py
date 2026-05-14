import csv
import os
import re
import json
import requests
import anthropic
from dotenv import load_dotenv
load_dotenv(override=True)
from caddy_auth import load_profile, save_profile, hash_pin

_anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
if not _anthropic_key:
    raise RuntimeError("Set ANTHROPIC_API_KEY environment variable before running.")
anthropic_client = anthropic.Anthropic(api_key=_anthropic_key)

TRACKMAN_API = "https://golf-player-activities.trackmangolf.com/api/reports/getreport"
TRACKMAN_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json",
    "Origin": "https://web-dynamic-reports.trackmangolf.com",
    "Referer": "https://web-dynamic-reports.trackmangolf.com/",
    "User-Agent": "Mozilla/5.0",
}

# Conversion: Trackman API returns metric (m/s for speeds, meters for distances)
M_TO_YARDS = 1.0936
MPS_TO_MPH = 2.2369

# Map Trackman club names to readable labels
CLUB_LABEL_MAP = {
    "Driver": "Driver", "3Wood": "3-wood", "5Wood": "5-wood", "7Wood": "7-wood",
    "2Iron": "2-iron", "3Iron": "3-iron", "4Iron": "4-iron", "5Iron": "5-iron",
    "6Iron": "6-iron", "7Iron": "7-iron", "8Iron": "8-iron", "9Iron": "9-iron",
    "PitchingWedge": "Pitching wedge", "GapWedge": "Gap wedge",
    "SandWedge": "Sand wedge", "LobWedge": "Lob wedge",
    "Hybrid": "Hybrid", "3Hybrid": "3-hybrid", "4Hybrid": "4-hybrid", "5Hybrid": "5-hybrid",
}


def extract_report_id(text):
    """Pull a Trackman report ID (UUID) from a URL or raw string."""
    m = re.search(r"r=([0-9a-fA-F\-]{36})", text)
    if m:
        return m.group(1)
    m = re.search(r"\b([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\b", text)
    if m:
        return m.group(1)
    return None


def resolve_short_url(url):
    """Follow Trackman short links to extract the report ID."""
    try:
        r = requests.get(url, headers=TRACKMAN_HEADERS, allow_redirects=True, timeout=10)
        return extract_report_id(r.url)
    except Exception:
        return None


def fetch_trackman_report(url_or_id):
    """Given a Trackman URL or report ID, fetch the full session JSON."""
    report_id = extract_report_id(url_or_id) if "-" in url_or_id else url_or_id
    if not report_id and url_or_id.startswith("http"):
        report_id = resolve_short_url(url_or_id)
    if not report_id:
        return None
    r = requests.post(TRACKMAN_API, headers=TRACKMAN_HEADERS, json={"reportId": report_id}, timeout=20)
    if r.status_code != 200:
        return None
    return r.json()


def summarize_trackman_session(session):
    """Build a clean per-club summary string from the raw Trackman JSON."""
    groups = session.get("StrokeGroups", [])
    if not groups:
        return None, 0

    lines = []
    total_shots = 0
    player_name = groups[0]["Player"].get("Name", "Player")
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

        def avg(field, conv=1.0):
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

        # Best (longest) carry
        best_carry = max((s["Measurement"].get("Carry", 0) or 0) for s in strokes) * M_TO_YARDS
        # Spread (stddev of carry)
        carries = [(s["Measurement"].get("Carry", 0) or 0) * M_TO_YARDS for s in strokes]
        mean = sum(carries) / len(carries)
        stddev = (sum((c - mean) ** 2 for c in carries) / len(carries)) ** 0.5

        lines.append(f"{club_label} ({n} shots)")
        lines.append(f"  Carry:       avg {carry:.0f} yd  |  best {best_carry:.0f} yd  |  consistency ±{stddev:.0f} yd")
        lines.append(f"  Total:       avg {total:.0f} yd")
        if carry_side is not None:
            lines.append(f"  Side miss:   avg {carry_side:+.1f} yd ({'right' if carry_side > 0 else 'left'} of target)")
        if ball_speed is not None:
            lines.append(f"  Ball speed:  {ball_speed:.1f} mph  |  Club speed: {club_speed:.1f} mph  |  Smash: {smash:.2f}")
        if launch is not None:
            lines.append(f"  Launch:      {launch:.1f}°  |  Spin: {spin:.0f} rpm  |  Apex: {max_height:.0f} yd")
        if face_to_path is not None:
            lines.append(f"  Face-to-path: {face_to_path:+.1f}°  |  Club path: {club_path:+.1f}°")
        lines.append("")

    return "\n".join(lines), total_shots


def analyze_trackman_session(profile, session):
    summary_str, shot_count = summarize_trackman_session(session)
    if not summary_str:
        print("No shot data found.")
        return

    print(f"\nFound {shot_count} shots across the session.")
    print("\n" + "=" * 70)
    print(summary_str)
    print("=" * 70)
    print("\nSending to Claude for tendency analysis...\n")

    existing = profile.get("tendencies_summary") or "No prior tendencies on file."
    first_name = profile["name"].split()[0]

    prompt = f"""You are analyzing a Trackman simulator session for {first_name} to update their AI caddy profile.

EXISTING TENDENCIES ON FILE:
{existing}

NEW TRACKMAN SESSION DATA:
{summary_str}

Write an updated player tendencies summary that this AI caddy can use during real rounds. Include:

1. Real average carry distances per club (use the data, not the player's estimates)
2. Consistency per club (the spread tells you which clubs are reliable)
3. Miss patterns — direction (left/right) and shape (face-to-path tells you draw/fade tendency)
4. Notable swing tendencies (smash factor reliability, spin rate concerns, launch angle)
5. Which clubs are strongest and which need work
6. How this confirms or updates the existing profile

Write in second person (e.g. "Your 7-iron averages 138 yards with a slight push right..."), factual, useful for in-round decision making. 200 words max."""

    response = anthropic_client.messages.create(
        model="claude-opus-4-7",
        max_tokens=600,
        messages=[{"role": "user", "content": prompt}]
    )

    summary = response.content[0].text
    profile["tendencies_summary"] = summary
    save_profile(profile)

    print("--- UPDATED PLAYER PROFILE TENDENCIES ---\n")
    print(summary)
    print("\nProfile saved.")


def parse_trackman_csv(csv_path):
    """Legacy CSV parser kept for users who do export a CSV."""
    with open(csv_path, newline='', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        rows = list(reader)
    if not rows:
        return None, None
    headers = list(rows[0].keys())
    lines = [", ".join(headers)]
    for row in rows:
        lines.append(", ".join(str(row.get(h, "")).strip() for h in headers))
    return "\n".join(lines), len(rows)


def analyze_trackman_csv(profile, csv_path):
    print("Reading Trackman CSV...")
    data_str, shot_count = parse_trackman_csv(csv_path)
    if not data_str:
        print("No shot data found in file.")
        return

    print(f"Found {shot_count} rows. Sending to Claude...\n")
    existing = profile.get("tendencies_summary") or "No prior data."
    first_name = profile["name"].split()[0]
    prompt = f"""Analyze Trackman session CSV data for {first_name}.

Existing tendencies:
{existing}

CSV data:
{data_str}

Write an updated tendencies summary in second person, under 200 words."""
    response = anthropic_client.messages.create(
        model="claude-opus-4-7",
        max_tokens=500,
        messages=[{"role": "user", "content": prompt}]
    )
    summary = response.content[0].text
    profile["tendencies_summary"] = summary
    save_profile(profile)
    print(summary)


def main():
    print("=== Caddy Trackman Upload ===\n")

    username = input("Username: ").strip().lower()
    profile = load_profile(username)
    if not profile:
        print("User not found.")
        return

    pin = input("PIN: ").strip()
    if profile["pin"] != hash_pin(pin):
        print("Incorrect PIN.")
        return

    print("\nPaste a Trackman web report URL OR a path to a Trackman CSV file:")
    src = input("> ").strip().strip('"').strip("'")

    if src.startswith("http") or extract_report_id(src):
        print("\nFetching Trackman session from web report...")
        session = fetch_trackman_report(src)
        if not session:
            print("Could not load report. Check the URL or report ID.")
            return
        analyze_trackman_session(profile, session)
    elif os.path.exists(src):
        analyze_trackman_csv(profile, src)
    else:
        print("Not a valid URL or file path.")


if __name__ == "__main__":
    main()
