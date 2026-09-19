"""
Course geometry + relative wind computation.

Pipeline:
  1. Fetch hole-level geometry for a course from OpenStreetMap via Overpass
  2. Pair tee + green polygons to their hole via spatial containment in the
     golf=hole polygon (OSM doesn't always tag ref on tee/green elements, but
     they sit INSIDE the hole outline so we can match geometrically)
  3. Compute hole bearing (tee centroid → green centroid)
  4. Combine hole bearing with live NWS wind direction to produce relative
     wind (headwind/tailwind + crosswind) from the player's perspective —
     no asking required.

OSM coverage isn't 100%, so callers must handle the "no data" case
gracefully — fall back to asking the player once per hole.
"""
import json
import math
import re
import urllib.parse
import urllib.request
from typing import Optional

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
OVERPASS_HEADERS = {
    "User-Agent": "Caddy AI Golf (caddy-sepia.vercel.app)",
}

# NWS reports wind direction as a compass string ("WSW") — these are the
# degree centers. 0° = N, 90° = E, 180° = S, 270° = W.
COMPASS_DEG = {
    "N":   0.0, "NNE":  22.5, "NE":  45.0, "ENE":  67.5,
    "E":  90.0, "ESE": 112.5, "SE": 135.0, "SSE": 157.5,
    "S": 180.0, "SSW": 202.5, "SW": 225.0, "WSW": 247.5,
    "W": 270.0, "WNW": 292.5, "NW": 315.0, "NNW": 337.5,
}


# ────────────────────────────────────────────────────────────
# Geometry helpers
# ────────────────────────────────────────────────────────────
def haversine_m(a: tuple, b: tuple) -> float:
    """Distance in meters between two (lat, lng) points."""
    R = 6371000.0
    lat1, lon1 = math.radians(a[0]), math.radians(a[1])
    lat2, lon2 = math.radians(b[0]), math.radians(b[1])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = math.sin(dlat/2)**2 + math.cos(lat1)*math.cos(lat2)*math.sin(dlon/2)**2
    return 2 * R * math.asin(math.sqrt(h))


def bearing_deg(a: tuple, b: tuple) -> float:
    """Compass bearing in degrees from point a to point b (0=N, 90=E)."""
    lat1, lon1 = math.radians(a[0]), math.radians(a[1])
    lat2, lon2 = math.radians(b[0]), math.radians(b[1])
    dlon = lon2 - lon1
    x = math.sin(dlon) * math.cos(lat2)
    y = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def centroid(points: list) -> Optional[tuple]:
    """Simple average centroid of a list of (lat, lng) tuples."""
    if not points:
        return None
    lat = sum(p[0] for p in points) / len(points)
    lng = sum(p[1] for p in points) / len(points)
    return (lat, lng)


def point_in_polygon(point: tuple, polygon: list) -> bool:
    """Ray-casting point-in-polygon test. Polygon is a list of (lat, lng)."""
    lat, lng = point
    n = len(polygon)
    if n < 3:
        return False
    inside = False
    j = n - 1
    for i in range(n):
        lat_i, lng_i = polygon[i]
        lat_j, lng_j = polygon[j]
        if ((lng_i > lng) != (lng_j > lng)) and \
           (lat < (lat_j - lat_i) * (lng - lng_i) / ((lng_j - lng_i) or 1e-12) + lat_i):
            inside = not inside
        j = i
    return inside


def compass_to_deg(direction: Optional[str]) -> Optional[float]:
    """Convert NWS compass string ('WSW') to degrees."""
    if not direction:
        return None
    return COMPASS_DEG.get(direction.upper().strip())


def parse_wind_speed_mph(wind_speed_str: Optional[str]) -> Optional[float]:
    """Parse NWS strings like '5 to 10 mph' → 7.5, or '15 mph' → 15."""
    if not wind_speed_str:
        return None
    nums = [int(n) for n in re.findall(r"\d+", wind_speed_str)]
    if not nums:
        return None
    return sum(nums) / len(nums)


