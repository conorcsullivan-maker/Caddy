"""Tests for the geometry + relative-wind math in caddy_geo.py.

The wind decomposition conventions are easy to silently flip (FROM vs
TOWARD, left vs right cross) — these tests pin them down with physically
obvious cases.
"""
import pytest

from caddy_geo import (
    bearing_deg,
    compass_to_deg,
    compute_relative_wind,
    format_gps_yardage_context,
    format_relative_wind_context,
    gps_yards_to_green,
    haversine_m,
    parse_wind_speed_mph,
    point_in_polygon,
)


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
