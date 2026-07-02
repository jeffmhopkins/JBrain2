"""Unit tests for the pure neighborhood BFS engine (Wave 3 T3.1).

No DB: a fake fetch callback serves per-hop edge batches from in-memory edge
lists, and we prove the engine's own contract — first-visit-wins dedup with
one connecting path per node, hop stamping, depth clamping, per-hop ranking
(refs above co-mentions, co-mentions by count then recency), hub-note
damping, the total cap, and mentions-only note assembly.
"""

from collections.abc import Sequence
from datetime import UTC, datetime

from jbrain.analysis.neighborhood import (
    MAX_DEPTH,
    CoMentionEdge,
    EdgeBatch,
    EntityRef,
    FetchEdges,
    NeighborEntity,
    NoteMention,
    RefEdge,
    assemble_notes,
    traverse,
)


def ent(eid: str, name: str | None = None, *, kind: str = "person") -> EntityRef:
    return EntityRef(id=eid, name=name or eid, kind=kind, domain="general")


def at(day: int) -> datetime:
    return datetime(2026, 6, day, 12, 0, tzinfo=UTC)


def make_fetch(
    refs: Sequence[RefEdge] = (), co_mentions: Sequence[CoMentionEdge] = ()
) -> FetchEdges:
    """Serve exactly the edges whose source is in the requested frontier."""

    async def fetch(frontier: Sequence[str], hop: int) -> EdgeBatch:
        wanted = set(frontier)
        return EdgeBatch(
            refs=tuple(e for e in refs if e.src_id in wanted),
            co_mentions=tuple(e for e in co_mentions if e.src_id in wanted),
        )

    return fetch


def by_id(result: list[NeighborEntity]) -> dict[str, NeighborEntity]:
    return {e.id: e for e in result}


ME = ent("me", "Me")


async def test_anchor_only_when_no_edges() -> None:
    result = await traverse(ME, make_fetch())
    assert [(e.id, e.hop, e.path) for e in result] == [("me", 0, "Me")]


async def test_path_reconstruction_mixed_edge_kinds() -> None:
    """The plan's canonical example: Me -spouse-> Celine -co-mention(note X)->
    Dr. Patel — one connecting path per node, hop-stamped, both edge kinds and
    both ref directions rendered."""
    celine, patel, kid = ent("celine", "Celine"), ent("patel", "Dr. Patel"), ent("kid", "Kid")
    result = await traverse(
        ME,
        make_fetch(
            refs=[
                RefEdge("me", celine, "spouse"),
                # An inbound fact (Kid -parent-> Me) renders with the arrow flipped.
                RefEdge("me", kid, "parent", direction="in"),
            ],
            co_mentions=[CoMentionEdge("celine", patel, "note-x", 3, noted_at=at(10))],
        ),
        depth=2,
    )
    got = by_id(result)
    assert got["celine"].hop == 1 and got["celine"].path == "Me -spouse-> Celine"
    assert got["kid"].path == "Me <-parent- Kid"
    assert got["patel"].hop == 2
    assert got["patel"].path == "Me -spouse-> Celine -co-mention(note note-x)-> Dr. Patel"
    assert got["patel"].kind == "person" and got["patel"].domain == "general"


async def test_first_visit_wins_on_diamond() -> None:
    """A -> B and A -> C, both -> D: D appears once, at hop 2, with its single
    parent chosen by rank (B's edge to D is newer than C's)."""
    b, c, d = ent("b", "B"), ent("c", "C"), ent("d", "D")
    result = await traverse(
        ME,
        make_fetch(
            refs=[
                RefEdge("me", b, "friend", recency=at(20)),
                RefEdge("me", c, "friend", recency=at(10)),
                RefEdge("b", d, "colleague", recency=at(5)),
                RefEdge("c", d, "sibling", recency=at(1)),
            ]
        ),
        depth=3,
    )
    assert [e.id for e in result].count("d") == 1
    got = by_id(result)
    assert got["d"].hop == 2
    assert got["d"].path == "Me -friend-> B -colleague-> D"