# ────────────────────────────────────────────────────────────
# Overpass fetcher + parser
# ────────────────────────────────────────────────────────────
def _overpass(query: str, timeout: int = 60) -> Optional[dict]:
    data = urllib.parse.urlencode({"data": query}).encode()
    req = urllib.request.Request(OVERPASS_URL, data=data, headers=OVERPASS_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception as e:
        print(f"[geo] Overpass error: {e}")
        return None


# Bump when the parsing/selection logic changes in a way that should
# invalidate cached geometry (store.py refetches anything older).
GEOMETRY_VERSION = 2


def _hole_ref(tags: dict) -> Optional[int]:
    """Hole number from a golf=hole way. Prefers the `ref` tag; falls back to
    a number in the name ("Hole 1") because some mappers only fill that in."""
    ref = str(tags.get("ref") or "").strip()
    if ref.isdigit() and 1 <= int(ref) <= 18:
        return int(ref)
    m = re.search(r"\b(\d{1,2})\b", str(tags.get("name") or ""))
    if m and 1 <= int(m.group(1)) <= 18:
        return int(m.group(1))
    return None


def select_course_holes(
    candidates: list,
    expected_pars: Optional[list] = None,
) -> dict:
    """Pick one hole polygon per hole number from a set that may contain
    several courses' worth (36-hole clubs, neighbouring courses inside the
    search radius). Returns {ref: candidate}.

    Each candidate: {"ref": int, "par": int|None, "centroid": (lat, lng),
                     "distance_to_course": m, ...}.

    Two passes. First, anything unambiguous: a lone candidate for its
    number, or the single one whose par matches the scorecard we loaded
    from the course API. Second, the collisions (two par-4 7th holes): pick
    the one nearest its already-resolved neighbours — hole N's polygon sits
    beside hole N-1's and N+1's on the same course, and a few hundred yards
    from the other course's. Falls back to distance from the course
    coordinate only when nothing else can decide.
    """
    by_ref: dict = {}
    for c in candidates:
        by_ref.setdefault(c["ref"], []).append(c)

    def expected(ref):
        if expected_pars and 1 <= ref <= len(expected_pars):
            return expected_pars[ref - 1]
        return None

    chosen: dict = {}
    unresolved: dict = {}
    for ref, cands in by_ref.items():
        if len(cands) == 1:
            chosen[ref] = cands[0]
            continue
        want = expected(ref)
        # A candidate with no par tag could be ours — an unknown never loses
        # to a known match, it goes to the proximity check alongside it.
        unknown = [c for c in cands if c.get("par") is None]
        matching = [c for c in cands if want is not None and c.get("par") == want]
        if len(matching) == 1 and not unknown:
            chosen[ref] = matching[0]
        else:
            unresolved[ref] = (matching + unknown) or cands

    # Resolve collisions nearest their neighbours. Loop until every hole is
    # placed: each pass anchors on what's resolved so far, and if nothing can
    # be anchored (no par data at all, say) the lowest unresolved hole is
    # seeded by distance from the course coordinate and chaining resumes.
    while unresolved:
        progressed = False
        for ref in sorted(unresolved):
            anchors = [chosen[n]["centroid"] for n in (ref - 1, ref + 1) if n in chosen]
            if not anchors:
                continue
            chosen[ref] = min(
                unresolved.pop(ref),
                key=lambda c: sum(haversine_m(c["centroid"], a) for a in anchors),
            )
            progressed = True
        if not progressed:
            ref = min(unresolved)
            chosen[ref] = min(unresolved.pop(ref), key=lambda c: c["distance_to_course"])
    return chosen


def fetch_course_geometry(
    course_lat: float,
    course_lng: float,
    radius_m: int = 2000,
    expected_pars: Optional[list] = None,
) -> dict:
    """Return parsed hole-level geometry for the course at (lat, lng).

    expected_pars is the loaded scorecard's par per hole (18 ints). It lets
    a 36-hole club's two courses be told apart — the course API pins both
    to the same clubhouse coordinate, so proximity alone can't.

    Shape:
      {
        "version": GEOMETRY_VERSION,
        "has_data": bool,
        "hole_count": int,
        "holes": {
          "1": {"tee": [lat, lng], "green": [lat, lng], "bearing_deg": 287,
                "distance_yd": 410, "par": 4},
          ...
        }
      }
    On Overpass failure, returns {"has_data": False, "hole_count": 0, "holes": {}}.
    """
    q = f"""
    [out:json][timeout:45];
    (
      way["golf"="hole"](around:{radius_m},{course_lat},{course_lng});
      way["golf"="tee"](around:{radius_m},{course_lat},{course_lng});
      way["golf"="green"](around:{radius_m},{course_lat},{course_lng});
      node["golf"="pin"](around:{radius_m},{course_lat},{course_lng});
    );
    out tags geom;
    """
    resp = _overpass(q, timeout=60)
    if not resp:
        return {"has_data": False, "hole_count": 0, "holes": {}}

    holes_raw, tees_raw, greens_raw, pins_raw = [], [], [], []
    for el in resp.get("elements", []):
        golf_tag = (el.get("tags") or {}).get("golf")
        if golf_tag == "hole":
            holes_raw.append(el)
        elif golf_tag == "tee":
            tees_raw.append(el)
        elif golf_tag == "green":
            greens_raw.append(el)
        elif golf_tag == "pin":
            pins_raw.append(el)

    # The radius usually captures more than our course — a 36-hole club's
    # other eighteen, or a neighbour's (Pebble + Spyglass + Cypress). Every
    # candidate gets its par and centroid; select_course_holes picks one
    # polygon per hole number.
    course_pt = (course_lat, course_lng)
    holes_with_centroids = []
    for h in holes_raw:
        geom = h.get("geometry") or []
        if not geom:
            continue
        poly = [(g["lat"], g["lon"]) for g in geom]
        c = centroid(poly)
        if c is None:
            continue
        tags = h.get("tags") or {}
        ref = _hole_ref(tags)
        if ref is None:
            continue
        par_raw = str(tags.get("par") or "")
        holes_with_centroids.append({
            "ref": ref,
            "par": int(par_raw) if par_raw.isdigit() else None,
            "polygon": poly,
            "centroid": c,
            "distance_to_course": haversine_m(c, course_pt),
            "tags": tags,
        })

    by_ref = select_course_holes(holes_with_centroids, expected_pars)

    if not by_ref:
        return {"has_data": False, "hole_count": 0, "holes": {}}

    # Pair tees/greens to their hole via spatial containment in the hole polygon.
    # Some courses tag a `ref` on tees/greens too — when they do, we trust the
    # explicit tag over geometry. Otherwise we fall back to containment.
    holes_out: dict = {}
    for ref, h in sorted(by_ref.items()):
        hole_polygon = h["polygon"]
        # Find tees and greens that geometrically fall inside this hole's outline
        hole_tees = []
        for t in tees_raw:
            tag_ref = (t.get("tags") or {}).get("ref", "")
            t_geom = t.get("geometry") or []
            if not t_geom:
                continue
            t_poly = [(g["lat"], g["lon"]) for g in t_geom]
            t_centroid = centroid(t_poly)
            if t_centroid is None:
                continue
            if tag_ref.isdigit() and int(tag_ref) == ref:
                hole_tees.append(t_centroid)
            elif not tag_ref and point_in_polygon(t_centroid, hole_polygon):
                hole_tees.append(t_centroid)

        hole_greens = []
        for g_ in greens_raw:
            tag_ref = (g_.get("tags") or {}).get("ref", "")
            g_geom = g_.get("geometry") or []
            if not g_geom:
                continue
            g_poly = [(p["lat"], p["lon"]) for p in g_geom]
            g_centroid = centroid(g_poly)
            if g_centroid is None:
                continue
            if tag_ref.isdigit() and int(tag_ref) == ref:
                hole_greens.append(g_centroid)
            elif not tag_ref and point_in_polygon(g_centroid, hole_polygon):
                hole_greens.append(g_centroid)

        tee_pt = centroid(hole_tees) if hole_tees else None
        green_pt = centroid(hole_greens) if hole_greens else None

        # Fallback: if no separately-tagged tee/green were found inside the
        # hole polygon, use the two extreme ends of the hole polygon itself.
        # Many courses outline the hole as a long thin corridor — its longest
        # axis is roughly tee → green.
        if tee_pt is None or green_pt is None:
            tee_pt2, green_pt2 = _approx_tee_green_from_hole_polygon(hole_polygon)
            tee_pt = tee_pt or tee_pt2
            green_pt = green_pt or green_pt2

        if tee_pt is None or green_pt is None:
            continue

        bearing = bearing_deg(tee_pt, green_pt)
        distance_m = haversine_m(tee_pt, green_pt)

        holes_out[str(ref)] = {
            "tee": [round(tee_pt[0], 6), round(tee_pt[1], 6)],
            "green": [round(green_pt[0], 6), round(green_pt[1], 6)],
            "bearing_deg": round(bearing, 1),
            "distance_yd": int(round(distance_m * 1.0936)),
            "par": h.get("par"),
        }

    return {
        "version": GEOMETRY_VERSION,
        "has_data": len(holes_out) > 0,
        "hole_count": len(holes_out),
        "holes": holes_out,
    }


def _approx_tee_green_from_hole_polygon(polygon: list) -> tuple:
    """When a hole polygon doesn't have a separate tee/green tagged inside,
    approximate tee and green as the two points on the polygon farthest from
    each other. Most golf=hole polygons are long thin corridors from tee to
    green, so the major axis is a decent proxy."""
    if len(polygon) < 2:
        return None, None
    n = len(polygon)
    max_dist = 0
    pair = (polygon[0], polygon[1])
    # Quadratic in polygon size, but golf hole polygons are typically <50 pts
    for i in range(n):
        for j in range(i + 1, n):
            d = haversine_m(polygon[i], polygon[j])
            if d > max_dist:
                max_dist = d
                pair = (polygon[i], polygon[j])
    # We don't know which end is the tee vs green from the polygon alone —
    # caller can disambiguate using the hole's tag or fall back to assuming
    # the first returned point is the tee.
    return pair[0], pair[1]


# ────────────────────────────────────────────────────────────
# Relative wind — the actual feature
# ────────────────────────────────────────────────────────────
def compute_relative_wind(
    hole_bearing_deg: float,
    wind_from_compass: Optional[str],
    wind_speed_str: Optional[str],
) -> Optional[dict]:
    """Decompose live wind into headwind/tailwind + crosswind components
    relative to a player standing on the tee facing the green.

    Conventions:
      headwind_mph: + into the player's face, - at their back (tailwind)
      crosswind_mph: + wind FROM the LEFT (pushes ball right),
                     - wind FROM the RIGHT (pushes ball left)

    Returns None if any input is missing/invalid."""
    wind_from_deg = compass_to_deg(wind_from_compass)
    speed = parse_wind_speed_mph(wind_speed_str)
    if wind_from_deg is None or speed is None or hole_bearing_deg is None or speed < 1:
        return None

    # NWS reports the direction the wind is blowing FROM. Convert to the
    # direction the air is moving TOWARD (add 180°).
    wind_motion_deg = (wind_from_deg + 180.0) % 360.0
    # Relative angle = how the wind motion sits vs the player's forward direction
    rel = (wind_motion_deg - hole_bearing_deg + 360.0) % 360.0
    rad = math.radians(rel)
    # Component along player's forward axis (positive = tailwind)
    along = speed * math.cos(rad)
    # Component along player's right axis (positive = blowing to the right
    # = wind FROM the LEFT). sin(rel=90°) = +1 = wind moving to player's right.
    across = speed * math.sin(rad)

    headwind = -along  # flip so positive = into-face
    crosswind = across  # positive = from the left

    parts = []
    if headwind >= 3:
        parts.append(f"into your face {int(round(headwind))} mph")
    elif headwind <= -3:
        parts.append(f"at your back {int(round(abs(headwind)))} mph")

    if crosswind >= 3:
        parts.append(f"cross from the left {int(round(crosswind))} mph")
    elif crosswind <= -3:
        parts.append(f"cross from the right {int(round(abs(crosswind)))} mph")

    if not parts:
        description = f"calm relative to the hole ({int(round(speed))} mph total)"
    else:
        description = " + ".join(parts)

    return {
        "headwind_mph": int(round(headwind)),
        "crosswind_mph": int(round(crosswind)),
        "speed_mph": int(round(speed)),
        "wind_from_compass": (wind_from_compass or "").upper(),
        "hole_bearing_deg": int(round(hole_bearing_deg)),
        "description": description,
    }


def gps_yards_to_green(
    player_lat: float,
    player_lng: float,
    green: Optional[list],
) -> Optional[int]:
    """Distance in yards from the player's GPS fix to a hole's green center.
    Returns None when out of plausible on-hole range (<5 or >700 yards) —
    beyond that the player is probably not on the hole we think they're on,
    and a confidently wrong number is worse than none."""
    if not green or len(green) < 2:
        return None
    dist_yd = haversine_m((player_lat, player_lng), (green[0], green[1])) * 1.0936
    if dist_yd < 5 or dist_yd > 700:
        return None
    return int(round(dist_yd))


def format_gps_yardage_context(gy: Optional[dict]) -> str:
    """Render the GPS-computed distance to the green as a prompt block.
    Empty string when unavailable."""
    if not gy:
        return ""
    yards = gy.get("yards_to_green")
    hole = gy.get("hole")
    return (
        f"\n=== GPS YARDAGE (hole {hole}) ===\n"
        f"The player's phone GPS puts them approximately {yards} yards from the "
        f"CENTER of the green.\n"
        f"Use this as the working distance when the player doesn't state one — "
        f"do NOT ask 'how far have you got?' when this block is present. If the "
        f"player states a yardage, theirs wins (they may be lasering the pin, "
        f"not the center). Phone GPS is accurate to roughly ±10 yards, so treat "
        f"it as 'about {yards}', never as exact.\n"
    )


def format_relative_wind_context(rw: Optional[dict], hole_number: Optional[int]) -> str:
    """Render relative wind as a prompt-context block for Claude. Returns
    empty string if no data — caller should fall back to ask-the-player rules."""
    if not rw:
        return ""
    hole_str = f"hole {hole_number}" if hole_number else "the current hole"
    return (
        f"\n=== COMPUTED RELATIVE WIND ({hole_str}) ===\n"
        f"Wind: {rw['description']}\n"
        f"Hole bearing: {rw['hole_bearing_deg']}° "
        f"(player faces this direction off the tee)\n"
        f"Surface wind: {rw['speed_mph']} mph from {rw['wind_from_compass']}\n"
        f"THIS IS AUTHORITATIVE — derived from the live wind direction "
        f"and the hole's actual orientation. Use it in your recommendation. "
        f"Do NOT ask the player how the wind is hitting them.\n"
    )
