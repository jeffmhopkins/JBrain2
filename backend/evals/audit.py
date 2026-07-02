"""Offline pre-flight audit of the eval case set — NO model or API key needed.

Two deterministic checks that catch the common case-authoring bugs before a
(billed) live run, and keep the committed case set honest over time:

  - self-consistency: every asserted name / number / time-phrase actually
    appears in the note body (the model can only extract what's in the text),
    and no `absent_person` contradicts its own body.
  - temporal: every CLOSED-SET backward phrase resolves to the asserted LOCAL
    date via the real `resolve_relative_date` — the pipeline's own logic, so an
    off-by-one in a case is caught by the code that will run in production.

Run by hand:  cd backend && uv run python -m evals.audit
Enforced in CI via tests/unit/test_eval_scoring.py (test_eval_cases_pass_audit),
so a new or edited case that fails either check fails the suite.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from jbrain.analysis.extraction import resolve_relative_date
from jbrain.evals.runner import _norm, load_cases

# Generic words that legitimately appear in a body but must never become an
# entity — `absent_person` on these is correct even though the word is present.
_GENERIC = frozenset({"someone", "they", "somebody", "anyone", "everyone", "no one", "nobody"})


def _in_body(name: str, body: str) -> bool:
    return _norm(name) in _norm(body)


def audit_cases(cases: list[dict[str, Any]]) -> list[str]:
    """Return a list of human-readable issues; empty means the set is clean."""
    issues: list[str] = []
    for c in cases:
        body, expect, name = c["body"], c.get("expect", {}), c["name"]
        for key in ("person_mentions", "mentions", "not_person"):
            for nm in expect.get(key, []):
                if _norm(nm) != "me" and not _in_body(nm, body):
                    issues.append(f"{name}: {key} {nm!r} not in note body")
        for mk in expect.get("mention_kind", []):
            if not _in_body(mk["name"], body):
                issues.append(f"{name}: mention_kind {mk['name']!r} not in note body")
        for edge in expect.get("edges", []):
            if _norm(edge["object"]) != "me" and not _in_body(edge["object"], body):
                issues.append(f"{name}: edge object {edge['object']!r} not in note body")
        # An absent_edges object not in the body would pass vacuously — the check
        # only means something when the note actually names the thing.
        for spec in expect.get("absent_edges", []):
            if not _in_body(spec["object"], body):
                issues.append(f"{name}: absent_edges object {spec['object']!r} not in note body")
        for nm in expect.get("absent_person", []):
            if _norm(nm) not in _GENERIC and _in_body(nm, body):
                issues.append(f"{name}: absent_person {nm!r} is in the note body (contradiction)")
        for v in expect.get("value", []):
            if str(v["contains"]).lower() not in body.lower():
                issues.append(f"{name}: value {v['contains']!r} not in note body")
        for t in expect.get("temporal", []):
            if _norm(t["phrase"]) not in _norm(body):
                issues.append(f"{name}: temporal phrase {t['phrase']!r} not in note body")
            expected = resolve_relative_date(t["phrase"], datetime.fromisoformat(c["created_at"]))
            if expected is not None and expected.isoformat() != t["resolved_date"]:
                issues.append(
                    f"{name}: temporal {t['phrase']!r} resolves to {expected.isoformat()} "
                    f"(closed-set), but case asserts {t['resolved_date']}"
                )
    return issues


def main() -> int:
    cases = load_cases()
    issues = audit_cases(cases)
    for issue in issues:
        print(issue)
    print(f"\n{len(issues)} issue(s) over {len(cases)} cases")
    return 1 if issues else 0


if __name__ == "__main__":
    raise SystemExit(main())
