"""Tests for the natural-language detection layer in caddy_round.py.

These are the regex/keyword fast paths that handle the vast majority of
on-course messages. The git history is full of one-at-a-time regression
fixes to this code (Whisper homophones, negations, 'par 3 hole' false
positives) — every case here locks one of those in.
"""
import pytest

from datetime import datetime, timedelta, timezone

from caddy_round import (
    _extract_hole_number,
    apply_score_to_round_state,
    calculate_handicap,
    compute_round_status,
    detect_and_log_score,
    detect_approach_shot,
    detect_gps_shot,
    detect_remaining_yardage,
    extract_club_mention,
    extract_miss_direction,
    find_tee,
    infer_drive_distance,
    is_end_of_round,
    might_mention_course,
)
from conftest import make_round_state


# ────────────────────────────────────────────────────────────
# Hole-number extraction
# ────────────────────────────────────────────────────────────
class TestExtractHoleNumber:
    def test_digit_forms(self):
        assert _extract_hole_number("I got a birdie on 5") == 5
        assert _extract_hole_number("bogey on hole 12") == 12
        assert _extract_hole_number("par on the 7th") == 7

    def test_word_forms(self):
        assert _extract_hole_number("double on the seventh") == 7
        assert _extract_hole_number("birdie on eighteen") == 18

    def test_whisper_homophones(self):
        # Voice transcription turns "on two" into "on too" etc.
        assert _extract_hole_number("birdie on too") == 2
        assert _extract_hole_number("bogey on won") == 1

    def test_last_mention_wins_after_self_correction(self):
        assert _extract_hole_number("I forgot 5, but on hole 4 I got a birdie") == 4

    def test_out_of_range_rejected(self):
        assert _extract_hole_number("on 19 I guess") is None
        assert _extract_hole_number("on 0") is None

    def test_no_mention(self):
        assert _extract_hole_number("I got a birdie") is None


# ────────────────────────────────────────────────────────────
# End-of-round detection
# ────────────────────────────────────────────────────────────
class TestEndOfRound:
    @pytest.mark.parametrize("phrase", [
        "round complete",
        "that's a wrap",
        "save the round",
        "I'm done for the day",
        "we're finished",
        "OK Caddy, end of round",
    ])
    def test_positive(self, phrase):
        assert is_end_of_round(phrase) is True

    @pytest.mark.parametrize("phrase", [
        "done with this hole",
        "finished the front nine",
        "I'm done talking about that shot",
        "what a round so far",
    ])
    def test_negative(self, phrase):
        assert is_end_of_round(phrase) is False


# ────────────────────────────────────────────────────────────
# Score detection — fast paths only (Haiku fallback is stubbed to fail)
# ────────────────────────────────────────────────────────────
class TestScoreDetection:
    def test_bare_birdie_on_current_hole(self, round_state):
        round_state["current_hole"] = 2  # par 4
        r = detect_and_log_score("birdie", round_state)
        assert r == {"hole": 2, "score": 3, "par": 4}

    def test_birdie_on_named_hole(self, round_state):
        round_state["current_hole"] = 6
        r = detect_and_log_score("I made a birdie on 3", round_state)  # hole 3 = par 3
        assert r == {"hole": 3, "score": 2, "par": 3}

    def test_double_bogey_beats_double(self, round_state):
        round_state["current_hole"] = 4  # par 5
        r = detect_and_log_score("ugh, double bogey", round_state)
        assert r == {"hole": 4, "score": 7, "par": 5}

    def test_snowman_is_always_eight(self, round_state):
        round_state["current_hole"] = 3  # par 3
        r = detect_and_log_score("snowman", round_state)
        assert r["score"] == 8

    def test_hole_in_one_on_named_hole(self, round_state):
        r = detect_and_log_score("hole in one on the seventh!", round_state)
        assert r == {"hole": 7, "score": 1, "par": 3}

    def test_negated_hole_in_one_not_logged(self, round_state):
        # Falls through to the (stubbed, failing) Haiku call → None
        assert detect_and_log_score("I didn't get a hole-in-one", round_state) is None

    def test_explicit_number_with_verb(self, round_state):
        round_state["current_hole"] = 5
        r = detect_and_log_score("I shot 5 there", round_state)
        assert r == {"hole": 5, "score": 5, "par": 4}

    def test_word_number(self, round_state):
        round_state["current_hole"] = 1
        r = detect_and_log_score("had an eight, brutal", round_state)
        assert r["score"] == 8

    def test_n_over(self, round_state):
        round_state["current_hole"] = 8  # par 5
        r = detect_and_log_score("2 over on that one", round_state)
        assert r == {"hole": 8, "score": 7, "par": 5}

    def test_made_par(self, round_state):
        round_state["current_hole"] = 9  # par 4
        r = detect_and_log_score("made par", round_state)
        assert r == {"hole": 9, "score": 4, "par": 4}

    def test_par_3_hole_description_is_not_a_score(self, round_state):
        # "it's a par 3 hole" describes the hole, doesn't report a score
        assert detect_and_log_score("it's a par 3 hole with water left", round_state) is None

    def test_ordinary_chat_ignored(self, round_state):
        assert detect_and_log_score("what club should I hit here?", round_state) is None


