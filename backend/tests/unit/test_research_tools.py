"""Pure-function coverage for the research-report library: the corpus helpers (question hash,
summary excerpt) and the tool helpers (ref resolution, view-data rebuild) — no DB, no network."""

from jbrain.agent.researchtools import _ref, _report_view_data
from jbrain.external.research_corpus import ReportRecord, _question_hash, _summary_excerpt


def test_question_hash_normalizes_whitespace_and_case() -> None:
    # The dedup key ignores casing and whitespace noise, so trivially-different phrasings of
    # the same question upsert the same row.
    base = _question_hash("How many died in 1918?")
    assert _question_hash("  how   MANY died in 1918?  ") == base
    assert _question_hash("How many died in 1919?") != base


def test_summary_excerpt_strips_markdown_and_caps() -> None:
    md = "## Heading\n\nThe **bold** answer is `~50 million` deaths.\n\n" + "x" * 2000
    out = _summary_excerpt(md)
    assert "#" not in out and "*" not in out and "`" not in out
    assert out.startswith("Heading The bold answer")
    assert len(out) <= 600  # capped for the listing + the summary embedding


def test_ref_prefers_id_then_question_then_url() -> None:
    assert _ref({"id": "abc", "question": "q", "url": "u"}) == "abc"
    assert _ref({"question": "  a question  "}) == "a question"
    assert _ref({"url": "u"}) == "u"
    assert _ref({}) == ""


def test_report_view_data_rebuilds_the_view_shape() -> None:
    rec = ReportRecord(
        id="r1",
        question="Q?",
        report_md="## R",
        complexity="deep",
        rounds=2,
        sub_agents=3,
        analyzed=True,
        revised=False,
        coverage_limited=True,
        truncated=False,
        sources=[{"url": "https://e.com", "title": "E"}],
        created_at=None,
        source_mode="library",
    )
    data = _report_view_data(rec)
    assert data["report_md"] == "## R" and data["question"] == "Q?"
    assert data["rounds"] == 2 and data["sub_agents"] == 3
    assert data["analyzed"] is True and data["coverage_limited"] is True
    assert data["web_sources"] == [{"url": "https://e.com", "title": "E"}]
    # A re-shown library report carries its source mode so the view can badge it.
    assert data["source_mode"] == "library"
    # The stored report has no live roster; the view treats children as optional.
    assert data["children"] == []
