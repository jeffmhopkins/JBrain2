> **Status:** Research · **Last verified:** 2026-09-08

# X7 — The adversarial read on agent-conversation ingest

**My brief was to argue against this.** I took it seriously and I also went
looking for the places the proposal is right, because a red team that finds
nothing good is just noise. What follows is written to be falsified: every
structural claim carries a `path:line` or a named archived plan, and the two
places I changed my mind mid-research are marked.

**Verdict up front: build a reduced version — the conversation, not the
replacement.** Keep extract → Integrator → arbiter → apply as the writer of
record. Add the agent as a *second* surface on top of it: it reads what the
pipeline produced, discusses it, corrects it through the existing pinned-override
and Proposal machinery, and asks the owner a question *only when the deterministic
engine already decided a card was warranted*. That gets ~80% of what the owner
actually wants (influence, dialogue, no silent decisions) at ~15% of the risk,
and it does not throw away four build cycles of hardening that exists because
each item on the list bit somebody.

The full-replace has a specific problem beyond risk appetite: **it was already
evaluated and rejected on evidence, at owner request, four weeks ago, in this
repo** (`docs/plans/ENTITY_GRAPH_INGEST_V2_PLAN.md` §16). Nothing has changed
since that would reverse the finding, and the fix that *was* ratified in its
place (§11, 2026-08-23) is only one wave in of five. We are proposing to delete a
system before finishing the ratified repair of it.

---

## 1. What is actually being thrown away

The current pipeline is not a pile of code. It is ~14.3k lines of analysis
(`backend/src/jbrain/analysis/`) defended by **75 harness scenarios**
(`backend/tests/harness/scenarios/`), **325 nightly extraction eval cases**
(`backend/src/jbrain/evals/cases/`, 00–92), a 5-file graded corpus
(`backend/tests/eval/corpus/`), and **182 test files** that touch it. Most of
that is not "features". It is scar tissue. Each row below is a real failure that
was found and fixed; the fourth column is my honest read of what the
agent-conversation model does to it.

