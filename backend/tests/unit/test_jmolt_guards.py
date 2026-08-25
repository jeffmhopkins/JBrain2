"""Unit tests for jmolt's mechanical publish-time guards (M8/M9/M10)."""

from datetime import UTC, datetime, timedelta

import pytest

from jbrain.agent.jmolt_guards import (
    MAX_POSTS_PER_NIGHT,
    TooManyPostsError,
    clamp_publish_at,
    is_near_duplicate,
    lint_content,
)

# ---- M8 content lint -----------------------------------------------------


def test_lint_passes_ordinary_text() -> None:
    assert lint_content("I read your last post about tide pools. What changed your mind?").ok


@pytest.mark.parametrize(
    "bad",
    [
        "buy $MOLT now before the presale",
        "send to 0x1234567890abcdef1234567890abcdef12345678",
        "guaranteed returns, 100x, ape in",
        "check the airdrop, to the moon",
    ],
)
def test_lint_blocks_crypto_promotion(bad: str) -> None:
    assert not lint_content(bad).ok


@pytest.mark.parametrize(
    "bad",
    [
        "my key is moltbook_abc123def456",
        "here: sk_live_abcdefghij1234567890",
        "xprv" + "a" * 60,
    ],
)
def test_lint_blocks_secret_shapes(bad: str) -> None:
    assert not lint_content(bad).ok


def test_lint_blocks_zero_width_and_bidi() -> None:
    # A zero-width space and a right-to-left override, injected into otherwise-clean text.
    assert not lint_content(f"hello{chr(0x200B)}world").ok
    assert not lint_content(f"safe{chr(0x202E)}txet").ok
    assert not lint_content(f"a{chr(0xFEFF)}b").ok


# ---- M9 near-duplicate ---------------------------------------------------


def test_near_duplicate_catches_repeats() -> None:
    a = "The general submolt is loud and mostly noise tonight, as usual."
    b = "The general submolt is loud and mostly noise tonight, as always."
    assert is_near_duplicate(b, [a])


def test_near_duplicate_allows_distinct_posts() -> None:
    a = "The general submolt is loud and mostly noise tonight."
    b = "I spent the hour reading one thread about how agents pick names."
    assert not is_near_duplicate(b, [a])


def test_near_duplicate_empty_is_never_dup() -> None:
    assert not is_near_duplicate("", ["anything"])


# ---- M10 publish_at clamp ------------------------------------------------


def _t(h: int, m: int = 0) -> datetime:
    return datetime(2026, 8, 25, h, m, tzinfo=UTC)


def test_clamp_uses_floor_when_requested_is_past() -> None:
    now = _t(3, 0)
    out = clamp_publish_at(_t(1, 0), [], now)  # requested before now
    assert out == now


def test_clamp_enforces_min_gap_after_latest() -> None:
    now = _t(3, 0)
    out = clamp_publish_at(_t(3, 5), [_t(9, 0)], now)  # requested too close to 9:00
    assert out >= _t(9, 30)


def test_clamp_honors_a_valid_future_request() -> None:
    now = _t(3, 0)
    out = clamp_publish_at(_t(14, 0), [_t(9, 0)], now)
    assert out == _t(14, 0)


def test_clamp_rejects_when_night_is_full() -> None:
    now = _t(3, 0)
    existing = [now + timedelta(hours=i) for i in range(MAX_POSTS_PER_NIGHT)]
    with pytest.raises(TooManyPostsError):
        clamp_publish_at(_t(20, 0), existing, now)


def test_clamp_pins_within_the_local_day() -> None:
    now = _t(3, 0)
    out = clamp_publish_at(_t(23, 0), [_t(23, 50)], now)  # gap would spill past midnight
    assert out.date() == now.date()
    assert out <= datetime(2026, 8, 25, 23, 59, tzinfo=UTC)
