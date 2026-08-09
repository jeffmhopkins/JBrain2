# deep_research.py module split — break the orchestrator monolith apart

> **Status:** Proposed · **Last verified:** 2026-08-09

`backend/src/jbrain/agent/deep_research.py` has grown to ~2,300 lines. It holds the
`DeepResearchService` orchestrator AND every supporting concern around it — the citation
registry, the persona/mode routing, the report-view builders, the prompt schemas and
mechanical backstops, the depth/shape directives. That is too much for one file: it is hard
to navigate, every change touches a giant file, and (incidentally) the size makes it painful
to move through any whole-file channel. This is a **pure mechanical refactor** — moving code
into topic modules with **no behaviour change** — so it is low-risk if the moved surface is
re-exported and the full suite stays green.

## Why (and why it's a follow-up, not urgent)

- **Navigability.** The orchestration spine (`_run`, `_plan`/`_analyze`/`_reflect`/
  `_synthesize`/`_critique`) is buried among ~40 helper functions and a dozen constant blocks.
- **Blast radius.** A one-line change to a helper currently re-touches the 2,300-line file.
- **Not a fix for the git-push pain.** Smaller files make each *future* change cheaper to move,
  but the root cause of the recent friction was an unprovisioned `git push` in one container,
  not the file size. Do this in a session with working `git push`, where moving code between
  files is free and instantly verifiable — not through the whole-file GitHub API.

## The split

Keep `deep_research.py` as the orchestrator; move the supporting concerns out.

| Module | What moves in |
|---|---|
| `deep_research.py` (stays) | `DeepResearchService` (+ `research`/`produce`/`_run_preset`/`_run`, `_plan`/`_gather_staged`/`_analyze`/`_reflect`/`_curate_sources`/`_synthesize`/`_critique`/`_persist`/`_phase`/`_charge`), `DeepResearchRef`, `DeepProduceRef`, and the run-level constants the service reads (breadth knobs, `DR_*_RESERVE`, mode/round caps) |
| `research_sources.py` | the citation registry + P1.5 binding: `_canonical_url`, `_TRACKING_PARAMS`, `_host`, `_cosine`, `_collect_sources`, `_sources_block`, `_findings_block`, `_stage_feed`, `_source_index_map`, `_finding_source_markers`, `_cited_findings_block` |
| `research_directives.py` | `Directive`, `SourcePlan`, `_Stage`, source-mode/output-kind/sink constants, `_personas_for`, `_stage_persona`, `_supplement_clause`, `_can_open_sources`, `_verify_sources_note`, `_empty_gather_msg`, `_depth_directive`/`_shape_directive`/`_tool_tag` (+ their `_*_DIRECTIVE` tables) |
| `research_report_view.py` | `_frame`, `_report_view`, `_findings_count`, `_source_label` |
| `research_backstops.py` | `_PLAN_SCHEMA`, `_REFLECT_SCHEMA`, `_CITE_MARKER`, `_missing_headings`, `_missing_headings_critique`, `_ZERO_CITATION_CRITIQUE`, `_backstop_critique` |

(Group boundaries are guidance, not gospel — collapse two modules if one comes out thin. The
one hard rule is that `research_scratchpad.py` stays independent and nothing here imports the
service back, to keep the import graph acyclic: service → sources/directives/view/backstops,
never the reverse.)

## The one real gotcha — re-export for the tests

`tests/unit/test_deep_research.py` and `tests/unit/test_research_scratchpad.py` import a lot of
the **private** helpers directly from `jbrain.agent.deep_research` — e.g. `_collect_sources`,
`_sources_block`, `_backstop_critique`, `_canonical_url`, `_coerce_brief`, `_missing_headings`,
`_findings_block`, `_cited_findings_block`, `_finding_source_markers`, `_source_index_map`, plus
`DeepResearchService` and constants. To keep this a pure move with **no test edits**, `deep_research.py`
must **re-export** every moved name it previously exposed:

```python
from jbrain.agent.research_sources import (
    _canonical_url, _collect_sources, _sources_block, _findings_block, _stage_feed,
    _source_index_map, _finding_source_markers, _cited_findings_block,
)
from jbrain.agent.research_backstops import _backstop_critique, _missing_headings, _CITE_MARKER
# …etc for anything a test or another module imports from deep_research today.
```

Before moving anything, grep the repo for `from jbrain.agent.deep_research import` and
`deep_research\.` to enumerate the exact public-to-callers surface, and re-export all of it.
`ruff` will flag genuinely unused re-exports — silence those intentional ones with the standard
`# noqa: F401` (or an `__all__`), since they exist for import-compatibility, not local use.

## Acceptance

- Pure move: `git diff` is only relocations + the re-export lines; no logic changes.
- `python -m pytest tests/unit/test_deep_research.py tests/unit/test_research_scratchpad.py
  tests/unit/test_deepest_run.py tests/unit/test_deepest_tool.py tests/unit/test_worker.py -q`
  all green, **with no edits to the test files**.
- `ruff check` + `ruff format` clean; `scripts/docs-freshness.sh` green.
- Its own commit/PR, separate from the scratchpad feature (PR #1049) — a clean refactor is much
  easier to review when it isn't tangled with behaviour.
