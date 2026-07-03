"""The pathology-narrative diagnosis extraction (docs/plans/EMR_IMPORT_PLAN.md
§6.5) — the ONE LLM touch on the otherwise-deterministic structured EMR path.

The bone-marrow surgical-pathology report is prose, not fields: it is chunked,
embedded, and FTS-indexed as narrative (searchable, cited) and NEVER shredded
into hundreds of facts (the ANALYSIS.md guard). Only its "Final Diagnosis" line
yields a *small, high-confidence* set of diagnoses. This module runs that one
extraction through the LLM adapter (never a provider SDK, non-neg #1) and returns
typed candidates; `importer.lower_parse_result` lowers the committable ones into
`encounterDiagnosis` edges.

Two gates keep the set small and honest, both applied at lowering, not here:
`ruled_out` diagnoses ("cannot rule out an evolving marrow process") stay
hypothetical and are never emitted as a diagnosis fact, and a diagnosis below the
model's own confidence floor is left in the prose rather than committed. The call
is fail-soft: an unrouted task or an unusable response yields no diagnoses (the
structured labs/encounters — the valuable deterministic data — still commit, and
the narrative is still searchable); the pathology set is a bonus, never a gate.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import structlog

from jbrain.llm import LlmBadResponseError, LlmError, LlmRouter
from jbrain.llm.promptfile import load_prompt

log = structlog.get_logger()

_PROMPT = load_prompt(Path(__file__).parent / "prompts" / "pathology_diagnosis.prompt")
PATHOLOGY_TASK = _PROMPT.name
PATHOLOGY_STRENGTH = _PROMPT.strength
PATHOLOGY_SCHEMA = _PROMPT.output_schema
_MAX_TOKENS = int(_PROMPT.config.get("max_tokens", 2048))

# A diagnosis the model reports below this self-confidence stays prose, never a
# fact (§6.5 "gated by model self-confidence"). Sits above the arbiter's inferred
# ceiling so the gate — not a downstream weight surprise — decides what commits.
CONFIDENCE_FLOOR = 0.75


@dataclass(frozen=True)
class PathologyDiagnosis:
    """One diagnosis parsed from a pathology report's Final Diagnosis line."""

    condition: str
    icd10: str | None
    ruled_out: bool
    confidence: float

    @property
    def committable(self) -> bool:
        """A diagnosis becomes a graph fact only when the report AFFIRMS it (not a
        rule-out) and the model is confident enough (§6.5)."""
        return not self.ruled_out and self.confidence >= CONFIDENCE_FLOOR


async def extract_pathology_diagnoses(
    router: LlmRouter, narrative: str
) -> list[PathologyDiagnosis]:
    """Extract the Final-Diagnosis set from a pathology narrative via the LLM
    adapter. Returns [] for empty prose, an unrouted task (the probe mirrors
    `_disambiguate`), or an unusable response — the narrative stays searchable and
    the deterministic import is unaffected either way."""
    narrative = (narrative or "").strip()
    if not narrative:
        return []
    try:
        router.spec(PATHOLOGY_TASK)  # routability probe; unrouted -> skip the LLM
    except LlmError:
        log.info("emr.pathology_unrouted")
        return []
    try:
        result = await router.complete(
            PATHOLOGY_TASK,
            system="",
            user_text=_PROMPT.render()
            + "\n\n<pathology_report>\n"
            + narrative
            + "\n</pathology_report>",
            json_schema=PATHOLOGY_SCHEMA,
            max_tokens=_MAX_TOKENS,
            strength=PATHOLOGY_STRENGTH,
        )
    except (LlmError, LlmBadResponseError) as exc:
        log.warning("emr.pathology_extract_failed", error=repr(exc))
        return []
    return _parse(result.parsed)


def _parse(payload: object) -> list[PathologyDiagnosis]:
    """Coerce the model payload into typed candidates, dropping any malformed
    entry rather than trusting the model's shape (a nameless or non-numeric
    diagnosis is discarded, never guessed into a fact)."""
    if not isinstance(payload, dict):
        return []
    rows = payload.get("diagnoses")
    if not isinstance(rows, list):
        return []
    out: list[PathologyDiagnosis] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        condition = row.get("condition")
        if not isinstance(condition, str) or not condition.strip():
            continue
        icd10 = row.get("icd10")
        confidence = row.get("confidence")
        if not isinstance(confidence, (int, float)) or isinstance(confidence, bool):
            continue
        out.append(
            PathologyDiagnosis(
                condition=condition.strip(),
                icd10=icd10.strip() if isinstance(icd10, str) and icd10.strip() else None,
                ruled_out=bool(row.get("ruled_out")),
                confidence=max(0.0, min(1.0, float(confidence))),
            )
        )
    return out
