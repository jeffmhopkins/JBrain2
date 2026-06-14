"""Run one corpus case through the real production chain against real Grok.

extract -> integrate (graph-aware) -> plan_intent. Intent-level (no DB): the
case supplies its own `graph_context` string, so this runs anywhere the xAI
token is set, no Postgres needed. The committed-fact path (value_json -> DB) is
already covered deterministically by test_apply_intent_pg; here we exercise the
MODEL's judgment, which is where the production defects lived.
"""

from __future__ import annotations

from datetime import UTC, datetime

from jbrain.analysis.arbiter import ArbiterPlan, compute_signals, plan_intent
from jbrain.analysis.integrate import Integrator
from jbrain.analysis.intent import IntegrationIntent
from jbrain.analysis.pipeline import _extract_note
from jbrain.llm import LlmRouter
from tests.eval.cases import Case

_OWNER_LINE = "Owner/author: entity id 'owner-1' name 'Me' (Person)."


def _graph_context(case: Case) -> str:
    # Production's build_graph_context ALWAYS names the owner (get_or_create_me),
    # so the agent can resolve first person to owner-1. Mirror that: inject the
    # owner line unless the case already seeds it.
    ctx = case.graph_context.strip()
    if "owner-1" in ctx:
        return ctx
    return _OWNER_LINE if not ctx else f"{_OWNER_LINE}\n{ctx}"


async def run_case(router: LlmRouter, case: Case) -> tuple[IntegrationIntent, ArbiterPlan]:
    anchor = datetime.now(UTC)
    extraction = await _extract_note(
        router,
        [case.note_text],
        domain=case.domain,
        prompt_anchor=anchor,
        parse_anchor=anchor,
        note_id=case.id,
    )
    intent = await Integrator(router).integrate(
        note_id=case.id,
        extraction=extraction,
        graph_context=_graph_context(case),
        schema_version=1,
        note_text=case.note_text,
    )
    plan = plan_intent(intent, compute_signals(intent, [case.note_text]))
    return intent, plan