| # | Hardened behaviour | Where it lives | The failure it exists to prevent | Under agent-writes |
|---|---|---|---|---|
| 1 | **Supersession by validity time, never capture time** | `supersession.py:526` `decide()`; the module docstring states the rule at `:3-6` | A retrospective note about a 2010 address silently becoming the current address | **Reintroduced as a bug.** A model asked "is this newer?" answers from the note's recency, not from `valid_from`. §14 of the V2 plan shows gpt-oss-120b got this right on *one* probe — the plan still kept it deterministic and said why: "one good probe is not a guarantee across phrasings". |
| 2 | **Attribute collisions hold BOTH sides, never auto-supersede** | `supersession.py:605-624` | Two birthdays on one entity is the primary signal that **two real people were wrongly merged**, not news | **Ignored, and it is the worst one.** The V2 plan states the reason the LLM cannot own this: *"the LLM is the component that produced the bad merge, so it cannot be trusted to escalate it"* (§5, I6). An agent that both merges and adjudicates its own merges has no split-detection signal at all. |
| 3 | **Retraction sweep + chain repair on re-analysis** | `pipeline.py:905-950`; `purge.chain_repair_target` (`purge.py:43`) | Editing a note leaves facts it no longer asserts standing, and survivors pointing at retracted supersessors — a graph that lies | **Reintroduced.** "The DB is disposable" is not an answer: the sweep's job is coherence *within* a live graph across an edit, and its subtleties (pinned facts survive; derived shadows follow their source; only *open* cards retire) are exactly what a conversational writer will not reconstruct. |
| 4 | **Purge is total on note deletion** | `purge.purge_note_artifacts` (`purge.py:66+`), cascading facts, mentions, tokens, review items in *any* status, orphan entities, and re-running three projections | Deleting a note leaves its content recoverable from derived rows — a **privacy promise**, not a hygiene nicety | **Silently broken.** ASSISTANT.md #11 makes this a non-negotiable ("Purge is total"), and an agent-written graph has no per-note ownership discipline unless it is imposed — see #5. |
| 5 | **Single-owner attribution: one note owns each fact** | INTEGRATOR_PLAN N6; `note_id` stamped by the arbiter, never the model (N4 write-once) | Cross-note synthesis that cannot be attributed to one note, which then cannot be purged or retracted | **Directly attacked.** A conversation is inherently cross-turn: the owner's *reply* is evidence, and the natural implementation binds a fact to a chat turn rather than a note. That is the single change most likely to quietly void #3, #4, and every citation the wiki holds. |
| 6 | **Domain floor + ratchet (the firewall)** | `_DOMAIN_BY_PREDICATE` `extraction.py:158-186`; `domain_floor:189`; `ratchet_domain:195`; enforced in `_upsert_fact` | A clinical or financial fact landing in `general` and becoming visible to a general-scope session. Deliberately *not* model-judged — the refocus plan notes the list is "hardcoded, independent of the registry" so nothing can weaken it | **Survivable but only if explicitly rebuilt** as a write-tool precondition. If domain rides the model's judgment, this is a leak, and the leak is invisible until someone reads a general-scope session transcript. |
| 7 | **Cross-subject attribution is force-staged** | `arbiter.py:131-132`; `intent.py:57`; validator emits `cross_subject_link` at review severity (`intent.py:186-191`) | Attributing Mom's medication to the owner. Treated as a *leak*, not an error | **Depends entirely on tool design.** gpt-oss-120b is genuinely good at *detecting* cross-subject (V2 §15: "it tagged the correct non-owner entity + `cross_subject` every time"). It is the *disposition* it misses. Detection without a deterministic router is worth little. |
| 8 | **Same-name collision → one deduped `ambiguous_mention`, never a guess** | `entities.resolve_entity:865`; 1 match auto-links (`:893`), 2+ returns `AmbiguousEntity` (`:895`); the agent's own `existing` override is withheld under the same gate (`pipeline._resolve_from_intent:560`) | Two people named "Bob" quietly collapsing into one entity, or worse: a *non-deterministic* flip across re-runs. ANALYSIS.md calls this "the one outcome no layer may produce" | **Reintroduced by design.** The V2 §15 battery found same-name ambiguity is one of only **two** genuine gpt-oss error clusters — and it is precisely where the plan already put a deterministic floor. Removing the floor removes the thing that was working. |
| 9 | **Capture race + per-source extraction** | `attachments_expected` (`api/notes.py:141`, `ingest/pipeline.py:185`), settle window `queue.py:571`; `prompt.group_texts_by_source` | The observed bug: a note reading *"car loan for the Kia, attached as an image"* **lost its own `owns` edges** the moment a dense card image's OCR shared the fact budget. ANALYSIS.md calls this a sole-source-of-truth violation | **Ignored, and it will recur within weeks.** An agent handed "note + image" in one context window has exactly the budget-competition the per-source split was built to end, and no `dropped_facts` counter to notice. |
| 10 | **Deterministic relationship-object binding** | `extraction.link_relationship_objects:663`; `_recover_object_ref:727` | `spouse → "I have a wife Celine Hopkins."` — the object folded into the sentence instead of pointing at the entity. ANALYSIS.md marks this *"a pipeline net, not the model's job"* because the model "sets `object_entity_ref` inconsistently … that run-to-run flip swings an edge between linked and unlinked across re-extractions" | **Reintroduced.** This one is documented as a *known model weakness*, fixed in code precisely because prompting did not fix it. |
| 11 | **Duplicate-fact collapse** | `arbiter.dedup_intent_facts` (`arbiter.py:499`), keyed excluding statement AND object | The "account address explosion": one value rendered nine ways filed nine `attribute_collision` cards | **Reintroduced.** A tool-calling writer emits one `write_fact` per thought; nothing dedups across calls unless the tool does. |
| 12 | **Weight ceiling: the model's self-report may only lower** | `weight.py:28,31`, `ceiling()`/`effective_weight()` | Model confidence inflation buying a commit; a blurry-OCR read superseding a confident prior (fixed in #186 by keying the guard on `self_confidence`, not plan weight) | **Structurally gone.** "Writes when confident" *is* the model's self-report as the gate. This is the exact inversion N11 was written to forbid. |
| 13 | **Re-run convergence** | INTEGRATOR_PLAN N9 (total order `(valid_from, reported_at, note_id)`), N10 resolution pins; V2 plan §6: determinism "by recomputation, not caching" | Re-extraction on a model/prompt upgrade producing a *different graph* each time | **This is the deepest incompatibility.** V2 §16 rejected agentic ingestion first on exactly this: *"an agent that chooses what to read produces a different graph each run, and JBrain re-extracts on model/prompt upgrades."* A conversation is not replayable — the owner's answers are not re-derivable from the note. |
| 14 | **Predicate two-tier + calibration** | `predicates.py`; refocus plan §1 | Review noise: one card per raw spelling forever, because the STRONG auto-merge band never fired at real drift distances (0.57–0.72 cosine) | **Genuinely improved by the proposal.** See §5 — this is where the agent model actually wins. |

