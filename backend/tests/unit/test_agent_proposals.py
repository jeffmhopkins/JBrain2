"""The Proposal tree logic (pure): containment cascade and dependency-safe
enactment (docs/reference/ASSISTANT.md "Staging & approval"). The DB repo is integration."""

from jbrain.agent.proposals import (
    Node,
    cascade_approve,
    cascade_reject,
    descendants,
    enactment_plan,
)


def leaf(node_id: str, parent: str | None = "root", status: str = "pending", deps=()) -> Node:
    return Node(node_id, parent, "leaf", status, tuple(deps))


def group(node_id: str, parent: str | None = None, status: str = "pending") -> Node:
    return Node(node_id, parent, "group", status, ())


# A small tree: root group → [a, b], where b depends on a.
TREE = [group("root"), leaf("a"), leaf("b", deps=("a",))]


class TestCascade:
    def test_descendants_walks_the_subtree(self) -> None:
        nodes = [group("root"), group("g", "root"), leaf("a", "g"), leaf("b", "root")]
        assert set(descendants(nodes, "root")) == {"g", "a", "b"}
        assert descendants(nodes, "g") == ["a"]

    def test_approve_cascades_to_descendants(self) -> None:
        changes = cascade_approve(TREE, "root")
        assert changes == {"root": "approved", "a": "approved", "b": "approved"}

    def test_approve_preserves_an_individually_rejected_descendant(self) -> None:
        nodes = [group("root"), leaf("a", status="rejected"), leaf("b")]
        changes = cascade_approve(nodes, "root")
        assert "a" not in changes  # an explicit rejection wins over the cascade
        assert changes == {"root": "approved", "b": "approved"}

    def test_reject_cascades_to_the_whole_subtree(self) -> None:
        assert cascade_reject(TREE, "root") == {
            "root": "rejected",
            "a": "rejected",
            "b": "rejected",
        }


class TestEnactmentPlan:
    def test_an_approved_leaf_with_no_deps_is_enactable(self) -> None:
        nodes = [group("root", status="approved"), leaf("a", status="approved")]
        plan = enactment_plan(nodes)
        assert plan.enactable == ("a",) and plan.held == ()

    def test_a_leaf_enacts_only_when_its_prereq_is_approved(self) -> None:
        nodes = [
            group("root", status="approved"),
            leaf("a", status="approved"),
            leaf("b", status="approved", deps=("a",)),
        ]
        plan = enactment_plan(nodes)
        assert set(plan.enactable) == {"a", "b"}

    def test_an_approved_leaf_with_a_rejected_prereq_is_held(self) -> None:
        # The fail-closed case: b is approved but its prereq a was rejected → held.
        nodes = [
            group("root", status="approved"),
            leaf("a", status="rejected"),
            leaf("b", status="approved", deps=("a",)),
        ]
        plan = enactment_plan(nodes)
        assert plan.enactable == () and plan.held == ("b",)

    def test_a_leaf_with_an_enacted_prereq_is_enactable(self) -> None:
        nodes = [leaf("a", None, "enacted"), leaf("b", None, "approved", deps=("a",))]
        assert enactment_plan(nodes).enactable == ("b",)

    def test_unapproved_and_group_nodes_are_neither(self) -> None:
        nodes = [group("root", status="approved"), leaf("a", status="pending")]
        plan = enactment_plan(nodes)
        assert plan.enactable == () and plan.held == ()

    def test_partial_approval_stays_consistent(self) -> None:
        # Owner approved the a-subtree but rejected c; b (needs a) enacts, d (needs c) holds.
        nodes = [
            leaf("a", None, "approved"),
            leaf("b", None, "approved", deps=("a",)),
            leaf("c", None, "rejected"),
            leaf("d", None, "approved", deps=("c",)),
        ]
        plan = enactment_plan(nodes)
        assert set(plan.enactable) == {"a", "b"}
        assert plan.held == ("d",)
