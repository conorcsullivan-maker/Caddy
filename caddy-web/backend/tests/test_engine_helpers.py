"""Tests for the pure helpers in caddy_engine.py and caddy_trackman.py:
JSON fence stripping, the Whisper hallucination filter, custom club labels,
and confidence-tier boundaries.
"""
from caddy_engine import (
    _extract_json,
    _format_custom_club_key,
    is_likely_hallucination,
)
from caddy_trackman import shot_count_tier


class TestExtractJson:
    def test_plain_object(self):
        assert _extract_json('{"a": 1}') == {"a": 1}

    def test_haiku_markdown_fences(self):
        # The exact quirk convention #9 exists for: Haiku wraps JSON in fences
        assert _extract_json('```json\n{"name": "Butter Brook"}\n```') == {"name": "Butter Brook"}

    def test_fences_without_language_tag(self):
        assert _extract_json('```\n[1, 2, 3]\n```') == [1, 2, 3]

    def test_json_embedded_in_prose(self):
        assert _extract_json('Sure! Here it is: {"score": 4} — done.') == {"score": 4}

    def test_garbage_returns_none(self):
        assert _extract_json("no json here") is None
        assert _extract_json("") is None
        assert _extract_json(None) is None


class TestHallucinationFilter:
    def test_empty_and_noise(self):
        assert is_likely_hallucination("") is True
        assert is_likely_hallucination("...") is True
        assert is_likely_hallucination("Thank you.") is True

    def test_youtube_outro_phrases(self):
        assert is_likely_hallucination("Thanks for watching, see you next time!") is True
        assert is_likely_hallucination("Please subscribe to my channel") is True

    def test_foreign_script_noise(self):
        assert is_likely_hallucination("ご視聴ありがとうございました") is True

    def test_mid_sentence_fragment(self):
        # Short fragments starting with conjunctions/prepositions = Whisper
        # grasping at silence
        assert is_likely_hallucination("of armed worker states") is True
        assert is_likely_hallucination("and then the") is True

    def test_real_golf_speech_passes(self):
        assert is_likely_hallucination("What club should I hit from 150?") is False
        assert is_likely_hallucination("165 to the pin, wind at my back") is False
        assert is_likely_hallucination("I made a birdie on 3") is False

    def test_long_sentence_with_preposition_start_passes(self):
        # The mid-sentence-starter rule only applies to short transcripts
        assert is_likely_hallucination(
            "From the tee I want you to tell me what club to hit on this long par five"
        ) is False


class TestCustomClubLabels:
    def test_custom_prefix_stripped(self):
        assert _format_custom_club_key("custom_chipper") == "Chipper"

    def test_separators_prettified(self):
        assert _format_custom_club_key("custom_driving-iron") == "Driving iron"
        assert _format_custom_club_key("custom_2_iron") == "2 iron"

    def test_non_custom_key_passthrough(self):
        assert _format_custom_club_key("mystery") == "Mystery"


class TestConfidenceTiers:
    def test_boundaries(self):
        # SHOT_TIER_SMALL=10, MEDIUM=50, HIGH=250 — boundaries are inclusive
        assert shot_count_tier(0) == "TOO FEW SHOTS"
        assert shot_count_tier(9) == "TOO FEW SHOTS"
        assert shot_count_tier(10) == "LOW CONFIDENCE — small sample"
        assert shot_count_tier(49) == "LOW CONFIDENCE — small sample"
        assert shot_count_tier(50) == "MEDIUM CONFIDENCE"
        assert shot_count_tier(249) == "MEDIUM CONFIDENCE"
        assert shot_count_tier(250) == "HIGH CONFIDENCE"