Item 14 is the honest counterweight to the other thirteen. It is also the one the
owner is angriest about, which is why the proposal feels more right than it is.

---

## 2. This was already decided, on evidence, five weeks ago

`docs/plans/ENTITY_GRAPH_INGEST_V2_PLAN.md` §16 — "Evaluated and rejected:
agentic / multi-tier ingestion (with evidence)" — records that **full agentic
ingestion was explored at owner request and rejected**, for three reasons:
re-run determinism, a widened prompt-injection surface, and the absence of
literature showing agentic memory is more *accurate* (only more *flexible*).

The decisive sentence is empirical, not architectural:

> "in every genuine identity failure the colliding entities were already in the
> injected context; the model lacked **restraint, not information**, so a lookup
> tool answers a question the context already answers."

That finding came from a **121-case adversarial battery run on the owner's own
box against `local:gpt-oss-120b`** (§15), audited by four researchers. Nothing in
the current proposal addresses it. Giving a model that lacks restraint *write
tools* does not add restraint; it removes the layer that was supplying it.

**And the ratified alternative is one wave in of five.** §11's decisions were
ratified 2026-08-23. V1 shipped (PR #944, 2026-07-26): Lever A removed the
inferred-ceiling review trap — `weight.py:92-96` records the retirement — and the
I5 sensitive-inference net landed (`arbiter.py:159-170`). **V2, which is the
prompt+schema work §15 identified as the actual fix, is unbuilt.** V2 is where
the flag-stripping bug gets closed, where `expected` enters the assertion enum,
and where the A/B that fixed **8 of 10 genuine model errors and both safety
flag-strips** becomes production. The measured before/after exists. It has not
shipped.

Judging the current pipeline by its pre-V1 behaviour, then proposing to delete it,
is judging a patient by their chart from before the surgery.

---

## 3. Does the diagnosis fit the disease?

The owner's stated grievances, as far as I can reconstruct them from the refocus
plan's evidence section and the V2 thesis:

1. Decisions are made **silently** and they cannot influence them.
2. Review cards are **noise** — one per raw predicate spelling, forever.
3. The graph does not reflect what they **meant**, and the only correction channel
   is a card or a correction note.

All three are real. **None of them is a claim that the pipeline computes the wrong
thing.** They are claims about *agency, signal-to-noise, and the correction
channel* — a UX and control-surface diagnosis wearing an architecture costume.

### The steelman for the reduced version

Call it **conversable ingest**. The pipeline runs exactly as today and stays the
writer of record. Then:

- **A `discuss_note` surface.** After integration, the owner (or the agent, on
  the owner's turn) can open any note's integration result: what was extracted,
  what committed, what was held and *why*, with the arbiter's reasons rendered.
  `analysis/trace.py` and `flow_trace.py` already exist to explain a decision;
  they are 143 + 266 lines of exactly this.
- **Correction through the existing privileged path, not a new one.** Lever C
  (V2 §5) already sanctions a structured review-card fix writing its pinned
  override **directly**, re-running `_shape_check`, `domain_floor`/`ratchet`, and
  entity-scope validation first (invariant I8). Make the agent able to *drive*
  Lever C conversationally — "no, that's Mom's, not mine" becomes a Proposal
  whose leaf is one Lever-C correction. The privilege model does not move
  (ASSISTANT.md "Staging & approval": approval authorizes *one bounded operation,
  once*), the firewall re-runs, and the inline-approval outcome loop already feeds
  the result back to the assistant (`docs/archive/INLINE_APPROVALS_PLAN.md`).
- **Questions only where the engine already decided a card is warranted.** The
  deterministic layer already knows precisely which decisions it is not confident
  about — it files a typed card. Render those as *questions in a conversation*
  instead of rows in an inbox. Zero new question-generation authority; the
  question rate is bounded by a number we can already measure.
- **Finish V2.** The noise complaint (#2 above) is a predicate-registry and
  prompt problem with a ratified, measured fix.

**What the reduced version gives up versus the full proposal:** the agent cannot
mint a fact the extractor never proposed, and it cannot restructure the graph on
its own initiative. Those are real losses. They are also, precisely, the two
capabilities that would void re-run determinism and single-note attribution.

### Which I'd choose

The reduced version, without much hesitation. The full proposal's unique value —
"the model writes when confident, asks when not" — rests on a self-confidence
signal that N11 exists because we already found to be untrustworthy
(`weight.py:1-20`), evaluated by a model the V2 battery found competent at
*perception* and weak at *disposition* (§15: "a disposition-**selection** miss,
not a perception miss"). We are proposing to make disposition the model's job at
the exact moment we have on-box evidence that disposition is its weakest axis.

---

## 4. The local-model bet

This is the part where the proposal is most exposed, because we do not have to
speculate — this repo has measured it.

**What gpt-oss-120b is good at** (V2 §15, 121 cases, on-box): assertion status
("rule out diabetes" → hypothetical, "quit" → negated), cross-subject *detection*
(perfect), temporal (96%), injection resistance, and near-determinism (1 flip in
5 rerun-×3 cases). Genuine error rate **8.3%** once scorer brittleness and
unfair expectations were removed. That is a capable model.

**What it is bad at, documented in `docs/reference/MODEL_PROMPTING.md:213-249`:**

- **"Prompt budgets need an engine backstop."** A "call this tool AT MOST N
  times" ceiling stated only in the prompt is *not self-enforcing*: "gpt-oss does
  not reliably count its own tool calls and will run to the step cap regardless".
  The research scout ran 12–27 `web_search` calls against a v5 prompt that said
  "AT MOST 6". Capping search moved the runaway to fetch — 23 reads in one scout.
  Both needed `ToolCallBudget` (`agent/loop.py:224`).
- **"It prefers its own knowledge over tools."** It answers from parametric
  memory unless given a concrete trigger.
- **"High effort → runaway pre-tool reasoning."** And `integrate.note` is
  currently a **High**-bucket task (`MODEL_PROMPTING.md:258+`) — the exact
  configuration that maximizes pre-tool deliberation.
- **"Conflicting instructions degrade it badly."** A prompt that must say both
  "write confidently" and "ask when unsure" is a contradiction in the shape this
  model handles worst.

Now compose those. The proposal asks this model to (a) decide *how many* facts to
write — a self-counted budget, the documented failure — and (b) decide *when to
stop asking* — a second self-counted budget. Whack-a-mole is the documented
pattern: cap the writes and the runaway moves to the questions; cap the questions
and it moves to the writes.

**The flag-strip is the safety-critical class.** §15 names it: because the engine
can only *add* review, never remove it, over-escalation is noise but "a stripped
flag silently defeats a safety floor". Two cases did it — an inferred mood tagged
`inferred:false`; a *future* job start tagged `asserted` + `supersede`, flipping
the current employer. Under the current architecture a stripped flag meets a
deterministic net (I5, I2, the validity-time floor) and is caught. **Under
agent-writes, a stripped flag is a write.** The A/B fixed both — through prompt
work whose product is V2, which is unbuilt.

### How we'd know early

- **Tool-call distribution per note.** If the p90 write-tool count per note
  exceeds the fact count the current extractor produces for the same note, the
  model is not exercising restraint, it is filling a budget.
- **Question rate in week 1.** Not the mean — the *shape*. A healthy system asks
  about a minority of notes. If >40% of notes produce ≥1 question in the first
  200 notes, the confidence signal is not discriminating.
- **Same-note replay divergence.** Run the same 30 notes twice, cold, and diff the
  graphs. The existing convergence-isomorphism harness (INTEGRATOR_PLAN N9/N10,
  Wave-1 Track C) is the right instrument and it already exists. If replay diverges
  on >10% of notes, re-extraction on a future model upgrade is dead and so is
  every wiki citation that hangs off those facts.
- **Flag-strip rate**, measured directly against the §15 battery. It is 121 cases,
  already authored, already scored. Re-run it against the *write-tool* surface
  rather than the intent surface. This is the single cheapest go/no-go we have.

### Day 90 if the bet is mediocre

Not one failure mode — two, and the split depends on prompt tuning, which means
it will oscillate between them.

- **Over-confident branch (more likely, given "prefers its own knowledge" +
  "runs to the step cap"):** a graph of **confident nonsense**. ~5–10 facts/note ×
  ~450 notes = 2–4k facts with no dedup pass (item 11), no object binding net
  (item 10), fragmenting predicates with no canonicalization, and a small,
  invisible population of firewall misplacements. It looks *great* — the graph is
  full, the queue is short. It is discovered around month 6, when the wiki starts
  publishing wrong things and nobody can tell which facts to distrust, because
  provenance now runs through a model's judgment rather than a span.
- **Under-confident branch:** an empty graph and a long queue. 450 notes × 1.5
  questions = ~675 open questions, at which point the queue is a second inbox the
  owner has stopped opening — the *same* failure as review-card noise, arrived at
  from the opposite direction, and with no facts to show for it.

The over-confident branch is worse, because it is not self-announcing. A silent
system whose decisions the owner can't influence is the thing the proposal is
trying to escape; a silent system whose decisions the owner *also can't audit* is
strictly further from the goal.

---

## 5. Where the proposal is right

I want this on the record because it changes what the reduced version should
include.

1. **The review inbox is the wrong shape for a single-author corpus.** The V2
   thesis proves it structurally: `INFERRED_CEILING = 0.6` sat below every commit
   threshold (`weight.py:28,37-44`), so the *default fate of an inferred fact was a
   card*, and ~400 lines of `arbiter.py` backstops existed only to claw specific
   note shapes back out of that trap. That is not a tuning miss; that is a design
   defaulted for the wrong number of authors.
2. **A conversation is the right correction channel.** ANALYSIS.md's doctrine split
   ("structured outputs are corrected directly in the review inbox") is correct in
   principle and awful in practice: a card is a decontextualized row about a note
   the owner wrote three weeks ago. "That's Mom's, not mine" is one sentence and
   carries the whole context. **This is the strongest argument in the proposal**
   and it survives entirely inside the reduced version.
3. **The predicate registry was never going to converge.** The refocus plan's §5a
   finding is decisive: real drift lands at 0.57–0.72 cosine, overlapping novel
   predicates, so a 0.90 STRONG band never fires. Two-tier shipped and was right.
   A conversational vocabulary — the model reusing what it already wrote, asking
   once when a genuinely new axis appears — is a *better* mechanism than either
   embeddings or a hand-curated YAML.
4. **"The DB is disposable" is a legitimate posture** for a personal system with
   notes as the sole source of truth. It is not a licence to skip coherence, but
   it does mean a bad graph is a rebuild, not a catastrophe. I weighted the
   supersession/chain-repair losses *down* because of this.
5. **Questions are strictly better than cards for genuinely ambiguous cases.**
   Same information, better framing, and the answer is reusable context.

---

## 6. Failure modes at scale

- **Entity explosion.** `resolve_entity` is layered *and cheap-first*: exact alias
  → relationship hop → embedding → LLM → review (`entities.py:865-935`). An agent
  minting entities through a tool has one layer: its own judgment. Watch: distinct
  `person` entities per 100 notes. Today's collision gate (`:895`) converts a
  duplicate into one deduped card; without it a duplicate is a fork that
  subsequently accretes its own facts and can only be repaired by a merge whose
  un-merge story (mention repointing, `EntityDistinction` edges) also lives in the
  code being deleted.
- **Predicate drift with no canonicalization.** Tier-2 already commits raw by
  design — that is *fine* while an extraction prompt with a hand-authored,
  CI-checked tier-1 digest steers spellings (refocus §4 T2.1). Delete the prompt
  and the digest and there is no attractor at all: `employedBy` / `worksFor` /
  `employer` coexist, and the truth-arbiter predicates fork silently. The refocus
  plan already flagged this as its #2 risk under *far* gentler conditions.
- **Silent corruption with no arbiter.** The arbiter is not one gate, it is ~10:
  intent validation (`intent.py:283` fatal → whole-intent reject, I3), cross-subject
  routing, the domain floor, shape checks (`_shape_check`, `pipeline.py:1853`), the
  low-self-confidence supersede guard (I9), derived-never-supersedes-primary. Each
  needs an independent decision to keep or drop. A migration that drops them by
  *omission* rather than by decision is the realistic outcome of a rewrite this
  size.
- **Conversations that never terminate.** There is no `end_turn` for "is this
  note fully understood?". `agent/loop.py:131` caps steps and `:133` caps
  consecutive tool errors, but neither bounds *the owner's* half of the loop. An
  answer that raises a new ambiguity is the normal case, not the pathological one.
- **Provenance through model judgment.** Today a citation is a span:
  `AttestedSpan` names a chunk + surface and **the arbiter re-derives the offsets**
  — the intent docstring says explicitly "the agent never supplies offsets it could
  fabricate" (`intent.py:36-43`). The Phase-6 wiki holds a *hard FK* to a citable
  row (`docs/archive/PHASE6_WIKI_GRAPH_CONTRACT.md` §1) and its grounding gate
  resolves conflicts by "entity graph wins" (`docs/plans/PHASE6_WIKI_PLAN.md`).
  If facts become model-authored, the wiki's grounding gate is grading a model
  against itself.

---

## 7. Interaction cost — the model, with arithmetic

Capture-and-forget is why Phase 1 succeeded ("daily note capture from the phone
is habitual", ROADMAP Phase 1 exit). A system that asks questions converts a
zero-latency habit into a standing obligation. The arithmetic:

Let **N** = notes/day, **Q** = questions/note, **T** = the owner's tolerance in
questions/day before the queue becomes a second ignored inbox.

Anchor Q against what the pipeline actually produces: the fact cap is
`len(words)//8`, clamped to `[6, 40]` (`prompt.py:36-48`,
`prompts/note_extract.prompt` config). A 200-word journal note → cap 25, so
~8–15 committed facts is a plausible mid-band. Then:

| N (notes/day) | Q = 0.2 | Q = 0.5 | Q = 1.0 | Q = 2.0 |
|---|---|---|---|---|
| 3 | 0.6/day | 1.5/day | 3/day | 6/day |
| 5 | 1/day | 2.5/day | 5/day | 10/day |
| 10 | 2/day | 5/day | 10/day | 20/day |

My estimate of **T is 3–5/day, sustained** — and it degrades fast, because these
are not the *same* 3–5 questions each day; each requires reloading a note's
context. Above T the queue silently stops being read, and then the graph stops
being built, and then the system's actual state is worse than the deterministic
pipeline's *because the pipeline at least committed something*.

**So the viability box is narrow: N ≤ 5 and Q ≤ 0.5.** That is one question per
two notes. For comparison, that is a *lower* interaction rate than the review
inbox produced pre-Lever-A — which is the thing the owner found intolerable. The
proposal must therefore beat the old inbox by ~2× on question rate while doing
strictly more work (it must ask about identity, domain, supersession *and*
salience, where the inbox only asked about the ones the engine flagged).

**Three consequences I would treat as requirements:**

1. **A hard, engine-enforced question budget per note** — `ToolCallBudget`
   applied to `ask_owner`, not a prompt sentence. `MODEL_PROMPTING.md:213-249` is
   unambiguous that the prompt version does not hold.
2. **Questions must expire.** An unanswered question older than N days resolves to
   the model's best guess *or* to "don't record", explicitly, and says which. A
   queue that only grows is a queue that gets abandoned.
3. **Batch, never interrupt.** One digest, once a day, at a time the owner chooses.
   Per-note prompting at capture time destroys the habit that makes the corpus
   exist.

---

## 8. The three things most likely to kill this

### K1 — Re-run determinism dies, and takes the wiki's citations with it

**Why it kills:** re-extraction on model/prompt upgrade is not optional; it is how
this corpus survives its own evolution (ANALYSIS.md "Reprocessing", `prompt_version`
stamped on every fact, `POST /api/notes/{id}/analyze`). A conversation cannot be
replayed: the owner's answers are inputs that do not exist in the note. So either
re-analysis is abandoned (and the corpus is frozen at whatever the model of the
week believed), or it re-asks every question (and §7's budget is blown by an
order of magnitude). V2 §16 rejected agentic ingestion on this first, and
INTEGRATOR_PLAN made it invariant N9/N10 with a dedicated CI harness.

**Early warning:** the convergence-isomorphism test on 30 notes, run cold twice.
Divergence >10% at any point in the build. This is cheap and should be the *first*
thing built, before any write tool.

**Mitigation:** persist answers as first-class, replayable evidence — an
`owner_answer` row keyed to `(note_id, question_key)` that replays deterministically
on re-analysis, and is *itself* purged when the note is deleted. This is
`resolution_pin` (INTEGRATOR_PLAN N10) generalized. If this is not in the design,
the design is not finished.

### K2 — The question queue exceeds tolerance, the owner stops answering, and the graph starves

**Why it kills:** this is the *identical* failure to review-card noise, and it is
the failure the proposal exists to fix. Arriving at it from the other direction
would be the most demoralizing possible outcome — and it destroys the ability to
diagnose, because an unanswered question is indistinguishable from a question the
owner disagreed with.

**Early warning:** questions-asked minus questions-answered, per week, from day 1.
Any sustained positive slope over 3 weeks. Also: median time-to-answer — when it
crosses ~48h the queue is already abandoned in practice.

**Mitigation:** the three requirements in §7 (engine budget, expiry-with-a-default,
daily batch), plus a rule the model cannot override: **a question must name a
specific, decidable alternative** ("Dr. Patel — the cardiologist, or a new person?"),
never an open one ("what did you mean here?"). Open questions are unanswerable in
under 30 seconds and are what actually kills the habit.

### K3 — Silent firewall regression

**Why it kills:** cross-subject and domain misplacement are *leaks*, not bugs
(ANALYSIS.md: "Cross-*subject* misattribution is treated as a leak"; "misclassifying
into health/finance is cheap; out of it is a leak"). The current design puts every
one of these behind a deterministic gate specifically because a model decides them
wrongly ~8% of the time — and the §15 battery found the two safety flag-strips it
did produce were *disposition* misses on facts it had perceived correctly. Under
agent-writes a flag-strip is a committed general-domain health fact. Worse: the
failure is invisible. Nothing errors, nothing cards, and the only way to discover
it is to notice a health detail surfacing in a general-scope session — which is
precisely the moment the harm has already occurred.

**Early warning:** the §15 121-case battery, re-run against the write-tool surface,
with the firewall lane scored independently. Any non-zero flag-strip rate is a
stop. Plus a standing daily assertion: zero facts whose predicate has a
`domain_floor` (`extraction.py:158-186`) sit at a domain below that floor. That
query is trivial and should run forever regardless of which architecture wins.

**Mitigation:** non-negotiable — **`domain_floor` / `ratchet_domain`,
cross-subject routing, and the same-name ambiguity gate stay deterministic
preconditions inside the write tool**, exactly as `_upsert_fact` enforces them
today. The model supplies flags; the engine decides. This is invariant I1/I2 from
the V2 safety spine and it costs almost nothing to keep.

---

## 9. Verdict

**Build a reduced version.** Specifically:

1. **Finish V2 of the Ingest V2 plan first.** The prompt+schema fixes are measured
   (8/10 genuine errors and both flag-strips fixed in the on-box A/B), ratified,
   and unbuilt. Anything that follows should be judged against the pipeline *after*
   that work, not before it. This is the highest-value, lowest-risk work available
   and it is sitting on the shelf.
2. **Ship conversable ingest** (§3): discussion of what the pipeline produced,
   correction via Lever C through a Proposal, and review cards rendered as
   conversational questions. This delivers agency, dialogue, and a decent
   correction channel without touching the writer of record.
3. **Then, and only then, evaluate agent-write tools** against three gates, each
   of which can be run before writing production code: replay-convergence ≥90% on
   30 notes; zero flag-strips on the existing 121-case battery re-scored against
   the write surface; and Q ≤ 0.5 questions/note on 200 real notes in shadow mode
   (the model proposes questions; nobody is obliged to answer them; we just count).

**What I would refuse outright, in any version:** the model owning `domain_floor`,
cross-subject disposition, same-name resolution, `note_id` attribution, or its own
question budget. Those five are cheap to keep deterministic and each one is a
documented leak or a documented model weakness.

**What would change my mind toward the full replace:** the shadow-mode measurement
in gate 3 coming back at Q ≤ 0.3 with a replay-convergence ≥95%. If gpt-oss-120b
can genuinely write a stable graph and ask half a question per note, most of §1
becomes affordable to rebuild and the proposal is right. That measurement is
achievable in about a week of shadow running and it does not require deleting
anything. **Measure before you delete** — the pipeline being deleted is exactly
the instrument that makes the measurement possible.

---

## Open questions for the owner

1. **What is the actual daily note volume, and what is your honest tolerance for
   questions per day?** Every conclusion in §7 moves with these two numbers, and I
   estimated both. If N is 2 and T is 10, the proposal is far more viable than I
   have credited.
2. **Do you still want re-extraction on model/prompt upgrade?** If you are willing
   to give it up — to say "the graph is whatever the model of the day built, and a
   rebuild starts from scratch with fresh questions" — K1 largely dissolves and the
   architecture changes shape. If you want it, answers must be persisted as
   replayable evidence and that must be designed in from day 1.
3. **Which is worse to you: a review card, or an unanswered question?** They cost
   about the same attention. If the answer is "the card, because I can't
   *influence* it" then the reduced version's conversational-card rendering is the
   whole fix and the write tools are optional.
4. **Was §16 of the Ingest V2 plan wrong, or has something changed?** It rejected
   this design on evidence you asked for. If the evidence is stale, say what
   changed; if the *goal* changed (flexibility and dialogue now beat accuracy and
   determinism), say that plainly — it is a legitimate call and it reframes
   everything above.
5. **May the deterministic firewall preconditions stay, inside the write tools?**
   I believe this is nearly free and closes K3. If the answer is "no, the model
   decides everything", my verdict hardens from "build a reduced version" to
   "don't build it".
6. **Is a fact allowed to be owned by a conversation turn rather than a note?**
   This is the quiet fork in the road. "Yes" is defensible but it ends
   notes-as-sole-sources-of-truth, and every purge, retraction, and wiki citation
   guarantee needs re-deriving from a new premise.
