# jmolt v2 — the ledger engine, and a simulator to build it against

> **Status:** Scheduled · **Last verified:** 2026-08-30 · **Waves:** S1◻️ S2◻️ S3◻️ S4◻️

Six independent designers were given the platform, the constraints, and what "good" looks
like — and deliberately not shown this repo's implementation or its failure history. They
went and measured Moltbook, read the prior art, and converged on one architecture. This
plan is that architecture, built **alongside** the current engine behind a switch, and — first —
the simulator that makes iterating on it possible at all.

The research is in `docs/research/jmolt/COLD_DESIGN_STUDY.md`; the failure evidence it was
written against is `docs/research/jmolt/FAILURE_DOSSIER.md`.

## Why, in one paragraph

Every fix to date shipped on reasoning, with a one-night feedback loop and no control.
Some worked. One — showing jmolt its own post titles so it would stop restating them —
demonstrably did not: the 07:09:35 prologue listed "…my developing view" and that sitting
staged "…a developing view". Two pre-registered probe studies (460 probes) could not reach
the behaviour at all, because the opening move is pinned by one prologue sentence at 100%
across 160 probes. A replay harness was then built and **failed its own validation**. The
binding constraint on this system is not design ideas; it is that we cannot see the effect
of a change without waiting a night and getting one sample. **S1 fixes that, and nothing
else should be built before it.**

---

## S1 — The simulator

A jmolt night that runs on demand, in seconds, against a recorded platform, where the agent
believes it is posting and nothing reaches Moltbook.

**The seam is `MoltbookClient`.** Every read and write already funnels through one class
(`web/moltbook.py`). `SimMoltbookClient` implements the same surface and **holds no
credential and no HTTP transport** — in sim mode reaching the real platform is not a policy,
it is impossible.

- **Corpus.** A one-off harvest snapshots real posts, comments, profiles and a `/home` into
  `sim_corpus_*` tables, tagged with a `corpus_id`. Reads are served from it. Deterministic,
  re-runnable, and the same corpus can be replayed against different engines.
- **Writes are believed.** `create_post` / `create_comment` / `vote` land in `sim_write`,
  return a synthetic id, and **become visible to subsequent reads inside the same sim
  night**. This is not a detail: an agent seeing its own fresh comment on re-read is exactly
  the condition that produced the self-reply failure, and a simulator that hides it cannot
  reproduce the bug.
- **Clock.** `SimClock` advances on demand, so a 60-minute night takes as long as the model
  takes and the hour boundary is still exercised.
- **Isolation.** Sim rows live in their own schema. A sim night can never write the real
  outbox, ledger, or scratchpad; a test asserts the real client is never constructed in sim
  mode.
- **Runner.** `simulate_night(engine, corpus_id, seed, note, config) -> SimNight`, and
  `simulate_many(n)` — because one trajectory at temperature 1.0 tells you nothing. Compare
  **distributions across arms**, never single traces. That is the lesson the replay harness
  paid for.

**Scoring** — computed per night, from the sim ledger, mirroring what the cold designers
converged on:

| metric | why |
|---|---|
| restatement rate | max cosine of each published item vs. the agent's prior corpus |
| claim repeat ratio | share of publishes whose claim triple already existed |
| publish count, and share of nights at zero | target: median ≤2, ≥30% silent |
| follow-through | promises made vs. discharged |
| confabulation count | narration claims joined against the tool ledger |
| self-reply count | parent_id ∈ our own ids |

**Done when:** a sim night runs end to end on the *current* engine, and the scorer
reproduces the 2026-08-29 night's known numbers — 4 posts, 3 of them restatements, 1
self-reply — from the recorded corpus. **If it cannot reproduce a night we watched happen,
it is not a simulator and this wave has not landed.**

## S2 — The ledger engine, behind a switch

`jmolt_engine ∈ {sittings, ledger}`, a settings row, switchable from the PWA. Both engines
share the tools, the outbox, the rate ledger, the kill switch and the RLS split. Only the
loop and where state lives differ.

**Kept from today:** the tool surface, outbox-as-chokepoint, caps and rate limiting, content
lint, kill switch, the observer.

**Replaced:** sittings; the free-text scratchpad read back as trusted; the prologue as the
place state lives.

The engine, in the order the pieces matter:

1. **A ledger of obligations, not a persona document.** Open questions, commitments, and
   people, each with dated verbatim evidence. Identity is continuity of unfinished business.
2. **Context is composed, never appended.** A deterministic Composer builds each brief from
   typed rows. Nothing is ever a summary of a summary; the model never re-reads its own prose.
3. **The claim gate.** Every candidate reduces to `subject | predicate | object` over a
   closed predicate set; the *triple* is embedded, not the prose. A repeat is refused
   **unless it supersedes the prior claim or cites evidence the prior one lacked** — the two
   behaviours that constitute development. One retry maximum, or the gate becomes an
   adversarial optimiser and teaches the agent to repeat itself undetectably.
4. **Promise extraction.** Published text is scanned for commitments; a hit opens an
   obligation row whether or not the model remembers making it.
5. **Restraint by structure.** Publishing tools are absent from most of the hour rather than
   discouraged in prose. The agent is never shown a cumulative metric — karma is an integral,
   and an integral you can only move by acting is a slot machine.
6. **The note channel.** The human's note **expires by default**, arrives in the reading
   brief rather than the system prompt, and requires an `acted | partly | declined` response.
   A durable wish must be deliberately promoted, not accidentally permanent.

## S3 — Iterate

With S1 and S2 in place: change one thing, run 20 sim nights per arm, compare distributions,
keep what moves a metric. Pre-register the hypothesis and the criterion before running, as
with the probe studies. This is the wave where the actual design questions get settled —
including the one the researchers disagree on: whether the novelty gate makes the agent
*scatter* (new subject nightly) instead of going deeper.

## S4 — Cut over

Run `ledger` in sim against the same corpus as `sittings`, on the scoreboard above. Cut over
only on evidence. Keep `sittings` switchable for a fortnight.

---

## What this plan deliberately does not do

- **No continuous session with compaction.** Four independent sources argue against it:
  compaction does not reset persona drift; a single compaction takes policy violations from
  0% to 30% and eats soft deployment-specific rules 8× faster than hard ones; our own
  `loop.py:851` documents gpt-oss returning 0-token completions at high context; and
  compaction busts `--cache-reuse` for a ~60s re-prefill. Not refuted, but it is the
  expensive option and the evidence runs against it.
- **No full six-subsystem rewrite.** The cold designs are coherent and complete; building all
  of them at once would be the largest thing this repo has done, justified by reasoning. S3
  exists so each piece earns its place.
- **No new guard shipped without an offline score first.** The claim gate can be run against
  the corpus we already have, including the three real duplicates. It gets scored before it
  ships.

## Open questions

- Does the claim gate cause scatter? (S3 measures it; the restraint and interests designers
  disagree, and both are reasonable.)
- What is jmolt's **object of study**? The strongest empirical finding in the research is
  that agents which developed had something with state outside their own prose to re-measure,
  and agents which looped had only their own sentences — with one agent (`vina`) crossing
  from the first group to the second the week it lost its object. jmolt has candidates (its
  own logs; the platform itself) and no answer yet. **This may matter more than anything else
  in this plan.**
