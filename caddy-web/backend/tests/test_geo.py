"""Tests for the geometry + relative-wind math in caddy_geo.py.

The wind decomposition conventions are easy to silently flip (FROM vs
TOWARD, left vs right cross) — these tests pin them down with physically
obvious cases.
"""
import pytest

from caddy_geo import (
    _hole_ref,
    bearing_deg,
    compass_to_deg,
    compute_relative_wind,
    format_gps_yardage_context,
    format_relative_wind_context,
    gps_yards_to_green,
    haversine_m,
    parse_wind_speed_mph,
    point_in_polygon,
    select_course_holes,
)


# ────────────────────────────────────────────────────────────
# Multi-course hole selection — real OSM data from the Quechee Club, VT
# (36 holes at one clubhouse coordinate; every number 2-18 appears twice,
# six of them with identical pars; Highland's 1st is tagged only by name;
# Lakeland's 18th has no par tag at all).
# ────────────────────────────────────────────────────────────
QUECHEE_CLUBHOUSE = (43.6570521, -72.4403071)
# (ref, par, lat, lon)
QUECHEE_OSM_HOLES = [
    (1, 4, 43.6561, -72.4391), (2, 4, 43.6557, -72.4383), (2, 5, 43.6513, -72.4386),
    (3, 4, 43.6547, -72.4379), (3, 4, 43.6473, -72.4395), (4, 3, 43.6564, -72.4354),
    (4, 4, 43.6444, -72.4402), (5, 5, 43.6551, -72.4318), (5, 5, 43.6449, -72.4412),
    (6, 3, 43.6533, -72.4303), (6, 4, 43.6491, -72.4417), (7, 5, 43.6505, -72.4278),
    (7, 4, 43.6515, -72.4407), (8, 3, 43.6483, -72.4271), (8, 3, 43.6527, -72.4400),
    (9, 4, 43.6474, -72.4267), (9, 4, 43.6532, -72.4421), (10, 4, 43.6466, -72.4263),
    (10, 4, 43.6556, -72.4440), (11, 5, 43.6474, -72.4306), (11, 4, 43.6555, -72.4478),
    (12, 4, 43.6486, -72.4336), (12, 3, 43.6541, -72.4500), (13, 3, 43.6486, -72.4303),
    (13, 4, 43.6500, -72.4473), (14, 4, 43.6488, -72.4293), (14, 5, 43.6474, -72.4479),
    (15, 5, 43.6502, -72.4290), (15, 4, 43.6474, -72.4487), (16, 3, 43.6526, -72.4307),
    (16, 4, 43.6509, -72.4488), (17, 4, 43.6548, -72.4325), (17, 3, 43.6541, -72.4508),
    (18, None, 43.6549, -72.4359), (18, 5, 43.6562, -72.4463),
    (1, 3, 43.6540, -72.4409),  # the way tagged name="Hole 1", ref missing
]
LAKELAND_PARS = [4, 4, 4, 3, 5, 3, 5, 3, 4, 4, 5, 4, 3, 4, 5, 3, 4, 5]
HIGHLAND_PARS = [3, 5, 4, 4, 5, 4, 4, 3, 4, 4, 4, 3, 4, 5, 4, 4, 3, 5]


def _quechee_candidates():
    return [
        {"ref": ref, "par": par, "centroid": (lat, lon), "polygon": [],
         "distance_to_course": haversine_m((lat, lon), QUECHEE_CLUBHOUSE), "tags": {}}
        for ref, par, lat, lon in QUECHEE_OSM_HOLES
    ]


