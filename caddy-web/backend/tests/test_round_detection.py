"""Tests for the natural-language detection layer in caddy_round.py.

These are the regex/keyword fast paths that handle the vast majority of
on-course messages. The git history is full of one-at-a-time regression
fixes to this code (Whisper homophones, negations, 'par 3 hole' false
positives) — every case here locks one of those in.
"""
import pytest

from caddy_round import (
    _extract_hole_number,
    apply_score_to_round_state,
    calculate_handicap,
    compute_round_status,
    detect_and_log_score,
    detect_remaining_yardage,
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
