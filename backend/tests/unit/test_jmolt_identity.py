"""The jmolt night-identity block: supplies the agent its own Moltbook handle (its NAME
for the night) since the persona prompt frames "jmolt" only as the KIND of agent."""

from jbrain.agent.jmolt_night import _identity_block


def test_names_the_handle_as_the_agents_own_name() -> None:
    block = _identity_block("tidepool_jmolt")
    assert "@tidepool_jmolt" in block
    assert "That handle is your name" in block
    assert "You are the jmolt at @tidepool_jmolt" in block


def test_strips_a_leading_at_and_whitespace() -> None:
    # The stored handle may or may not carry an "@" — normalize so the block reads "@x", once.
    assert _identity_block("  @foo ") == _identity_block("foo")
    assert "@@" not in _identity_block("@foo")


def test_blank_when_no_handle_is_registered() -> None:
    # No handle → nothing injected; the persona's own "you are a jmolt" framing stands.
    assert _identity_block("") == ""
    assert _identity_block("   ") == ""
