"""Check a produced IntegrationIntent + ArbiterPlan against a case's `expect`.

Pure (no DB, no LLM): the real-Grok runner produces (intent, plan); this decides
pass/fail, so the GATE LOGIC itself is unit-testable in CI even though the model
run is opt-in. Returns a list of human-readable failures ([] == pass).

Matching is deliberately lenient where the model has latitude and strict where a
regression hides:
- entity/object names match flexibly (casefold, either-substring) — the agent's
  mention_ref naming varies ("Celine" vs "Celine Hopkins").
- predicates match after registry normalization (synonyms collapse).
- a fact's `value` is checked against its value_json BARE datum, and value_json
  being None when a value was expected is the sentence-regression — a hard fail.

A failure string prefixed "advisory:" is an ADVISORY-ONLY miss (the tightened
`max_facts_advisory` bound landing uncalibrated): the runner reports it but
never hard-fails the case on it, even when the case is otherwise a hard gate.
"""

from __future__ import annotations

from typing import Any

from jbrain.analysis.arbiter import ArbiterPlan
from jbrain.analysis.intent import IntegrationIntent, IntentFact
from jbrain.schema import get_registry
from tests.eval.cases import UNSET, Case, CommittedFact, DbCommit, ExpectFact


def _norm(predicate: str) -> str:
    return get_registry().normalize_predicate(predicate)


def _name_match(want: str, got: str | None) -> bool:
    if got is None:
        return False
    a, b = want.casefold().strip(), got.casefold().strip()
    return a == b or a in b or b in a


def _bare(value_json: dict[str, Any] | None) -> Any:
    """The bare datum a value_json carries — its 'value' key, else the dict."""
    if isinstance(value_json, dict) and "value" in value_json:
        return value_json["value"]
    return value_json


def _value_match(want: Any, value_json: dict[str, Any] | None) -> bool:
    if isinstance(want, dict):
        # Subset match: every key the case names must match (tolerates extra keys).
        return isinstance(value_json, dict) and all(value_json.get(k) == v for k, v in want.items())
    got = _bare(value_json)
    if isinstance(want, str) and isinstance(got, str):
        return want.casefold().strip() == got.casefold().strip()
    return want == got


def _fact_matches_spec(fact: IntentFact, spec: dict[str, Any]) -> bool:
    """Loose match for absent_facts: only the keys present in `spec` are checked."""
    if "entity" in spec and not _name_match(spec["entity"], fact.entity_ref):
        return False
    if "predicate" in spec and _norm(spec["predicate"]) != fact.predicate:
        return False
    if "object" in spec and not _name_match(spec["object"], fact.object_entity_ref):
        return False
    return not ("assertion" in spec and fact.assertion != spec["assertion"])


def _find_fact(intent: IntegrationIntent, ef: ExpectFact) -> IntentFact | None:
    pred = _norm(ef.predicate)
    for f in intent.facts:
        if not _name_match(ef.entity, f.entity_ref) or f.predicate != pred:
            continue
        if ef.qualifier and f.qualifier != ef.qualifier:
            continue
        if ef.object is not None and not _name_match(ef.object, f.object_entity_ref):
            continue
        return f
    return None


