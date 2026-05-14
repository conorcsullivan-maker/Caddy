import json
import os
import hashlib
from datetime import datetime

PROFILES_DIR = os.path.join(os.path.dirname(__file__), "profiles")

CLUBS = [
    "driver", "3-wood", "5-wood", "4-iron", "5-iron", "6-iron",
    "7-iron", "8-iron", "9-iron", "pitching_wedge", "gap_wedge",
    "sand_wedge", "lob_wedge"
]

CLUB_LABELS = {
    "driver": "Driver",
    "3-wood": "3-wood",
    "5-wood": "5-wood",
    "4-iron": "4-iron",
    "5-iron": "5-iron",
    "6-iron": "6-iron",
    "7-iron": "7-iron",
    "8-iron": "8-iron",
    "9-iron": "9-iron",
    "pitching_wedge": "Pitching wedge",
    "gap_wedge": "Gap wedge",
    "sand_wedge": "Sand wedge",
    "lob_wedge": "Lob wedge"
}


def hash_pin(pin):
    return hashlib.sha256(pin.encode()).hexdigest()


def profile_path(username):
    return os.path.join(PROFILES_DIR, f"{username.lower()}.json")


def load_profile(username):
    path = profile_path(username)
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f)


def save_profile(profile):
    path = profile_path(profile["username"])
    with open(path, "w") as f:
        json.dump(profile, f, indent=2)


def create_user():
    print("\n--- Create New User ---")
    name = input("Full name: ").strip()
    username = input("Username: ").strip().lower()

    if os.path.exists(profile_path(username)):
        print(f"Username '{username}' already exists.")
        return

    pin = input("Assign PIN (4 digits): ").strip()
    if not pin.isdigit() or len(pin) != 4:
        print("PIN must be exactly 4 digits.")
        return

    profile = {
        "name": name,
        "username": username,
        "pin": hash_pin(pin),
        "created": datetime.now().strftime("%Y-%m-%d"),
        "onboarded": False,
        "bag": {club: None for club in CLUBS},
        "driver_miss": None,
        "iron_miss": None,
        "home_course": None,
        "rounds": [],
        "handicap_index": None,
        "tendencies_summary": ""
    }

    save_profile(profile)
    print(f"\nUser '{username}' created. Share these credentials:")
    print(f"  Username: {username}")
    print(f"  PIN:      {pin}")


def list_users():
    print("\n--- All Users ---")
    files = [f for f in os.listdir(PROFILES_DIR) if f.endswith(".json")]
    if not files:
        print("No users yet.")
        return
    for f in files:
        with open(os.path.join(PROFILES_DIR, f)) as fp:
            p = json.load(fp)
        rounds = len(p.get("rounds", []))
        onboarded = "onboarded" if p.get("onboarded") else "not yet onboarded"
        print(f"  {p['username']} ({p['name']}) — {rounds} rounds — {onboarded}")


def delete_user():
    print("\n--- Delete User ---")
    username = input("Username to delete: ").strip().lower()
    path = profile_path(username)
    if not os.path.exists(path):
        print("User not found.")
        return
    confirm = input(f"Delete '{username}' and all their data? (yes/no): ").strip().lower()
    if confirm == "yes":
        os.remove(path)
        print(f"User '{username}' deleted.")
    else:
        print("Cancelled.")


def reset_pin():
    print("\n--- Reset PIN ---")
    username = input("Username: ").strip().lower()
    profile = load_profile(username)
    if not profile:
        print("User not found.")
        return
    new_pin = input("New PIN (4 digits): ").strip()
    if not new_pin.isdigit() or len(new_pin) != 4:
        print("PIN must be exactly 4 digits.")
        return
    profile["pin"] = hash_pin(new_pin)
    save_profile(profile)
    print(f"PIN reset for '{username}'.")


def view_user():
    print("\n--- View User Profile ---")
    username = input("Username: ").strip().lower()
    profile = load_profile(username)
    if not profile:
        print("User not found.")
        return

    print(f"\nName:       {profile['name']}")
    print(f"Username:   {profile['username']}")
    print(f"Created:    {profile['created']}")
    rounds = profile.get('rounds', [])
    print(f"Rounds:     {len(rounds)}")
    handicap = profile.get("handicap_index")
    print(f"Handicap:   {handicap if handicap is not None else 'Not yet calculated (need 3+ rounds)'}")
    if rounds:
        last = rounds[-1]
        print(f"Last round: {last['score']} on {last['date']} at {last['course']}")
    print(f"\nBag:")
    for club in CLUBS:
        yards = profile["bag"].get(club)
        label = CLUB_LABELS[club]
        if yards:
            print(f"  {label}: {yards} yards")
        else:
            print(f"  {label}: not in bag")
    print(f"\nDriver miss: {profile.get('driver_miss') or 'not set'}")
    print(f"Iron miss:   {profile.get('iron_miss') or 'not set'}")
    print(f"Home course: {profile.get('home_course') or 'not set'}")
    if profile.get("tendencies_summary"):
        print(f"\nTendencies:\n{profile['tendencies_summary']}")


def main():
    print("=== Caddy Admin ===")
    while True:
        print("\n1. Create user")
        print("2. List users")
        print("3. View user")
        print("4. Reset PIN")
        print("5. Delete user")
        print("6. Exit")
        choice = input("\nChoice: ").strip()

        if choice == "1":
            create_user()
        elif choice == "2":
            list_users()
        elif choice == "3":
            view_user()
        elif choice == "4":
            reset_pin()
        elif choice == "5":
            delete_user()
        elif choice == "6":
            break
        else:
            print("Invalid choice.")


if __name__ == "__main__":
    main()