async def test_first_visit_keeps_earliest_hop() -> None:
    """A node reached directly at hop 1 is never restamped by a hop-2 edge."""
    b, d = ent("b", "B"), ent("d", "D")
    result = await traverse(
        ME,
        make_fetch(
            refs=[
                RefEdge("me", b, "friend", recency=at(20)),
                RefEdge("me", d, "colleague", recency=at(10)),
                RefEdge("b", d, "sibling", recency=at(30)),
            ]
        ),
        depth=3,
    )
    got = by_id(result)
    assert got["d"].hop == 1
    assert got["d"].path == "Me -colleague-> D"


async def test_refs_rank_above_co_mentions_and_per_hop_limit_applies() -> None:
    """With per_hop_limit=2, the typed-ref candidate is admitted first even
    when the co-mention edges are fresher; only the best co-mention follows."""
    r, c1, c2 = ent("r", "R"), ent("c1", "C1"), ent("c2", "C2")
    result = await traverse(
        ME,
        make_fetch(
            refs=[RefEdge("me", r, "friend", recency=at(1))],
            co_mentions=[
                CoMentionEdge("me", c1, "n1", 2, noted_at=at(5)),
                CoMentionEdge("me", c1, "n2", 2, noted_at=at(6)),
                CoMentionEdge("me", c2, "n3", 2, noted_at=at(28)),
            ],
        ),
        depth=1,
        per_hop_limit=2,
    )
    assert [e.id for e in result] == ["me", "r", "c1"]


async def test_co_mentions_rank_by_count_desc_then_recency() -> None:
    """Two shared notes beat one fresher note; equal counts fall back to
    recency, newest first."""
    c1, c2, c3 = ent("c1", "C1"), ent("c2", "C2"), ent("c3", "C3")
    result = await traverse(
        ME,
        make_fetch(
            co_mentions=[
                CoMentionEdge("me", c1, "n1", 2, noted_at=at(1)),
                CoMentionEdge("me", c1, "n2", 2, noted_at=at(2)),
                CoMentionEdge("me", c2, "n3", 2, noted_at=at(28)),
                CoMentionEdge("me", c3, "n4", 2, noted_at=at(14)),
            ]
        ),
        depth=1,
    )
    assert [e.id for e in result] == ["me", "c1", "c2", "c3"]
    # The connecting note in the path is the freshest shared one.
    assert by_id(result)["c1"].path == "Me -co-mention(note n2)-> C1"


async def test_hub_note_is_not_expanded_through() -> None:
    """A 15-entity note explodes past hub_cap: none of its co-mentions become
    candidates. An entity ALSO reachable through a small note still gets in —
    damping is per note, not per entity."""
    guests = [ent(f"g{i}", f"Guest {i}") for i in range(14)]
    hub_edges = [CoMentionEdge("me", g, "hub-note", 15, noted_at=at(20)) for g in guests]
    rescue = CoMentionEdge("me", guests[3], "small-note", 2, noted_at=at(4))
    result = await traverse(ME, make_fetch(co_mentions=[*hub_edges, rescue]), depth=2)
    assert [e.id for e in result] == ["me", "g3"]
    assert by_id(result)["g3"].path == "Me -co-mention(note small-note)-> Guest 3"


async def test_hub_notes_are_excluded_from_co_mention_counts() -> None:
    """Ranking counts only non-hub shared notes: two hub shares + one small
    note must not outrank two small-note shares."""
    a, b = ent("a", "A"), ent("b", "B")
    result = await traverse(
        ME,
        make_fetch(
            co_mentions=[
                CoMentionEdge("me", a, "hub-1", 15, noted_at=at(20)),
                CoMentionEdge("me", a, "hub-2", 15, noted_at=at(21)),
                CoMentionEdge("me", a, "n1", 2, noted_at=at(22)),
                CoMentionEdge("me", b, "n2", 2, noted_at=at(1)),
                CoMentionEdge("me", b, "n3", 2, noted_at=at(2)),
            ]
        ),
        depth=1,
    )
    assert [e.id for e in result] == ["me", "b", "a"]


async def test_total_cap_counts_anchor_and_stops_traversal() -> None:
    others = [ent(f"p{i}", f"P{i}") for i in range(6)]
    refs = [RefEdge("me", o, "knows", recency=at(30 - i)) for i, o in enumerate(others)]
    result = await traverse(ME, make_fetch(refs=refs), depth=3, total_cap=3)
    assert len(result) == 3
    assert result[0].id == "me"


