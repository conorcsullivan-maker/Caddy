import json
import os
import hashlib

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


def login():
    print("\n=== Caddy Login ===")
    username = input("Username: ").strip().lower()
    profile = load_profile(username)
    if not profile:
        print("Username not found.")
        return None

    pin = input("PIN: ").strip()
    if profile["pin"] != hash_pin(pin):
        print("Incorrect PIN.")
        return None

    return profile


CLUB_MAX = {
    "driver": (100, 400),
    "3-wood": (80, 350),
    "5-wood": (70, 300),
    "4-iron": (60, 270),
    "5-iron": (60, 250),
    "6-iron": (50, 230),
    "7-iron": (50, 210),
    "8-iron": (40, 190),
    "9-iron": (40, 170),
    "pitching_wedge": (30, 150),
    "gap_wedge": (30, 140),
    "sand_wedge": (20, 130),
    "lob_wedge": (20, 120),
}


def enter_club_distances():
    bag = {}
    print("Enter your carry distance for each club.")
    print("Leave blank if you don't carry it. Type 'back' to correct the previous club.\n")

    i = 0
    while i < len(CLUBS):
        club = CLUBS[i]
        label = CLUB_LABELS[club]
        val = input(f"  {label}: ").strip().lower()

        if val == "back":
            if i == 0:
                print("  Already at the first club.")
                continue
            i -= 1
            prev_club = CLUBS[i]
            print(f"  Re-entering {CLUB_LABELS[prev_club]}...")
            continue

        if val == "":
            bag[club] = None
            i += 1
            continue

        try:
            yards = int(val)
            lo, hi = CLUB_MAX[club]
            if yards < lo or yards > hi:
                print(f"  That's outside the expected range for a {label} ({lo}–{hi} yards). Are you sure? (yes/no): ", end="")
                if input().strip().lower() != "yes":
                    continue
            bag[club] = yards
            i += 1
        except ValueError:
            print("  Please enter a number.")

    return bag


def review_bag(bag):
    print("\n--- Your Bag ---")
    for club in CLUBS:
        yards = bag.get(club)
        label = CLUB_LABELS[club]
        if yards:
            print(f"  {label}: {yards} yards")
        else:
            print(f"  {label}: not in bag")

    print("\nDoes everything look right? (yes / no — enter 'no' to edit a club)")
    answer = input().strip().lower()
    if answer == "yes":
        return bag

    while True:
        edit_club = input("Which club to edit? (e.g. '9-iron', or 'done'): ").strip().lower()
        if edit_club == "done":
            break
        match = next((c for c in CLUBS if c == edit_club or CLUB_LABELS[c].lower() == edit_club), None)
        if not match:
            print("  Club not recognized. Try again.")
            continue
        label = CLUB_LABELS[match]
        while True:
            val = input(f"  New distance for {label} (blank = not in bag): ").strip()
            if val == "":
                bag[match] = None
                break
            try:
                yards = int(val)
                lo, hi = CLUB_MAX[match]
                if yards < lo or yards > hi:
                    print(f"  Outside expected range ({lo}–{hi} yards). Are you sure? (yes/no): ", end="")
                    if input().strip().lower() != "yes":
                        continue
                bag[match] = yards
                break
            except ValueError:
                print("  Please enter a number.")

    return bag


def run_onboarding(profile):
    print(f"\nWelcome, {profile['name']}! Let's set up your bag.")

    while True:
        bag = enter_club_distances()
        bag = review_bag(bag)
        print("\nBag confirmed. ")
        break

    profile["bag"] = bag
    print()
    profile["driver_miss"] = input("Driver miss tendency (e.g. 'fades right late'): ").strip() or None
    profile["iron_miss"] = input("Iron miss tendency (e.g. 'left, alignment issue'): ").strip() or None
    profile["home_course"] = input("Home course name: ").strip() or None

    profile["onboarded"] = True
    save_profile(profile)
    print(f"\nYou're all set. Let's play, {profile['name'].split()[0]}.")
    return profile


def build_bag_summary(profile):
    lines = []
    for club in CLUBS:
        yards = profile["bag"].get(club)
        if yards:
            label = CLUB_LABELS[club]
            lines.append(f"{label}: {yards} yards")
    return "\n".join(lines)


def build_system_prompt(profile, base_prompt):
    name = profile["name"].split()[0]
    bag = build_bag_summary(profile)
    driver_miss = profile.get("driver_miss") or "unknown"
    iron_miss = profile.get("iron_miss") or "unknown"
    tendencies = profile.get("tendencies_summary") or "No round history yet."

    player_section = f"""
=== PLAYER PROFILE ===
Name: {name}
Driver miss: {driver_miss}
Iron miss: {iron_miss}

=== PLAYER CLUB DISTANCES ===
{bag}

=== PLAYER HISTORY & TENDENCIES ===
{tendencies}
"""
    return base_prompt + player_section
