"""LLM-in-the-middle test harness.

A scenario hand-authors the JSON a perfect note.extract model WOULD return for
a sequence of notes, then runs it through the REAL analyze_note pipeline
(entity resolution, supersession, temporal tokens, domain ratchet, review
inbox) against real Postgres and asserts the resulting graph. It tests the
deterministic pipeline given good model output — not the prompt, which only a
live model can exercise. See README.md.
"""