def check_case(case: Case, intent: IntegrationIntent, plan: ArbiterPlan) -> list[str]:
    fails: list[str] = []
    ex = case.expect

    # Plan disposition keyed by fact identity (plan.facts aligns with intent.facts).
    status_by_id = {id(pf.fact): pf.status for pf in plan.facts}

    # --- resolutions ---
    for er in ex.resolutions:
        match = next(
            (r for r in intent.entity_resolutions if _name_match(er.mention, r.mention_ref)), None
        )
        if match is None:
            fails.append(f"resolution for {er.mention!r} missing")
            continue
        if er.mode and match.mode != er.mode:
            fails.append(f"{er.mention!r}: mode {match.mode!r} != {er.mode!r}")
        if er.entity_id and match.proposed_entity_id != er.entity_id:
            fails.append(
                f"{er.mention!r}: entity_id {match.proposed_entity_id!r} != {er.entity_id!r}"
            )
        if er.cross_subject is not None and match.cross_subject != er.cross_subject:
            fails.append(
                f"{er.mention!r}: cross_subject {match.cross_subject} != {er.cross_subject}"
            )

    # --- forbidden entities (a name minted as its own NEW entity) ---
    # Directional: flag only when the MINTED name equals or contains the forbidden
    # phrase (so a legit "Celine" doesn't trip on a forbidden "Celine Kitina
    # Hopkins" by mere substring).
    for name in ex.forbidden_entities:
        nlow = name.casefold().strip()
        for r in intent.entity_resolutions:
            if r.mode != "new":
                continue
            minted = (r.new_name or r.mention_ref or "").casefold().strip()
            if minted == nlow or nlow in minted:
                fails.append(
                    f"forbidden entity minted: {name!r} (as {r.new_name or r.mention_ref!r})"
                )
                break

    # --- entity-count bound (the no-duplicate / no-junk-entity gate) ---
    if ex.max_entities is not None:
        non_owner = [
            r
            for r in intent.entity_resolutions
            if r.proposed_entity_id != "owner-1"
            and (r.new_name or r.mention_ref or "").casefold() != "me"
        ]
        if len(non_owner) > ex.max_entities:
            minted = [r.new_name or r.mention_ref for r in non_owner]
            fails.append(f"too many entities: {len(non_owner)} > max {ex.max_entities} ({minted})")

    # --- required facts ---
    for ef in ex.facts:
        f = _find_fact(intent, ef)
        if f is None:
            fails.append(f"expected fact {ef.entity}.{ef.predicate} not found")
            continue
        if ef.value is not UNSET:
            if f.value_json is None:
                fails.append(
                    f"{ef.entity}.{ef.predicate}: value_json is None"
                    f" (sentence regression?), expected {ef.value!r}"
                )
            elif not _value_match(ef.value, f.value_json):
                fails.append(
                    f"{ef.entity}.{ef.predicate}: value {_bare(f.value_json)!r} != {ef.value!r}"
                )
        if ef.kind and f.kind != ef.kind:
            fails.append(f"{ef.entity}.{ef.predicate}: kind {f.kind!r} != {ef.kind!r}")
        if ef.assertion and f.assertion != ef.assertion:
            fails.append(
                f"{ef.entity}.{ef.predicate}: assertion {f.assertion!r} != {ef.assertion!r}"
            )
        if ef.inferred is not None and f.inferred != ef.inferred:
            fails.append(f"{ef.entity}.{ef.predicate}: inferred {f.inferred} != {ef.inferred}")
        if ef.disposition:
            want = "active" if ef.disposition == "commit" else "pending_review"
            got = status_by_id.get(id(f))
            if got != want:
                fails.append(
                    f"{ef.entity}.{ef.predicate}: disposition {got!r} != {want!r}"
                    f" ({ef.disposition})"
                )

    # --- facts that must NOT appear ---
    for spec in ex.absent_facts:
        if any(_fact_matches_spec(f, spec) for f in intent.facts):
            fails.append(f"forbidden fact present: {spec}")

    # --- supersession proposals ---
    for spec in ex.supersede:
        pred = _norm(spec["predicate"])
        if not any(
            _name_match(spec["entity"], s.entity_ref) and s.predicate == pred
            for s in intent.supersession_proposals
        ):
            fails.append(
                f"expected supersede proposal {spec['entity']}.{spec['predicate']} missing"
            )

    # --- over-extraction bound ---
    if ex.max_facts is not None and len(intent.facts) > ex.max_facts:
        fails.append(f"too many facts: {len(intent.facts)} > max {ex.max_facts}")
    if ex.max_facts_advisory is not None and len(intent.facts) > ex.max_facts_advisory:
        fails.append(
            f"advisory: facts {len(intent.facts)} > tightened bound {ex.max_facts_advisory}"
            " (uncalibrated — reports only)"
        )

    return fails