# ────────────────────────────────────────────────────────────
# Score application / cursor movement
# ────────────────────────────────────────────────────────────
class TestApplyScore:
    def test_advances_cursor_at_current_hole(self, round_state):
        apply_score_to_round_state(round_state, 1, 4)
        assert round_state["current_hole"] == 2
        assert round_state["hole_scores"][0] == 4

    def test_backfill_does_not_move_cursor(self, round_state):
        round_state["current_hole"] = 6
        apply_score_to_round_state(round_state, 3, 4)
        assert round_state["current_hole"] == 6
        assert round_state["hole_scores"][2] == 4

    def test_skipped_holes_padded_with_none(self, round_state):
        apply_score_to_round_state(round_state, 4, 6)
        assert round_state["hole_scores"] == [None, None, None, 6]
        assert round_state["current_hole"] == 5


# ────────────────────────────────────────────────────────────
# Round status (the anti-hallucination source of truth)
# ────────────────────────────────────────────────────────────
class TestRoundStatus:
    def test_no_scores(self, round_state):
        assert compute_round_status(round_state) is None

    def test_even_par(self, round_state):
        round_state["hole_scores"] = [4, 4]  # pars 4,4
        assert compute_round_status(round_state) == "even par through 2 holes"

    def test_over_par(self, round_state):
        round_state["hole_scores"] = [5, 4, 4]  # pars 4,4,3 → +2
        assert compute_round_status(round_state) == "2-over par through 3 holes"

    def test_under_par(self, round_state):
        round_state["hole_scores"] = [3, 3]  # pars 4,4 → -2
        assert compute_round_status(round_state) == "2-under par through 2 holes"

    def test_gap_is_called_out(self, round_state):
        round_state["hole_scores"] = [4, None, 3]  # skipped hole 2
        status = compute_round_status(round_state)
        assert "still need to log hole 2" in status
        assert "2 holes logged" in status


# ────────────────────────────────────────────────────────────
# Drive inference
# ────────────────────────────────────────────────────────────
class TestDriveInference:
    def test_remaining_yardage_simple(self):
        assert detect_remaining_yardage("165 to the pin") == 165
        assert detect_remaining_yardage("I have 145") == 145

    def test_smallest_number_wins(self):
        # "400 yard hole, 150 left" → 150 is the remaining
        assert detect_remaining_yardage("400 yard hole, 150 left") == 150

    def test_out_of_range_ignored(self):
        assert detect_remaining_yardage("I shot a 12 on that 700 yarder") is None

    def test_infer_drive(self, round_state):
        # Hole 1 yardage is 350 → 150 remaining = 200-yard drive
        r = infer_drive_distance("150 to the pin", round_state)
        assert r == {"hole": 1, "hole_yardage": 350, "remaining": 150, "inferred_drive": 200}

    def test_implausible_drive_rejected(self, round_state):
        # 350-yard hole, 320 remaining → 30-yard "drive" → noise, reject
        assert infer_drive_distance("320 to the pin", round_state) is None

    def test_no_course_no_inference(self):
        assert infer_drive_distance("150 to the pin", {"current_hole": 1}) is None


