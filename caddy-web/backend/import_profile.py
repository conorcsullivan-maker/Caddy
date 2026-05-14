"""
One-time import of existing JSON profiles from the Mac voice version.
Run once after setting up the DB. Marks Conor's account as admin.
"""
import json
import sqlite3
import sys
from pathlib import Path
from datetime import datetime, timezone

BACKEND_DIR = Path(__file__).parent
DB_PATH = BACKEND_DIR / "caddy.db"
PROFILES_DIR = BACKEND_DIR.parent.parent / "profiles"  # ../../profiles relative to backend/

ADMIN_USERNAMES = {"sullydakid"}  # flagged as admin on import


def import_profile(conn, profile: dict):
    username = profile["username"].lower()
    cursor = conn.execute("SELECT id, status, is_admin FROM users WHERE username = ?", (username,))
    existing = cursor.fetchone()

    # Preserve existing admin status if user already exists, otherwise check the seed list
    if existing and existing[2]:
        is_admin = 1
    else:
        is_admin = 1 if username in ADMIN_USERNAMES else 0
    bag_json = json.dumps(profile.get("bag") or {})
    rounds_json = json.dumps(profile.get("rounds") or [])
    now = datetime.now(timezone.utc).isoformat()

    if existing:
        conn.execute("""
            UPDATE users SET
                pin_hash = ?, full_name = ?, status = 'approved', is_admin = ?,
                onboarded = ?, approved_at = COALESCE(approved_at, ?),
                bag = ?, driver_miss = ?, iron_miss = ?, home_course = ?,
                rounds = ?, handicap_index = ?, tendencies_summary = ?
            WHERE username = ?
        """, (
            profile["pin"],  # already hashed in original profiles
            profile["name"],
            is_admin,
            1 if profile.get("onboarded") else 0,
            now,
            bag_json,
            profile.get("driver_miss"),
            profile.get("iron_miss"),
            profile.get("home_course"),
            rounds_json,
            profile.get("handicap_index"),
            profile.get("tendencies_summary"),
            username,
        ))
        print(f"  ↺ updated existing user '{username}' (admin={bool(is_admin)})")
    else:
        conn.execute("""
            INSERT INTO users (
                username, pin_hash, full_name, status, is_admin, onboarded,
                created_at, approved_at, bag, driver_miss, iron_miss, home_course,
                rounds, handicap_index, tendencies_summary
            ) VALUES (?, ?, ?, 'approved', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            username,
            profile["pin"],
            profile["name"],
            is_admin,
            1 if profile.get("onboarded") else 0,
            profile.get("created", now),
            now,
            bag_json,
            profile.get("driver_miss"),
            profile.get("iron_miss"),
            profile.get("home_course"),
            rounds_json,
            profile.get("handicap_index"),
            profile.get("tendencies_summary"),
        ))
        print(f"  + imported '{username}' (admin={bool(is_admin)})")


def main():
    if not PROFILES_DIR.exists():
        print(f"Profiles directory not found at {PROFILES_DIR}")
        sys.exit(1)

    # Initialize DB schema if it doesn't exist
    from main import init_db
    init_db()

    profile_files = sorted(PROFILES_DIR.glob("*.json"))
    if not profile_files:
        print("No profile files found.")
        return

    print(f"Importing {len(profile_files)} profile(s) from {PROFILES_DIR}\n")

    conn = sqlite3.connect(DB_PATH)
    try:
        for path in profile_files:
            with open(path) as f:
                profile = json.load(f)
            import_profile(conn, profile)
        conn.commit()
    finally:
        conn.close()

    print(f"\nDone. Database: {DB_PATH}")


if __name__ == "__main__":
    main()
