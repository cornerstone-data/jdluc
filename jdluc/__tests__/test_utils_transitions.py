"""Unit tests for scalar transition encoding (no GEE dependency).

Cross-form consistency between ``encode_transition`` and
``encode_transition_image`` is validated by the transform integration
test against a real exported Delaware ``land_use`` asset; no separate
consistency test is needed here.
"""

import pytest

from jdluc.utils.transitions import encode_transition


def test_no_transition_sentinel() -> None:
    """``encode_transition(a, a)`` collapses to the 0 sentinel for every ``a``."""
    for a in range(16):
        assert encode_transition(a, a) == 0


def test_known_values() -> None:
    """Sanity-check the bit layout against the spec's worked examples."""
    assert encode_transition(1, 5) == 0x15
    assert encode_transition(2, 8) == 0x28


def test_out_of_range_raises() -> None:
    """Catches accidental addition of a 17th category."""
    with pytest.raises(AssertionError):
        encode_transition(16, 0)
    with pytest.raises(AssertionError):
        encode_transition(0, 16)
    with pytest.raises(AssertionError):
        encode_transition(-1, 0)
    with pytest.raises(AssertionError):
        encode_transition(0, -1)