# ────────────────────────────────────────────────────────────
# Approach-shot detection ("hit 7-iron from 145" → shot_stats)
# ────────────────────────────────────────────────────────────
class TestApproachShotDetection:
    def test_basic_from_form(self):
        r = detect_approach_shot("hit 7 iron from 150")
        assert r == {"club": "7-iron", "distance": 150, "direction": None}

    def test_word_number_and_about(self):
        r = detect_approach_shot("just hit my seven iron from about 155")
        assert r["club"] == "7-iron"
        assert r["distance"] == 155

    def test_hyphenated_club(self):
        assert detect_approach_shot("hit 4-iron from 200")["club"] == "4-iron"

    def test_wedges_use_trackman_labels(self):
        assert detect_approach_shot("hit pitching wedge from 110")["club"] == "Pitching wedge"
        assert detect_approach_shot("hit sand wedge from 80")["club"] == "Sand wedge"

    def test_woods_and_hybrids(self):
        assert detect_approach_shot("hit 3 wood from 240")["club"] == "3-wood"
        assert detect_approach_shot("hit a hybrid from 210")["club"] == "Hybrid"
        assert detect_approach_shot("took 4 hybrid from 205")["club"] == "4-hybrid"

    def test_bare_number_after_club(self):
        # "hit 8 iron 140" — no "from" but the number follows the club
        r = detect_approach_shot("hit 8 iron 140")
        assert r == {"club": "8-iron", "distance": 140, "direction": None}

    def test_colloquial_bare_number_club(self):
        # "hit a 7 from 155" — number-only club, trusted because "from" is present
        r = detect_approach_shot("hit a 7 from 155")
        assert r["club"] == "7-iron"
        assert r["distance"] == 155

    def test_miss_left(self):
        assert detect_approach_shot("pulled my 8 iron from 140")["direction"] == "left"
        assert detect_approach_shot("hit 6 iron from 175, hooked it")["direction"] == "left"

    def test_miss_right(self):
        assert detect_approach_shot("blocked the 6 iron from 175")["direction"] == "right"

    def test_on_target(self):
        r = detect_approach_shot("flushed a 4 iron from 200, stuck it to 6 feet")
        assert r["direction"] == "center"

    def test_question_not_logged(self):
        assert detect_approach_shot("should I hit 7 iron from 150?") is None
        assert detect_approach_shot("7 iron or the 8 from 150") is None
        assert detect_approach_shot("what do I hit from 150") is None

    def test_intent_not_logged(self):
        assert detect_approach_shot("I'm going to hit 7 iron from 150") is None
        assert detect_approach_shot("thinking 8 iron from 160") is None

    def test_no_verb_not_logged(self):
        assert detect_approach_shot("7 iron from 150") is None

    def test_remaining_yardage_is_not_a_shot_distance(self):
        # "150 left" after driver = yards remaining, not a 150-yard drive
        assert detect_approach_shot("hit driver, 150 left") is None

    def test_out_of_range_distance(self):
        assert detect_approach_shot("hit 7 iron from 20") is None
        assert detect_approach_shot("hit driver from 400") is None

    def test_ordinary_chat(self):
        assert detect_approach_shot("what club should I hit here?") is None
        assert detect_approach_shot("made par on 3") is None

    def test_club_report_does_not_log_a_score(self, round_state):
        # The score detector's "took N" pattern must not swallow "took 6 iron"
        assert detect_and_log_score("took 6 iron from 180", round_state) is None
        # ...but a real "took N" score still logs
        round_state["current_hole"] = 1
        assert detect_and_log_score("took 6 there", round_state)["score"] == 6


# ────────────────────────────────────────────────────────────
# GPS-diff shot detection (rung 2 of automatic tracking)
# ────────────────────────────────────────────────────────────
def _fix(lat=42.0, lng=-71.0, hole=7, age_seconds=120):
    ts = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
    return {"lat": lat, "lng": lng, "hole": hole, "ts": ts.isoformat()}


class TestGpsShotDetection:
    # ~1 degree latitude = 111.2 km; 150 yd ≈ 137 m ≈ 0.001234°
    LAT_150YD = 42.0 + 150 / 1.0936 / 111_200

    def test_plausible_move_same_hole(self):
        r = detect_gps_shot(_fix(), self.LAT_150YD, -71.0, current_hole=7)
        assert r is not None
        assert abs(r["distance"] - 150) <= 2

    def test_different_hole_rejected(self):
        assert detect_gps_shot(_fix(hole=6), self.LAT_150YD, -71.0, current_hole=7) is None

    def test_jitter_sized_move_rejected(self):
        near = 42.0 + 15 / 1.0936 / 111_200  # ~15 yd
        assert detect_gps_shot(_fix(), near, -71.0, current_hole=7) is None

    def test_cart_ride_sized_move_rejected(self):
        far = 42.0 + 500 / 1.0936 / 111_200  # ~500 yd
        assert detect_gps_shot(_fix(), far, -71.0, current_hole=7) is None

    def test_stale_fix_rejected(self):
        old = _fix(age_seconds=45 * 60)  # 45 minutes
        assert detect_gps_shot(old, self.LAT_150YD, -71.0, current_hole=7) is None

    def test_missing_inputs(self):
        assert detect_gps_shot(None, self.LAT_150YD, -71.0, 7) is None
        assert detect_gps_shot(_fix(), None, None, 7) is None
        assert detect_gps_shot({"lat": 42.0, "lng": -71.0, "hole": 7}, self.LAT_150YD, -71.0, 7) is None  # no ts