# --- DB-mode: assert on COMMITTED rows, not proposals -------------------------


def _committed_match(ef: ExpectFact, facts: tuple[CommittedFact, ...]) -> CommittedFact | None:
    pred = _norm(ef.predicate)
    for f in facts:
        if not _name_match(ef.entity, f.entity_name) or _norm(f.predicate) != pred:
            continue
        if ef.qualifier and f.qualifier != ef.qualifier:
            continue
        if ef.object is not None and not _name_match(ef.object, f.object_name):
            continue
        return f
    return None


def _committed_matches_spec(f: CommittedFact, spec: dict[str, Any]) -> bool:
    if "entity" in spec and not _name_match(spec["entity"], f.entity_name):
        return False
    if "predicate" in spec and _norm(spec["predicate"]) != _norm(f.predicate):
        return False
    if "object" in spec and not _name_match(spec["object"], f.object_name):
        return False
    return not ("assertion" in spec and f.assertion != spec["assertion"])


def check_case_db(case: Case, commit: DbCommit) -> list[str]:
    """The DB-mode gate: assert a case's `expect` against the COMMITTED graph
    (dispositions as active/pending_review rows + cards, supersession closure,
    resolve-to-existing onto the seeded UUID, domain floors on the row). Pure over
    the DbCommit, so it is unit-testable without Postgres."""
    fails: list[str] = []
    ex = case.expect
    seeded_vals = set(commit.seeded_ids.values())

    # Entities this note REFERENCED that are neither the owner nor a seed = newly
    # minted. The no-duplicate / no-junk-entity gate works off this set.
    new_ents = {
        eid: name
        for eid, name in commit.entities.items()
        if eid != commit.owner_id and eid not in seeded_vals
    }

    # --- forbidden entities (a name minted as its own row) ---
    for name in ex.forbidden_entities:
        nlow = name.casefold().strip()
        for minted in new_ents.values():
            m = (minted or "").casefold().strip()
            if m == nlow or nlow in m:
                fails.append(f"forbidden entity committed: {name!r} (as {minted!r})")
                break

    # --- entity-count bound ---
    if ex.max_entities is not None and len(new_ents) > ex.max_entities:
        names_seen = list(new_ents.values())
        fails.append(f"too many entities: {len(new_ents)} > max {ex.max_entities} ({names_seen})")

    # --- resolve-to-existing: a known mention must NOT fork a new row ---
    for er in ex.resolutions:
        if er.mode != "existing" or not er.entity_id:
            continue
        target = (
            commit.owner_id if er.entity_id == "owner-1" else commit.seeded_ids.get(er.entity_id)
        )
        if target is None:
            continue  # case didn't seed this id; nothing to verify in DB mode
        # A fork is a NEW entity carrying the mention's own name (exact, not the
        # loose substring _name_match — else short mentions like "Me" trip on any
        # word that contains them, e.g. "metformin").
        m = er.mention.casefold().strip()
        forked = [
            name for eid, name in new_ents.items() if name.casefold().strip() == m and eid != target
        ]
        if forked:
            fails.append(
                f"{er.mention!r}: forked a new entity {forked} instead of resolving to existing"
            )

    # --- required committed facts ---
    for ef in ex.facts:
        f = _committed_match(ef, commit.facts)
        if f is None:
            fails.append(f"expected committed fact {ef.entity}.{ef.predicate} not found")
            continue
        if ef.value is not UNSET:
            if f.value_json is None:
                fails.append(
                    f"{ef.entity}.{ef.predicate}: value_json is None"
                    f" (sentence regression?), expected {ef.value!r}"
                )
            elif not _value_match(ef.value, f.value_json):
                fails.append(
                    f"{ef.entity}.{ef.predicate}: value {_bare(f.value_json)!r} != {ef.value!r}"
                )
        if ef.kind and f.kind != ef.kind:
            fails.append(f"{ef.entity}.{ef.predicate}: kind {f.kind!r} != {ef.kind!r}")
        if ef.assertion and f.assertion != ef.assertion:
            fails.append(
                f"{ef.entity}.{ef.predicate}: assertion {f.assertion!r} != {ef.assertion!r}"
            )
        if ef.domain and f.domain_code != ef.domain:
            fails.append(f"{ef.entity}.{ef.predicate}: domain {f.domain_code!r} != {ef.domain!r}")
        if ef.former is not None:
            is_former = f.valid_to is not None  # closed interval = FORMER
            if is_former != ef.former:
                fails.append(
                    f"{ef.entity}.{ef.predicate}: former={is_former} (valid_to={f.valid_to!r}),"
                    f" expected former={ef.former}"
                )
        if ef.disposition:
            has_card = f.id in commit.review_fact_ids
            if ef.disposition == "commit" and (f.status != "active" or has_card):
                fails.append(
                    f"{ef.entity}.{ef.predicate}: expected active commit, got"
                    f" status={f.status!r} card={has_card}"
                )
            elif ef.disposition == "review" and (f.status != "pending_review" or not has_card):
                fails.append(
                    f"{ef.entity}.{ef.predicate}: expected pending_review + card, got"
                    f" status={f.status!r} card={has_card}"
                )

    # --- facts that must NOT be committed ---
    for spec in ex.absent_facts:
        if any(_committed_matches_spec(f, spec) for f in commit.facts):
            fails.append(f"forbidden committed fact present: {spec}")

    # --- supersession EFFECT: the prior seeded edge is actually closed ---
    for spec in ex.supersede:
        pred = _norm(spec["predicate"])
        prior = [
            s
            for s in commit.seeded_facts
            if _name_match(spec["entity"], s.entity_name) and _norm(s.predicate) == pred
        ]
        if not prior:
            fails.append(f"no seeded prior fact for supersede {spec['entity']}.{spec['predicate']}")
        elif not any(s.status == "superseded" and s.superseded_by for s in prior):
            fails.append(
                f"prior {spec['entity']}.{spec['predicate']} not superseded"
                f" (status={[s.status for s in prior]})"
            )

    # --- firewall floor: committed facts landed in the right domain_code ---
    if ex.committed_domains:
        counts: dict[str, int] = {}
        for f in commit.facts:
            counts[f.domain_code or ""] = counts.get(f.domain_code or "", 0) + 1
        for dom, need in ex.committed_domains.items():
            if counts.get(dom, 0) < need:
                fails.append(
                    f"domain floor: {counts.get(dom, 0)} committed facts in {dom!r} < {need}"
                )

    # --- review cards (the new_predicate canonicalization card, Phase 4) ---
    for spec in ex.review_cards:
        kind = spec.get("kind")
        pred = spec.get("predicate")
        need = spec.get("min_suggestions", 0)
        match = next(
            (
                c
                for c in commit.review_cards
                if c.kind == kind
                and (pred is None or _name_match(pred, c.predicate))
                and len(c.suggestions) >= need
            ),
            None,
        )
        if match is None:
            fails.append(
                f"expected review card {kind!r} for {pred!r} (>= {need} suggestions) missing"
            )

    # --- review cards that must NOT be filed (tier-2 commits raw, card-free) ---
    for spec in ex.absent_review_cards:
        kind = spec.get("kind")
        pred = spec.get("predicate")
        hits = [
            c
            for c in commit.review_cards
            if (kind is None or c.kind == kind) and (pred is None or _name_match(pred, c.predicate))
        ]
        if hits:
            fails.append(f"forbidden review card filed: {spec} ({len(hits)} matched)")

    # --- over-extraction bound (committed facts this note wrote) ---
    if ex.max_facts is not None and len(commit.facts) > ex.max_facts:
        fails.append(f"too many committed facts: {len(commit.facts)} > max {ex.max_facts}")
    if ex.max_facts_advisory is not None and len(commit.facts) > ex.max_facts_advisory:
        fails.append(
            f"advisory: committed facts {len(commit.facts)} > tightened bound"
            f" {ex.max_facts_advisory} (uncalibrated — reports only)"
        )

    return fails
