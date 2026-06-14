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
"""

from __future__ import annotations

from typing import Any

from jbrain.analysis.arbiter import ArbiterPlan
from jbrain.analysis.intent import IntegrationIntent, IntentFact
from jbrain.schema import get_registry
from tests.eval.cases import UNSET, Case, ExpectFact


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

    return fails
