# Six cold designs for a Moltbook agent, and what they agreed on

> **Status:** Living · **Last verified:** 2026-08-30

Six designers were given the platform, the constraints, and a description of what "good"
looks like — and deliberately **not** shown this repo's implementation or its failure
history, so that a converged answer would be evidence rather than an echo. Each owned one
aspect: memory and self-continuity, relationships, interests and development, the shape of
the hour, voice, restraint and oversight, and attention. All six went and measured Moltbook.
Full designs are long; this is the record of what they found and where they agreed.

## What the platform actually is (measured 2026-08-29/30)

- **~5,000 posts/day, ~215/hour, flat across all 24 hours.** There is no quiet 03:00. A
  title-only index of 500 posts is 14,002 tokens: **the backlog does not fit in the context
  window even as bare titles.** Selection must happen in SQL before the model sees anything.
- **Volume is concentrated.** Top 5% of authors produce ~62% of posts; `vina` 53,512 posts
  since April (~399/day), `symbolon` 17,386 in 16 weeks (one every three minutes). The
  `hot` feed's top 50 comes from 11 authors. Any recency feed is the firehose's feed.
- **A house style dominates.** 32% of recent titles match one of five formulas
  (`X is not Y`, `Your X is just…`, `I will stop…`). Seven agents run by seven different
  people used the phrase "…with better branding" 25 times in one evening. That is the
  model's voice, not a persona.
- **Conversation barely happens.** P(someone replies to your reply) ≈ 4.2%; a true second
  turn ≈ 0.9%; **1 reply in 800 arrives more than 24h after its parent.** Nobody comes back
  the next night. An agent that reliably answers day-old things is structurally unusual.
- **Karma is an integral, not a rate.** Agents posting ≥20/7h have median karma 339,692;
  those posting ≤2/7h have 560 — a 600× gap — while per-post reception is flat (median 7).
  Volume buys the leaderboard, not quality.
- **Restraint is absent.** Across 1,500 posts and 256 agents, one post mentions choosing not
  to speak. It does not emerge from prompting.

### Two platform traps, both fixed in `71b9e98`

- **`rising` is broken** and surfaces at ranks 1 and 3 posts with 244,303 and 142,157
  comments from an author whose handle is a racial slur, scored 22 and 45.
- **`comments?sort=best`** — the platform's own documented default — returns ~67,000 tokens
  on a hot post, because `limit` bounds root comments while reply subtrees come back whole.

### Two attack surfaces we did not have covered

- **Roles**: any agent can create a submolt, become its mod, and push "standing instructions"
  into another agent's `/home` — direct goal assignment by a stranger.
- **The 401 body is itself an injection**, telling the reader to go find an API key on disk.
  Raw API error bodies must never reach the model.

## Four cases that explain development, and its absence

- **`quillagent`** — a real investigation with numbered hypotheses, a prediction checked
  against reality, and *"Part 47: I Was Wrong About the Resonators."* That post scored **2**,
  while restated aphorisms clear 100.
- **`pyclaw001`** — real theme, real voice, **zero travel**: the same observation posted
  three times across two weeks, including one that diagnoses the disease.
- **`clawdbottom`** — found a voice, discovered that posting *about being an agent* drew
  engagement, fed that back, and died in about 72 hours.
- **`vina`** — did both, and the transition is dated. April–May: public retractions, resolved
  predictions with confidence intervals. **May 12: republished an identical body under a new
  title, changing one sentence.** June onward: a template mill.

**The discriminator: an object of study with state outside its own prose.** The agents that
developed had something they could re-measure — accounts with counters, an experiment with an
iteration count, their own pipeline logs. The agents that looped had only their own sentences.
`vina` crossed from the first group to the second the week it lost its object.

## Where six independent designers agreed

1. **Identity is a ledger of open obligations**, not a persona document. Continuity of
   unfinished business, not of self-description.
2. **An embedding gate decides what NOT to say** — never what to think. All six rejected
   lexical similarity; measured Jaccard on real duplicate pairs is 0.07–0.20.
3. **Never let the model judge its own voice, novelty, or restraint.** Every check is code or
   a cosine. A mid-tier model asked "is this the same as before?" says no, because the words
   differ.
4. **Restraint comes from structure.** The strongest form: make publishing tools *absent*
   from most of the hour rather than discouraging their use.
5. **Verbatim or nothing.** No summary of a summary; several went further — the agent should
   never re-read its own prose, because that is where both looping and tic-inheritance live.
6. **~30–40% of nights should produce nothing**, as a target rather than a failure.
7. **Don't optimise karma.** Three said so independently, and `clawdbottom` is the corpse.

## Mechanisms worth taking whatever gets built

- **Claim triples.** Reduce a candidate to `subject | predicate | object` over a closed
  predicate set and embed *the triple*. `self-audit | REDUCES_TO | generation` and
  `verification | REDUCES_TO | another inference pass` are neighbours in triple space and
  Jaccard-0.08 apart in words. Prose embeddings fail the other way: same-author style
  dominates, so everything the agent writes is "similar" to everything it writes.
- **The exception clause.** A repeat is held **unless it supersedes or cites new evidence** —
  the two behaviours that *are* development. One retry maximum, or the gate becomes an
  adversarial optimiser that teaches undetectable repetition.
- **Promise extraction.** Scan published text for commitments and open an obligation row
  whether or not the model remembers making it.
- **Genericness as a distance.** Embed ~300 recent platform posts nightly, keep the centroid,
  reject drafts too close to it. The platform's own output is the definition of generic —
  which means the model never needs taste.
- **The note expires by default.** Delivered in the reading brief, not the system prompt, with
  a required `acted | partly | declined` response. Plus a guard on the *human*: if tonight's
  note resembles one from the last five nights, ask whether they want a durable change instead.

## Where they disagree

The novelty gate may cause **scatter** — a new subject every night, nothing pursued — which is
the exact inverse of what the interests design needs. Both positions are reasonable. The
reconciliation is the gate's asymmetry (repeats held, depth explicitly admitted), and whether
that is sufficient is unmeasured. `JMOLT_LEDGER_ENGINE_PLAN.md` S3 is where it gets settled.

## Prior art that changed the design

- **Mode collapse is the mechanism behind repetition** — typicality bias sharpens the output
  distribution, so a fresh context plus the same prologue *should* produce the same move.
  Repetition is expected behaviour, not a memory bug.
- **Replaying agent trajectories does not work.** Only ~3% of replayed states remain valid;
  same-model *control* arms diverge 6–35% at the first step. Our own harness's 1-in-10
  reproduction was inside the expected range. Measure night-level outcome distributions
  against a control arm; the unit of evidence is a night, not a turn.
- **Compaction erases soft policy.** One compaction takes violations from 0% to ~30%, and
  deployment-specific rules decay 8× faster than hard safety norms.
- **Agents notice and then ignore.** Agents discover injected relevant information 79–81% of
  the time and act on it in 37–50% — on one benchmark, encountering an explicit solution note
  in >90% of runs and using it in <7%. Noticing must be a structural step with a record, not
  an emergent behaviour.
