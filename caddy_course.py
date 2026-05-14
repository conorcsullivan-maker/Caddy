import os
import requests
from dotenv import load_dotenv
load_dotenv(override=True)

GOLF_COURSE_API_KEY = os.environ.get("GOLF_COURSE_API_KEY")
if not GOLF_COURSE_API_KEY:
    raise RuntimeError("Set GOLF_COURSE_API_KEY environment variable before running.")
BASE_URL = "https://api.golfcourseapi.com/v1"
HEADERS = {"Authorization": f"Key {GOLF_COURSE_API_KEY}"}

TEE_COLORS = ["black", "blue", "gold", "white", "green", "red", "silver", "championship", "tips", "tournament"]


def search_course(query):
    try:
        r = requests.get(f"{BASE_URL}/search", headers=HEADERS, params={"search_query": query}, timeout=5)
        return r.json().get("courses", []) if r.status_code == 200 else []
    except Exception:
        return []


def get_course(course_id):
    try:
        r = requests.get(f"{BASE_URL}/courses/{course_id}", headers=HEADERS, timeout=5)
        data = r.json()
        return data.get("course", data) if r.status_code == 200 else None
    except Exception:
        return None


def find_tee(course, tee_color=None):
    male_tees = course.get("tees", {}).get("male", [])
    if not male_tees:
        return None
    if tee_color:
        for tee in male_tees:
            if tee_color.lower() in tee["tee_name"].lower():
                return tee
    return male_tees[0]


def extract_tee_color(text):
    for color in TEE_COLORS:
        if color in text.lower():
            return color
    return None


def format_course_for_prompt(course, tee):
    club = course.get("club_name", "Unknown Course")
    lines = [
        f"\n=== COURSE DATA: {club} ===",
        f"Tee: {tee['tee_name']} | Rating: {tee['course_rating']} | Slope: {tee['slope_rating']} | Total: {tee['total_yards']} yards",
        "",
        "Hole-by-hole yardages:"
    ]
    for i, hole in enumerate(tee.get("holes", []), 1):
        lines.append(f"  Hole {i}: Par {hole['par']}, {hole['yardage']} yards (Handicap {hole['handicap']})")
    lines.extend([
        "",
        "Use this course data to:",
        "- When the player gives their remaining yardage, calculate hole_length - remaining = estimated carry distance and silently note it against the club they used",
        "- Automatically reference course rating and slope for handicap — no need to ask",
        "- Factor in hole layout and handicap index when advising risk vs. conservative plays",
    ])
    return "\n".join(lines)
