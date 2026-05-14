"""
One-time migration of an admin's profile data from the LOCAL dev SQLite database
to the live production backend (via API).

Run this AFTER:
  1. Production backend is deployed
  2. Bootstrap admin has been created (so you can log in)

Usage:
  python3 seed_production_profile.py
"""
import json
import sqlite3
import sys
import getpass
from pathlib import Path

import requests

LOCAL_DB = Path(__file__).parent / "caddy.db"
PROD_URL = "https://caddy-api.onrender.com"


def main():
    if not LOCAL_DB.exists():
        print(f"Local DB not found at {LOCAL_DB}")
        sys.exit(1)

    username = input("Username (the admin to migrate): ").strip().lower()

    # Load profile from local DB
    conn = sqlite3.connect(LOCAL_DB)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    conn.close()

    if not row:
        print(f"User '{username}' not found in local DB")
        sys.exit(1)

    bag = json.loads(row["bag"]) if row["bag"] else None
    rounds = json.loads(row["rounds"]) if row["rounds"] else None

    print(f"\nLocal data found for {row['full_name']}:")
    print(f"  bag clubs:   {len([v for v in (bag or {}).values() if v])}")
    print(f"  rounds:      {len(rounds or [])}")
    print(f"  tendencies:  {len(row['tendencies_summary'] or '')} chars")
    print(f"  driver_miss: {row['driver_miss'] or 'none'}")
    print(f"  iron_miss:   {row['iron_miss'] or 'none'}")
    print(f"  home_course: {row['home_course'] or 'none'}")
    print(f"  handicap:    {row['handicap_index']}")

    confirm = input(f"\nPush this to {PROD_URL}? (yes/no): ").strip().lower()
    if confirm != "yes":
        print("Cancelled.")
        return

    # Login to production
    pin = getpass.getpass(f"PIN for {username} on production: ").strip()
    session = requests.Session()
    r = session.post(f"{PROD_URL}/api/login", json={"username": username, "pin": pin})
    if r.status_code != 200:
        print(f"Login failed: {r.status_code} {r.text}")
        sys.exit(1)
    me = r.json()["user"]
    if not me.get("is_admin"):
        print("That account isn't admin. Aborting.")
        sys.exit(1)
    print(f"Logged in as {me['full_name']} ({me['username']}) — admin")

    # Push the profile data
    r = session.post(f"{PROD_URL}/api/admin/import-my-profile", json={
        "bag": bag,
        "driver_miss": row["driver_miss"],
        "iron_miss": row["iron_miss"],
        "home_course": row["home_course"],
        "rounds": rounds,
        "handicap_index": row["handicap_index"],
        "tendencies_summary": row["tendencies_summary"],
        "onboarded": True,
    })
    if r.status_code != 200:
        print(f"Import failed: {r.status_code} {r.text}")
        sys.exit(1)
    result = r.json()
    print(f"\n✓ Updated fields: {result.get('fields')}")
    print(f"\nGo refresh https://caddy-sepia.vercel.app/profile — your data should be there.")


if __name__ == "__main__":
    main()