class TestClubMention:
    def test_standard_clubs(self):
        assert extract_club_mention("the 7 iron") == "7-iron"
        assert extract_club_mention("hit sand wedge") == "Sand wedge"
        assert extract_club_mention("that was the 3 wood") == "3-wood"

    def test_bare_number_short_answer(self):
        assert extract_club_mention("the 7") == "7-iron"
        assert extract_club_mention("7") == "7-iron"

    def test_bare_number_needs_short_message(self):
        # Long messages with a bare digit are not club answers
        assert extract_club_mention("I think the wind is coming off the water on 7") is None

    def test_yardage_numbers_not_clubs(self):
        assert extract_club_mention("150 out") is None

    def test_question_about_next_shot_rejected(self):
        assert extract_club_mention("should I hit 9 iron now?") is None

    def test_no_club(self):
        assert extract_club_mention("on the green") is None


class TestMissDirection:
    def test_directions(self):
        assert extract_miss_direction("pulled it a bit") == "left"
        assert extract_miss_direction("blocked it out") == "right"
        assert extract_miss_direction("stuck it pin high") == "center"
        assert extract_miss_direction("that'll do") is None


# ────────────────────────────────────────────────────────────
# Handicap (WHS)
# ────────────────────────────────────────────────────────────
class TestHandicap:
    def test_fewer_than_three_rounds(self):
        assert calculate_handicap([{"differential": 10.0}]) is None

    def test_three_rounds_uses_best_minus_two(self):
        rounds = [{"differential": d} for d in (10.0, 12.0, 14.0)]
        # n=3 → best 1, adjustment -2.0 → (10.0 - 2.0) * 0.96 = 7.68 → 7.7
        assert calculate_handicap(rounds) == 7.7

    def test_rounds_without_differential_skipped(self):
        rounds = [{"differential": None}, {"score": 90}, {"differential": 8.0}]
        assert calculate_handicap(rounds) is None  # only 1 usable differential


# ────────────────────────────────────────────────────────────
# Course-mention pre-filter (the Haiku latency gate)
# ────────────────────────────────────────────────────────────
class TestMightMentionCourse:
    def test_mid_round_chatter_skipped_when_course_loaded(self):
        assert might_mention_course("165 to the pin, wind at my back", True) is False
        assert might_mention_course("made a mess of that one", True) is False

    def test_explicit_phrases_pass(self):
        assert might_mention_course("we're playing the back nine at pebble", True) is True
        assert might_mention_course("actually we're at a different course", True) is True

    def test_proper_noun_pair_passes(self):
        assert might_mention_course("Just got to Butter Brook", True) is True
        assert might_mention_course("teeing it up at Worcester Country Club", False) is True

    def test_permissive_when_no_course_loaded(self):
        # bare "at " is enough before a course is loaded
        assert might_mention_course("i'm at pebble today", False) is True


# ────────────────────────────────────────────────────────────
# Tee selection
# ────────────────────────────────────────────────────────────
class TestFindTee:
    def _course(self):
        return {"tees": {"male": [
            {"tee_name": "BLACK", "total_yards": 7100},
            {"tee_name": "BLUE", "total_yards": 6700},
            {"tee_name": "WHITE/BLUE", "total_yards": 6500},
            {"tee_name": "WHITE", "total_yards": 6300},
        ]}}

    def test_exact_match_beats_combo(self):
        assert find_tee(self._course(), "white")["tee_name"] == "WHITE"

    def test_combo_substring_match(self):
        course = {"tees": {"male": [{"tee_name": "WHITE/BLUE", "total_yards": 6500}]}}
        assert find_tee(course, "blue")["tee_name"] == "WHITE/BLUE"

    def test_default_is_second_longest(self):
        assert find_tee(self._course())["tee_name"] == "BLUE"

    def test_no_tees(self):
        assert find_tee({"tees": {"male": []}}) is None
