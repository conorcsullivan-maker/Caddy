"""Shared test setup.

Two jobs:
1. Make the backend modules importable with fake API keys (caddy_engine
   refuses to import without them, and Conor's shell exports an EMPTY
   ANTHROPIC_API_KEY that would otherwise make caddy_round skip its
   fast paths entirely).
2. Guarantee no test ever hits the network: the Anthropic client in
   caddy_round is replaced with a stub that raises on use, so any test
   that accidentally falls through a regex fast path into the Haiku
   fallback fails loudly inside the module's try/except and returns None
   instead of making a real API call.
"""
import os
import sys

# Force-set (not setdefault): the empty ANTHROPIC_API_KEY from Claude
# Desktop's shell config must be overridden, and tests must never run
# against real keys regardless of the local environment.
os.environ["ANTHROPIC_API_KEY"] = "sk-test-not-a-real-key"
os.environ["OPENAI_API_KEY"] = "sk-test-not-a-real-key"
os.environ["GOLF_COURSE_API_KEY"] = ""

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

import caddy_round  # noqa: E402


class _NetworkDisabledMessages:
    @staticmethod
    def create(*args, **kwargs):
        raise RuntimeError("Network calls are disabled in tests")


class _NetworkDisabledClient:
    """Truthy (so `if not anthropic_client` guards pass and fast paths run)
    but raises if any code path actually tries to call the API."""
    messages = _NetworkDisabledMessages()


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    monkeypatch.setattr(caddy_round, "anthropic_client", _NetworkDisabledClient())


# Standard 18-hole test course: front nine pars 4,4,3,5,4,4,3,5,4 mirrored
# on the back. Yardages 350..367 so drive-inference math is predictable.
DEFAULT_PARS = [4, 4, 3, 5, 4, 4, 3, 5, 4] * 2


def make_round_state(pars=None, current_hole=1, hole_scores=None):
    pars = pars or DEFAULT_PARS
    holes = [
        {"par": p, "yardage": 350 + i, "handicap": i + 1}
        for i, p in enumerate(pars)
    ]
    return {
        "course": {"id": 1, "club_name": "Test Golf Club"},
        "tee": {
            "tee_name": "WHITE",
            "course_rating": 71.0,
            "slope_rating": 128,
            "total_yards": sum(h["yardage"] for h in holes),
            "holes": holes,
        },
        "hole_scores": hole_scores or [],
        "current_hole": current_hole,
    }


@pytest.fixture
def round_state():
    return make_round_state()
