"""
One-time migration of admin/user accounts from the LOCAL dev SQLite database
to the live production backend (via API).

Run this AFTER:
  1. Production backend is deployed
  2. Bootstrap admin (sullydakid) has been created on production
  3. You can log in to https://caddy-sepia.vercel.app

Usage:
  python3 seed_production_profile.py

It will:
  • Log in as the bootstrap admin (sullydakid)
  • UPDATE that admin's profile with all your local data (bag, rounds, tendencies, etc.)
  • CREATE every other approved user from your local DB (Drew etc.) preserving their hashed PIN
"""
import json
import sqlite3
import sys
import getpass
from pathlib import Path

import requests

LOCAL_DB = Path(__file__).parent / "caddy.db"
PROD_URL = "https://caddy-api.onrender.com"
ADMIN_USERNAME = "sullydakid"


def main():
    if not LOCAL_DB.exists():
        print(f"Local DB not found at {LOCAL_DB}")
        sys.exit(1)

    # Load all approved/admin users from local DB
    conn = sqlite3.connect(LOCAL_DB)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM users WHERE status = 'approved' ORDER BY id"
    ).fetchall()
    conn.close()

    if not rows:
        print("No approved users found in local DB")
        sys.exit(1)

    # Find the admin row
    admin_row = next((r for r in rows if r["username"] == ADMIN_USERNAME), None)
    if not admin_row:
        print(f"Admin '{ADMIN_USERNAME}' not found in local DB")
        sys.exit(1)
    other_rows = [r for r in rows if r["username"] != ADMIN_USERNAME]

    print(f"Found in local DB:")
    print(f"  • {ADMIN_USERNAME} (admin) — to UPDATE on production")
    for r in other_rows:
        kind = "admin" if r["is_admin"] else "user"
        print(f"  • {r['username']} ({kind}) — to CREATE on production")

    confirm = input(f"\nPush to {PROD_URL}? (yes/no): ").strip().lower()
    if confirm != "yes":
        print("Cancelled.")
        return

    # Login
    pin = getpass.getpass(f"Production PIN for {ADMIN_USERNAME}: ").strip()
    session = requests.Session()
    r = session.post(f"{PROD_URL}/api/login", json={"username": ADMIN_USERNAME, "pin": pin})
    if r.status_code != 200:
        print(f"Login failed: {r.status_code} {r.text}")
        sys.exit(1)
    me = r.json()["user"]
    if not me.get("is_admin"):
        print("That account isn't admin. Aborting.")
        sys.exit(1)
    print(f"\n✓ Logged in as {me['full_name']} ({me['username']}) — admin")

    # 1. Update the admin's own profile
    print(f"\nUpdating {ADMIN_USERNAME}'s profile...")
    bag = json.loads(admin_row["bag"]) if admin_row["bag"] else None
    rounds = json.loads(admin_row["rounds"]) if admin_row["rounds"] else None
    r = session.post(f"{PROD_URL}/api/admin/import-my-profile", json={
        "bag": bag,
        "driver_miss": admin_row["driver_miss"],
        "iron_miss": admin_row["iron_miss"],
        "home_course": admin_row["home_course"],
        "rounds": rounds,
        "handicap_index": admin_row["handicap_index"],
        "tendencies_summary": admin_row["tendencies_summary"],
        "onboarded": True,
    })
    if r.status_code != 200:
        print(f"  ✗ Failed: {r.status_code} {r.text}")
    else:
        result = r.json()
        print(f"  ✓ Updated fields: {result.get('fields')}")

    # 2. Create the other users
    for row in other_rows:
        print(f"\nCreating {row['username']}...")
        bag = json.loads(row["bag"]) if row["bag"] else None
        rounds = json.loads(row["rounds"]) if row["rounds"] else None
        r = session.post(f"{PROD_URL}/api/admin/create-user-directly", json={
            "username": row["username"],
            "pin_hash": row["pin_hash"],
            "full_name": row["full_name"],
            "is_admin": bool(row["is_admin"]),
            "onboarded": bool(row["onboarded"]),
            "bag": bag,
            "driver_miss": row["driver_miss"],
            "iron_miss": row["iron_miss"],
            "home_course": row["home_course"],
            "rounds": rounds,
            "handicap_index": row["handicap_index"],
            "tendencies_summary": row["tendencies_summary"],
        })
        if r.status_code != 200:
            print(f"  ✗ Failed: {r.status_code} {r.text}")
        else:
            print(f"  ✓ Created: {r.json()}")

    print(f"\nDone. Refresh https://caddy-sepia.vercel.app and your data should be live.")


if __name__ == "__main__":
    main()