async def test_depth_clamps_to_valid_range() -> None:
    """depth<1 behaves as 1; an absurd depth clamps to MAX_DEPTH hops."""
    chain = [ent(f"h{i}", f"H{i}") for i in range(1, 6)]
    refs = [RefEdge("me", chain[0], "knows")] + [
        RefEdge(chain[i].id, chain[i + 1], "knows") for i in range(4)
    ]
    shallow = await traverse(ME, make_fetch(refs=refs), depth=0)
    assert {e.id for e in shallow} == {"me", "h1"}
    deep = await traverse(ME, make_fetch(refs=refs), depth=99)
    assert {e.id for e in deep} == {"me", "h1", "h2", "h3"}
    assert max(e.hop for e in deep) == MAX_DEPTH


async def test_fetch_receives_frontier_and_hop() -> None:
    """The callback is driven per hop with exactly the newly admitted frontier
    (an empty frontier ends the walk without another fetch)."""
    calls: list[tuple[tuple[str, ...], int]] = []
    b = ent("b", "B")

    async def fetch(frontier: Sequence[str], hop: int) -> EdgeBatch:
        calls.append((tuple(frontier), hop))
        if hop == 1:
            return EdgeBatch(refs=(RefEdge("me", b, "friend"),))
        return EdgeBatch()

    await traverse(ME, fetch, depth=3)
    assert calls == [(("me",), 1), (("b",), 2)]


def neighbor(eid: str, name: str, hop: int) -> NeighborEntity:
    return NeighborEntity(id=eid, name=name, kind="person", domain="general", hop=hop, path=name)


def test_assemble_notes_stamps_min_hop_and_connecting_names() -> None:
    entities = [neighbor("me", "Me", 0), neighbor("c", "Celine", 1), neighbor("p", "Dr. Patel", 2)]
    notes = assemble_notes(
        entities,
        [
            NoteMention("note-x", "c", noted_at=at(10)),
            NoteMention("note-x", "p", noted_at=at(10)),
            NoteMention("note-y", "p", noted_at=at(20)),
            NoteMention("note-z", "ghost", noted_at=at(25)),  # not in the set: ignored
        ],
    )
    assert [(n.note_id, n.hop, n.connects) for n in notes] == [
        ("note-x", 1, ("Celine", "Dr. Patel")),
        ("note-y", 2, ("Dr. Patel",)),
    ]


def test_assemble_notes_orders_hop_then_recency_and_caps() -> None:
    entities = [neighbor("me", "Me", 0), neighbor("c", "Celine", 1)]
    notes = assemble_notes(
        entities,
        [
            NoteMention("old-anchor", "me", noted_at=at(1)),
            NoteMention("new-anchor", "me", noted_at=at(20)),
            NoteMention("undated-anchor", "me", noted_at=None),
            NoteMention("hop1-note", "c", noted_at=at(28)),
        ],
        note_cap=3,
    )
    # Hop first (all anchor notes precede the fresher hop-1 note), then
    # newest-first with undated last; the cap trims the hop-1 note.
    assert [n.note_id for n in notes] == ["new-anchor", "old-anchor", "undated-anchor"]


def test_assemble_notes_empty_inputs() -> None:
    assert assemble_notes([], [NoteMention("n", "x")]) == []
    assert assemble_notes([neighbor("me", "Me", 0)], []) == []


async def test_degenerate_caps_still_return_the_anchor() -> None:
    """Caps are advertised as freely tunable for evals: a zero/negative cap
    clamps instead of producing an impossible empty result."""
    celine = ent("celine", "Celine")
    fetch = make_fetch(refs=[RefEdge("me", celine, "spouse")])
    zero_total = await traverse(ME, fetch, total_cap=0)
    assert [e.id for e in zero_total] == ["me"]
    zero_hop = await traverse(ME, fetch, per_hop_limit=-1)
    assert [e.id for e in zero_hop] == ["me"]


async def test_out_of_frontier_edge_fails_loudly() -> None:
    """The FetchEdges contract (src_id in the requested frontier) is enforced
    at admission, so a retrieval-layer bug surfaces as a clear contract error
    instead of an opaque KeyError at path-render time."""

    async def rogue(frontier: Sequence[str], hop: int) -> EdgeBatch:
        return EdgeBatch(refs=(RefEdge("never-traversed", ent("x"), "knows"),))

    import pytest

    with pytest.raises(ValueError, match="outside the traversed set"):
        await traverse(ME, rogue)
