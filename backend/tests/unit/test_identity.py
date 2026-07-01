"""The agent's owner-self anchor: the ambient `me_block` line that hands a
knowledge-base turn the "Me" entity id."""

from jbrain.agent.identity import _ME_FRAME, me_block


def test_me_block_is_data_framed_and_carries_the_id() -> None:
    block = me_block("7d381675-68af-4898-8101-82f870d5610a")
    assert block.startswith(_ME_FRAME)
    assert "7d381675-68af-4898-8101-82f870d5610a" in block


def test_me_block_points_at_read_entity_not_search_or_relate() -> None:
    """The whole point: an owner self-attribute is one read_entity on Me — never a
    note search first, never relate (which follows relationships to other people)."""
    block = me_block("abc")
    assert "read_entity" in block
    assert "don't search notes" in block
    assert "relate" in block