class TestMultiCourseHoleSelection:
    def _assert_one_course(self, chosen, pars):
        assert sorted(chosen) == list(range(1, 19))
        for ref, want in enumerate(pars, start=1):
            got = chosen[ref]["par"]
            assert got in (want, None), f"hole {ref}: par {got}, scorecard says {want}"
        # Consecutive holes on one course sit near each other; the other
        # course's same-numbered hole is a kilometre away.
        for ref in range(2, 19):
            gap = haversine_m(chosen[ref]["centroid"], chosen[ref - 1]["centroid"])
            assert gap < 700, f"hole {ref} is {gap:.0f} m from hole {ref - 1} — wrong course"

    def test_lakeland_resolves_to_one_course(self):
        chosen = select_course_holes(_quechee_candidates(), LAKELAND_PARS)
        self._assert_one_course(chosen, LAKELAND_PARS)
        # The untagged-par 18th must go to Lakeland by proximity, not lose
        # to Highland's tagged par-5 18th.
        assert chosen[18]["centroid"] == (43.6549, -72.4359)

    def test_highland_resolves_to_one_course(self):
        chosen = select_course_holes(_quechee_candidates(), HIGHLAND_PARS)
        self._assert_one_course(chosen, HIGHLAND_PARS)
        assert chosen[1]["centroid"] == (43.6540, -72.4409)  # the name-only "Hole 1"

    def test_two_courses_share_no_holes(self):
        lake = select_course_holes(_quechee_candidates(), LAKELAND_PARS)
        high = select_course_holes(_quechee_candidates(), HIGHLAND_PARS)
        overlap = [r for r in range(1, 19) if lake[r]["centroid"] == high[r]["centroid"]]
        assert overlap == []

    def test_no_scorecard_still_yields_a_coherent_course(self):
        # Without pars we can't know WHICH course, but chaining from the
        # nearest hole 1 must still produce one contiguous course, not a mix.
        chosen = select_course_holes(_quechee_candidates(), None)
        assert sorted(chosen) == list(range(1, 19))
        for ref in range(2, 19):
            assert haversine_m(chosen[ref]["centroid"], chosen[ref - 1]["centroid"]) < 700

    def test_single_course_untouched(self):
        cands = [c for c in _quechee_candidates() if c["centroid"][1] < -72.438]  # west course only
        chosen = select_course_holes(cands, HIGHLAND_PARS)
        assert len(chosen) == 18


class TestHoleRef:
    def test_ref_tag(self):
        assert _hole_ref({"ref": "7"}) == 7

    def test_name_fallback(self):
        assert _hole_ref({"name": "Hole 1"}) == 1
        assert _hole_ref({"ref": "0", "name": "Hole 12"}) == 12

    def test_out_of_range(self):
        assert _hole_ref({"ref": "19"}) is None
        assert _hole_ref({"name": "Practice green"}) is None


class TestCompass:
    def test_cardinals(self):
        assert compass_to_deg("N") == 0.0
        assert compass_to_deg("E") == 90.0
        assert compass_to_deg("S") == 180.0
        assert compass_to_deg("W") == 270.0

    def test_intercardinal_and_case(self):
        assert compass_to_deg("wsw") == 247.5
        assert compass_to_deg(" NNE ") == 22.5

    def test_invalid(self):
        assert compass_to_deg(None) is None
        assert compass_to_deg("NORTHISH") is None


class TestWindSpeedParse:
    def test_range_averages(self):
        assert parse_wind_speed_mph("5 to 10 mph") == 7.5

    def test_single_value(self):
        assert parse_wind_speed_mph("15 mph") == 15

    def test_garbage(self):
        assert parse_wind_speed_mph("calm") is None
        assert parse_wind_speed_mph(None) is None


class TestGeometryBasics:
    def test_haversine_one_degree_latitude(self):
        # 1° of latitude ≈ 111.2 km everywhere on Earth
        d = haversine_m((42.0, -71.0), (43.0, -71.0))
        assert d == pytest.approx(111_200, rel=0.01)

    def test_bearing_due_north_and_east(self):
        assert bearing_deg((42.0, -71.0), (43.0, -71.0)) == pytest.approx(0, abs=0.5)
        assert bearing_deg((42.0, -71.0), (42.0, -70.0)) == pytest.approx(90, abs=1.0)

    def test_point_in_polygon(self):
        square = [(0.0, 0.0), (0.0, 1.0), (1.0, 1.0), (1.0, 0.0)]
        assert point_in_polygon((0.5, 0.5), square) is True
        assert point_in_polygon((1.5, 0.5), square) is False

    def test_degenerate_polygon(self):
        assert point_in_polygon((0.5, 0.5), [(0, 0), (1, 1)]) is False


class TestRelativeWind:
    """Player faces the hole bearing. NWS reports where wind blows FROM."""

    def test_pure_headwind(self):
        # Facing north, wind from the north → straight into the face
        rw = compute_relative_wind(0.0, "N", "10 mph")
        assert rw["headwind_mph"] == 10
        assert rw["crosswind_mph"] == 0
        assert "into your face 10 mph" in rw["description"]

    def test_pure_tailwind(self):
        rw = compute_relative_wind(0.0, "S", "10 mph")
        assert rw["headwind_mph"] == -10
        assert "at your back 10 mph" in rw["description"]

    def test_cross_from_left(self):
        # Facing north, wind from the west → comes over the player's left shoulder
        rw = compute_relative_wind(0.0, "W", "10 mph")
        assert rw["crosswind_mph"] == 10
        assert rw["headwind_mph"] == 0
        assert "cross from the left" in rw["description"]

    def test_cross_from_right(self):
        rw = compute_relative_wind(0.0, "E", "10 mph")
        assert rw["crosswind_mph"] == -10
        assert "cross from the right" in rw["description"]

    def test_quartering_wind_on_rotated_hole(self):
        # Hole bearing 90° (due east), wind from NE 14 mph → part headwind,
        # part cross from the left.
        rw = compute_relative_wind(90.0, "NE", "14 mph")
        assert rw["headwind_mph"] == pytest.approx(10, abs=1)
        assert rw["crosswind_mph"] == pytest.approx(10, abs=1)

    def test_light_air_is_calm(self):
        rw = compute_relative_wind(0.0, "N", "2 mph")
        assert "calm relative to the hole" in rw["description"]

    def test_below_one_mph_returns_none(self):
        assert compute_relative_wind(0.0, "N", "0 mph") is None

    def test_missing_inputs_return_none(self):
        assert compute_relative_wind(None, "N", "10 mph") is None
        assert compute_relative_wind(0.0, None, "10 mph") is None
        assert compute_relative_wind(0.0, "N", None) is None

    def test_context_block_mentions_hole(self):
        rw = compute_relative_wind(0.0, "N", "10 mph")
        ctx = format_relative_wind_context(rw, 7)
        assert "hole 7" in ctx
        assert "AUTHORITATIVE" in ctx

    def test_empty_context_when_no_wind(self):
        assert format_relative_wind_context(None, 7) == ""


class TestGpsYardage:
    # Green ~183 m (200 yd) due north of the player
    PLAYER = (42.0, -71.0)
    GREEN_200YD = [42.0 + 183.0 / 111_200, -71.0]

    def test_basic_distance(self):
        yards = gps_yards_to_green(*self.PLAYER, self.GREEN_200YD)
        assert yards == pytest.approx(200, abs=2)

    def test_too_far_means_wrong_hole(self):
        # ~1100 yd away → player isn't on this hole, return None
        far_green = [42.0 + 0.009, -71.0]
        assert gps_yards_to_green(*self.PLAYER, far_green) is None

    def test_standing_on_green(self):
        assert gps_yards_to_green(42.0, -71.0, [42.0, -71.0]) is None

    def test_missing_green(self):
        assert gps_yards_to_green(42.0, -71.0, None) is None
        assert gps_yards_to_green(42.0, -71.0, [42.0]) is None

    def test_context_block(self):
        ctx = format_gps_yardage_context({"hole": 5, "yards_to_green": 152})
        assert "hole 5" in ctx
        assert "152 yards" in ctx
        assert "CENTER of the green" in ctx

    def test_empty_context(self):
        assert format_gps_yardage_context(None) == ""
