> **Status:** Research · **Last verified:** 2026-09-08

# L2 — Making the confidence split real

Research for the proposed agent-ingest redesign (the deterministic arbiter is deleted; a
tool-using agent writes the entity/predicate graph directly, committing what it is confident
about and asking the owner about what it isn't). Binding constraint: **inference is
local-only** — `gpt-oss-120b` (text), `qwen3.8-27b-abliterated` (vision), 128 GB box, no
frontier judge.

Legend: **[V]** verified in this repo or a cited source · **[A]** assumed / needs measurement.

---

## 0. Recommendation (read this, skip the rest if you must)

**Adopt the owner's prior, with two amendments.**

1. **The server computes an autonomy ceiling per proposed write, from deterministic
   structural risk signals (option `e`). The ceiling is the whole decision.** Every signal
   the current arbiter weighs survives as a risk input; only the *arbiter object* dies. The
   ceiling is a lattice value `commit ≥ commit_and_log ≥ ask ≥ refuse`, taken as the **min**
   over the signals that fire. This is `plan_intent`'s job re-expressed as a pure function
   over a *write* instead of over an *intent*, and it stays 100 %-covered security-path code.

2. **Amendment A — the model's self-report is a *flag*, not a float, and it may only lower.**
   Delete the `[0,1]` `self_confidence` scalar from the write contract. Replace it with a
   ternary `certainty ∈ {stated, inferred, unsure}` plus a free-text `doubt` string. Rationale
   below (§3a): the local model's *perception* flags are good and its *scalar self-rating* and
   *disposition selection* are measurably bad — the repo has already measured exactly this
   (§2). A float invites the model to buy autonomy with a number; a flag cannot.

3. **Amendment B — the model MAY raise the ceiling, but only by handing over a receipt the
   server verifies.** The one channel is verbatim attestation: the agent supplies
   `attested_span.surface`, the server checks it literally appears in the note text. This is
   already built and already load-bearing (`backend/src/jbrain/analysis/arbiter.py:617-672`)
   and it is *not* a trust concession — the raise is contingent on a deterministic string
   check, so a hallucinated quote raises nothing. Without this channel the design over-asks
   badly: the arbiter's eight "attest anyway" backstops exist precisely because a
   server-only ceiling with no receipt channel buried the owner in cards
   (`docs/plans/ENTITY_GRAPH_INGEST_V2_PLAN.md:22-32`).

4. **Question budget is a hard resource, not an emergent rate.** Target **≤ 0.2 questions per
   note** steady-state, hard cap **2 per note**, global cap **3 open questions at once**, and
   **every question expires** (7 days → resolves to the safe default, the write is *not*
   committed, and it is never re-asked unless a new note re-raises it). A queue that is never
   empty is the documented failure mode this system already has a name for: *approval
   fatigue*, named as a red-team threat in `docs/reference/ASSISTANT.md:1101-1112`.

5. **Token logprobs (option `b`): build the plumbing, use it for one narrow job.** llama.cpp's
   OAI chat endpoint does support `logprobs`/`top_logprobs`, returning **pre-sampling**
   `log(softmax(logits))` [V, PR #10783]; the repo's client neither sends nor parses them
   [V, `backend/src/jbrain/llm/openai_compat.py:188-232`]. Use mean token logprob over the
   **value span only** as an OCR/garble detector feeding the existing low-confidence
   supersession guard (`supersession.py:183,718-731,758-781`), which today reads a model
   float. Do **not** use it as the general confidence signal.

6. **Reject self-consistency as a per-note default (option `c`); keep it as a calibration
   instrument.** Cost is minutes per note on this box (§3c) and — decisively — the repo's own
   121-case on-box battery measured the model as *near-deterministic* (1 flip in 5 cases rerun
   ×3, `ENTITY_GRAPH_INGEST_V2_PLAN.md:541`). Agreement that high carries almost no
   information about correctness; it is confidently repeating the same answer.

7. **Second-pass verifier (option `d`): narrow yes, one class only — flag-stripping.** Not as
   an LLM-judge of quality (same-model self-preference bias is well documented), but as a
   *re-elicitation of a checkable receipt*: "does the note literally state X? quote it." The
   server grades the quote, not the verdict. Run it only on writes the ceiling put at `ask`,
   so its only possible effect is converting an ask into a commit — the direction that reduces
   owner burden.

8. **What replaces `docs/archive/CALIBRATION_LOOP.md`: frozen-transcript policy replay.**
   Freeze the *agent's tool calls* from on-box runs as JSON fixtures; CI replays them through
   the pure risk-gate and asserts auto-commit precision, question rate, and question recall on
   the labeled set. Zero model calls in CI, the policy is graded on every PR, and the box is
   only needed to *refresh* transcripts when the prompt or model changes. This preserves
   CALIBRATION_LOOP's two-track structure (`docs/archive/CALIBRATION_LOOP.md:13-27`) while
   surviving the pipeline's deletion.

**The prior survives its test.** The single strongest piece of evidence is in-repo and
model-specific, not from the literature: on the 121-case battery gpt-oss-120b set
`inferred:true` + `domain:health` correctly on **7/7** sensitive facts and then still proposed
`commit` — "a disposition-*selection* miss, not a perception miss"
(`ENTITY_GRAPH_INGEST_V2_PLAN.md:542-545`). A model that perceives risk accurately and
consistently chooses to write anyway is exactly the model for which "the server decides, the
model may only lower" is correct.

---

## 1. What the deterministic arbiter actually weighs today

Line numbers are **current** as of this branch. Note that `ENTITY_GRAPH_REFOCUS_PLAN.md:74`
and `PREDICATE_CANONICALIZATION.md:60` both cite a `predicate_known` weight signal at
`arbiter.py:179`/`:574` — **that signal no longer exists** [V, `grep` finds it only in docs];
tier-2 predicates carry no penalty (`weight.py:66-68`). Treat those doc lines as stale.

### 1a. The two-input weight model (`backend/src/jbrain/analysis/weight.py`)

| Signal / constant | Value | Where | Effect |
|---|---|---|---|
| `surface_attested` | bool | `weight.py:56` | `True` ⇒ ceiling 1.0; the note is the authority |
| `is_supersede` | bool | `weight.py:58` | inferred + overwrite is the dangerous case |
| `INFERRED_CEILING` | 0.6 | `weight.py:28` | inferred fact cannot exceed |
| `INFERRED_OVERWRITE_CEILING` | 0.4 | `weight.py:31` | inferred fact that overwrites history |
| `COMMIT_THRESHOLDS` | attribute .8 · relationship/state/measurement/event .7 · preference .5 | `weight.py:37-44` | historical per-kind commit bar |
| `DEFAULT_THRESHOLD` | 0.7 | `weight.py:45` | unlisted kinds |
| `effective_weight` | `min(self_report, ceiling)`; attested ⇒ full ceiling | `weight.py:74-87` | **the anti-inflation rule** |

The anti-inflation rule is already the owner's prior, in code, with the reasoning stated
verbatim at `weight.py:1-8`: *"The agent's self-confidence is untrusted content … so it may
only ever lower a deterministic ceiling … it can make the system more cautious, never more
permissive."* This design does not invent the principle; it inherits it.

**Already dead:** `commit_status`/`assess` — the weight-ceiling *review gate* was retired by
Lever A (`weight.py:90-95`). A fact commits by default today. The thresholds survive only for
the audit trace. So the redesign is not removing a gate that exists; it is choosing what
replaces a gate that was already removed.

### 1b. Forced-review cases in `plan_intent` (`arbiter.py:95-190`)

| # | Case | Where | Rule |
|---|---|---|---|
| F1 | Fatal structural violation | `arbiter.py:115-123` | rejects the **whole** intent; nothing is written (no partial commit) |
| F2 | Ambiguous mention | `arbiter.py:129-130` | forces every fact on that ref to review, weight irrelevant |
| F3 | Cross-subject attribution | `arbiter.py:131-132` | same; a cross-*subject* misattribution is treated as a leak (`ANALYSIS.md:173-176`) |
| F4 | I5 sensitive-inference net | `arbiter.py:159-172` | `inferred` **and** `domain_floor(predicate) is not None` ⇒ hold. Keyed on the **deterministic floor**, explicitly *never* on a model-supplied per-fact domain (firewall red-team) |
| F5 | Merge / distinct-from proposals | `arbiter.py:83-84,184-189` | never auto-enacted; the agent never folds identity |
| F6 | Missing signals | `arbiter.py:56-59` | `_CONSERVATIVE = (surface_attested=False, is_supersede=True)` — absent evidence is the most cautious reading |
| F7 | Correction notes | `arbiter.py:137-144` | full weight **only** for surface-attested facts; an *inferred* fact inside an owner correction gets no elevation |

Structural fatals (`intent.py:158-282`): `resolution_missing_entity`, `resolution_missing_new`,
`unknown_entity_ref`, `unknown_object_ref`, `bad_kind`, `bad_assertion`, `bad_confidence`
(`self_confidence` outside `[0,1]`), `bad_supersession_action`, `*_empty_id`. Plus one
`review`-severity: `surface_fact_unanchored` — claims attestation, supplies no span
(`intent.py:241-249`).

### 1c. Attestation: what "surface_attested" actually means (`arbiter.py:617-672`)

This is the repo's decade of judgment and the part most worth preserving. The model's claim is
**never** taken at face value; the server independently checks the note text (`_norm`
collapses whitespace/case, `arbiter.py:232-236`; `_token_present` uses word-edge lookarounds,
not substring containment, so a `7` does not match inside `$17`, `arbiter.py:239-250`):

| Backstop | Where | What it grounds |
|---|---|---|
| model quote present in note | `arbiter.py:651-658` | the primary path; gated behind `not fact.inferred` |
| `_object_named` | `arbiter.py:193-213` | the edge's **object** is named verbatim ⇒ the edge is stated even if the quote drifted |
| `_value_attested` | `arbiter.py:253-267` | the stored **value** appears as a standalone token |
| `_relationship_object_named` | `arbiter.py:216-229` | fires **even when the model flagged the edge inferred** — enumerated kinship ("daughters named Summer, Lydian…") is systematically under-flagged |
| `_date_phrase_grounded` | `arbiter.py:580-593` | age → birthDate is deterministic arithmetic over a *stated* age |
| `_gender_grounded` | `arbiter.py:312-329` | "daughter" ⇒ female is a 1:1 implication, not a guess |
| `_time_grounded` | `arbiter.py:596-614` | a resolved clock time printed in the note (OCR'd appointments) |

Three repair passes sit upstream and are *recall* machinery, not confidence machinery:
`recover_dropped_fields` (`arbiter.py:408-496`, backfills object/value/temporal the integrator
drops), `derive_kinship_gender` (`arbiter.py:350-405`), `dedup_intent_facts`
(`arbiter.py:499-577`, collapses re-emitted paraphrases so the owner never sees a card for a
fact already committed).

**The lesson these encode:** the model's self-assessment of *whether it inferred something* is
unreliable in a specific, repeatable direction — it under-flags mechanical derivations as
inferred and it drifts on quotes. Any new design that reads `inferred` as gospel will
regress every one of these cases.

### 1d. Signals living outside the arbiter (still binding)

| Signal | Where | Rule |
|---|---|---|
| Domain floor | `extraction.py:159-192` | hardcoded predicate→domain allowlist (health/finance/precise-location); **registry-independent**, so a schema trim cannot weaken it. `weight`/`temperature` deliberately excluded as ambiguous |
| Domain ratchet | `extraction.py:195-208` | up is free, down or across needs review — asymmetric by doctrine (`ANALYSIS.md:328-331`) |
| Per-kind conflict policy | `ANALYSIS.md:106-113`; `supersession.py:526-796` | event/measurement **never** auto-supersede; `attribute` **never** auto-supersedes (`:605-624`) because a collision is the primary hidden-merge signal; state/preference/functional-relationship newest-wins silently with retained history |
| Validity-time ordering | `supersession.py:3-6` | "newest" = validity time, never capture time — a retrospective note must not become the head |
| Low-confidence overwrite guard | `supersession.py:183,718-731,758-781` | `self_confidence < 0.5` **and** below the incumbent's ⇒ park the candidate, keep the incumbent (the blurry-OCR guard) |
| Pinned-override immutability | `supersession.py:610` etc. | re-flag, never flip: a human decision survives reprocessing |
| Same-name identity gate | `pipeline.py:581-590`, `entities.py:584` | 2+ live entities share the surface ⇒ the *engine* decides, the agent's own `existing` resolution is overridden |
| Value shape | `schema/models.py:199,225`; `pipeline.py` shape check | `validate_value`/`coerce_value`, declares-guarded |
| Untrusted-content doctrine | `ASSISTANT.md:953-955`, `:1109-1112` | "Importance from *content* is untrusted and capped; only owner-confirmed signals raise priority" |

The whole set is I1–I9, tabulated at `ENTITY_GRAPH_INGEST_V2_PLAN.md:223-239`. **Every one is a
risk input for the new gate.** None of them needs the arbiter object to survive.

---

## 2. The decisive in-repo evidence (why a naive confidence split fails)

`ENTITY_GRAPH_INGEST_V2_PLAN.md:515-583` records a 121-case adversarial battery run **on the
owner's box against `local:gpt-oss-120b`**, audited by four independent reviewers. Findings
that bear directly on this design [V, in-repo, model-specific]:

- Genuine model-error rate **8.3 % (10/121)** after removing scorer brittleness and probe-prompt
  gaps. The model is *capable* at the meaning task.
- **The detection→abstention gap (P3):** on the sensitive net the model set `inferred:true` +
  `domain:health` correctly on **7/7** facts and then proposed `commit` on all of them. It
  perceives risk and does not act on it.
- **Flag-stripping is the only class that matters for safety:** two cases stripped a flag (an
  inferred mood tagged `inferred:false`; a *future* job start tagged `asserted`+`supersede`,
  flipping the current employer). "Because the engine can only *add* review, never remove it,
  over-escalation is mere noise but a stripped flag silently defeats a safety floor."
- **Near-determinism:** 1 flip in 5 cases rerun ×3.
- Both flag-strips were fixed by **prompt + schema** changes, not architecture.

Also recorded: **full agentic ingestion was evaluated and rejected**
(`ENTITY_GRAPH_INGEST_V2_PLAN.md:585-613`) on three grounds — re-run determinism, injection
surface, and "the literature does not show agentic memory is more *accurate*, only more
flexible." The proposed change re-opens that decision. Whatever L1/L3 conclude about the agent
architecture, **the confidence design must answer the determinism objection**: an agent that
chooses what to read produces a different write set each run, and JBrain re-analyses notes on
model/prompt upgrades (`ANALYSIS.md` "a silent flip is the one outcome no layer may produce").
A server-side ceiling helps here — the *gate* is deterministic even when the *proposal* is not
— but it does not by itself make re-analysis idempotent. **Flagged as the largest unresolved
risk in this area; it belongs to L1/L3, not to this document.**

---

## 3. The five candidate signals, evaluated

### (a) Model self-report as a tool parameter — **use as a flag, never as a float**

Literature is consistent and blunt: LLMs are systematically overconfident when verbalizing
confidence, with values piling up in the 0.8–1.0 band regardless of accuracy
([Xiong et al., arXiv 2306.13063](https://arxiv.org/abs/2306.13063) — "LLMs tend to be highly
overconfident when verbalizing their confidence"; [Large Language Models Are Overconfident in
Their Own Responses](https://arxiv.org/pdf/2606.03437); [Wired for Overconfidence: A
Mechanistic Perspective](https://arxiv.org/html/2604.01457)). The mechanism matters: the string
"0.9" is a *token choice conditioned on training-corpus confidence language*, not a readout of
an internal probability.

The nuance that cuts the other way: [Tian et al., "Just Ask for Calibration"
(arXiv 2305.14975)](https://arxiv.org/abs/2305.14975) found verbalized confidence **better**
calibrated than the model's own conditional probabilities for RLHF-tuned models, often halving
ECE. So verbalized confidence is not worthless — it is a usable *ranking* signal that is not a
probability. [Kadavath et al. (arXiv 2207.05221)](https://arxiv.org/abs/2207.05221) adds the
key qualifier: zero-shot `P(True)` is *poorly* calibrated and sits near 50 % for typical
samples; calibration appears with the right format and with the model seeing multiple of its
own samples first.

**Repo-specific reasons to demote the float further:**
- The prompt *teaches* the model that its number can only lower a ceiling
  (`integrate_note.prompt:130-131`), which is an incentive to write a high number.
- The only production consumer of the raw float is the OCR guard at
  `supersession.py:718-731,758-781`, thresholded at `LOW_CONFIDENCE = 0.5`. A single threshold
  at 0.5 over a distribution known to cluster ≥ 0.8 fires almost never — **[A]** no measurement
  of the actual `self_confidence` histogram exists in this repo; I could find no recorded box
  transcripts (`backend/evals/box/` contains drivers only). **Measure this first; it is one
  SQL query over `facts.self_confidence` and it will settle the question empirically.**
- `intent.py:234-238` validates the float's *range* and nothing else — a `0.95` on garbage is
  structurally valid.

**Verdict:** replace `self_confidence: float` with `certainty ∈ {stated, inferred, unsure}` +
`doubt: str`. `stated` claims a receipt (§0.3) and must carry a span the server checks.
`unsure` is a monotone step *down* the autonomy lattice — the abstention channel the battery
showed the model does not use when the choice is framed as a disposition. If a float is kept
for continuity, treat it as an ordinal rank inside a bucket, never as a probability, and
recalibrate it post-hoc (histogram binning on the labeled set is the standard, cheap fix; see
§5).

### (b) Token logprobs from the local stack — **available; use narrowly**

**Verified:** the serving stack is llama-swap in front of `llama-server` (llama.cpp)
(`backend/src/jbrain/llm/llama_swap_config.py:1-30`, `local_gateway.py:1-30`). llama.cpp's
OAI-compatible `/v1/chat/completions` accepts `logprobs`/`top_logprobs` and returns
**pre-sampling** `log(softmax(logits))` — the PR that added it explicitly rejected
post-sampler probabilities as "essentially worthless"
([ggml-org/llama.cpp#10783](https://github.com/ggml-org/llama.cpp/pull/10783)). The native
`/completion` endpoint has `n_probs` + `post_sampling_probs`
([server README](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md)).
Pre-sampling matters enormously here: the ingest path is **grammar/JSON-schema constrained**
(`openai_compat.py:188-201` sets `response_format`), and a post-grammar probability of a
forced token is trivially ~1.0. Pre-sampling logprobs are not contaminated by the mask.

**Verified absent:** the repo's client never sends or parses them (`openai_compat.py:188-232`,
`:275-300`); nothing in `backend/src/jbrain/` reads a chat logprob. The only logprob use in the
codebase is Whisper's `avg_logprob → exp() → [0,1]` confidence
(`backend/src/jbrain/transcribe.py:208-213`) — a good precedent and a good template.

**Costs/caveats [A]:** (1) attribution — you get a token stream, so locating the *value span*
inside the JSON arguments requires reconstructing offsets; doable, not free. (2) gpt-oss-120b
is MXFP4-quantized (`local_catalog.py:581-583`) and served with reasoning traces; the distribution
over reasoning tokens is noise for this purpose. (3) whether logprobs survive llama-swap's proxy
and this box's specific llama.cpp build is unverified — a 10-minute debug-console probe settles
it. (4) the literature is split on whether logprobs beat verbalized confidence
([Asking Is Not Enough: Protocol Sensitivity](https://arxiv.org/pdf/2605.27752);
[Comparing Uncertainty Measurement and Mitigation Methods](https://arxiv.org/pdf/2504.18346)),
so do not expect a general-purpose win.

**Verdict:** build the plumbing (small, reversible), then use mean token logprob over the
**value tokens only** as a garble/OCR detector replacing the `self_confidence` float in the
`supersession.py` low-confidence guard. That is a signal about *transcription of a datum*,
which is exactly the regime where token probability is meaningful, and it removes the one
production dependency on the model's number.

### (c) Self-consistency (sample N, check agreement) — **reject as default, keep for calibration**

**Cost on this box [V inputs, A arithmetic]:** gpt-oss-120b runs ~31 t/s
(`local_catalog.py:585`); a v2 integrate call produced ~950 output tokens per probe
(`ENTITY_GRAPH_INGEST_V2_PLAN.md:494`), before reasoning tokens at production-high effort. A
cold 12k-token prompt "spends tens of seconds in prompt evaluation" (`prefill.py:3-5`).
Repeated samples over an identical prefix reuse the KV cache (`--cache-reuse`,
`openai_compat.py:52-54`), so N samples ≈ 1× prefill + N× generation ⇒ **N=3 ≈ 2–5 min/note,
N=5 ≈ 4–8 min/note**. Sampling defaults are the vendor's temp 1.0 / top_p 1.0
(`local_catalog.py:571-574`), so the samples genuinely differ.

**The killing argument is not cost, it is information.** The battery measured 1 flip in 5
cases rerun ×3 (`ENTITY_GRAPH_INGEST_V2_PLAN.md:541`). A near-deterministic model produces
near-unanimous agreement on both its right and its wrong answers, so agreement rate has almost
no discriminative power — while self-consistency's known cost is exactly this repeated sampling
([self-consistency / semantic-entropy survey, arXiv 2510.20460](https://arxiv.org/pdf/2510.20460)).
Structured-output clustering also has to be exact-match-after-normalization rather than
semantic, which makes the agreement measure brittle to harmless key ordering.

**Verdict:** reject per-note. **Keep it as the owner-run calibration instrument** — which is
what `CALIBRATION_LOOP.md:110-116` constraint 3 already prescribes ("N samples/case, report a
rate, flag high variance"). Use N=5 on the labeled set to measure *policy stability*, which is
also the honest way to answer the re-run-determinism objection from §2.

### (d) Cheap second-pass verifier (same model, different framing) — **narrow yes**

Same-model verification is weak where it is a *quality judgment*: self-preference bias inflates
scores on a model's own output, and false-positive rates rise with solver–verifier similarity
([Quantifying and Mitigating Self-Preference Bias of LLM Judges](https://arxiv.org/pdf/2604.22891);
[Humans or LLMs as the Judge?](https://arxiv.org/pdf/2402.10669);
[When Should a Language Model Trust Itself?](https://arxiv.org/pdf/2605.02915)). With one local
model there is no cross-family verifier available, so the generic "add a judge" move is closed.

It is *not* weak where the second pass produces a **server-checkable artifact**. Ask only:
"the note is below. Does it literally state ⟨claim⟩? If yes, quote the exact words." The
server then runs the existing `_norm` + `_token_present` check (`arbiter.py:232-250`). The
model cannot self-prefer its way past a string comparison. This is also precisely the fix the
battery's A/B validated — an improved prompt fixed 8/10 genuine errors and **both**
flag-strips (`ENTITY_GRAPH_INGEST_V2_PLAN.md:553-556`).

**Verdict:** run it only on writes the ceiling placed at `ask`, and only as an
attestation re-elicitation. Its sole possible effect is `ask → commit`, so a compromised or
sycophantic second pass can only reduce owner burden on a fact whose quote checks out; it can
never launder an unsupported claim. Budget ~1 short call (≈200 output tokens ≈ 7 s) per ask
candidate.

### (e) Deterministic structural risk signals — **this is the design**

Every item is computable server-side from (the proposed write, the current graph, the note
text) with no model involvement, and every one is already implemented or trivially derivable:

| ID | Risk signal | Source today |
|---|---|---|
| S1 | Firewall domain (health/finance/precise-location) | `extraction.py:159-192` `domain_floor` |
| S2 | Domain would ratchet down or across | `extraction.py:195-208` |
| S3 | Cross-subject attribution | `arbiter.py:131-132`, `intent.py` `cross_subject` |
| S4 | Ambiguous mention / same-surface collision (2+ live matches) | `arbiter.py:129-130`; `pipeline.py:581-590`; `entities.py:584` |
| S5 | Novel entity (mint) vs resolve-to-existing | `EntityResolution.mode` |
| S6 | Address already has an active head (contention) | `supersession.decide` inputs |
| S7 | Head is **pinned** (a human decided this) | `supersession.py:610` |
| S8 | Kind is `attribute` and would collide | `supersession.py:605-624` |
| S9 | Would supersede by *capture* time rather than validity time | `supersession.py:3-6` |
| S10 | Not surface-attested (the receipt failed) | `arbiter.py:617-672` |
| S11 | Value shape invalid / needs coercion | `schema/models.py:199,225` |
| S12 | Predicate never seen: undeclared **and** absent from this owner's graph | `declares_predicate` `schema/models.py:167` + a graph count |
| S13 | Low retrieval support — the agent wrote to an address whose neighborhood was not in its context | new; requires logging what the agent read |
| S14 | Blast radius — merge, distinct-from, retract, or N writes on one address in one note | `arbiter.py:83-84` |

S12 deserves care: the two-tier model is explicit that an unregistered predicate carries **no**
penalty (`weight.py:66-68`, `ENTITY_GRAPH_REFOCUS_PLAN.md:88-93`) — long-tail facts commit on
the same terms as declared ones. So S12 must be "never seen *in this graph* before", not
"undeclared", or the redesign silently reinstates the review noise the refocus plan removed.

S13 is the genuinely new one and the one an *agentic* design makes both possible and necessary:
when the agent chooses its own reads, "did you look before you wrote?" becomes a checkable
server-side fact. Log every read tool call; a write to an address the agent never retrieved is
a blind write and steps down the lattice.

---

## 4. The recommended design

```
agent proposes a write  ──►  server computes ceiling = min over S1..S14
                                        │
                        certainty=unsure │ steps down one level
                        certainty=stated │ (only) if the span verifies, steps up one level
                                        ▼
        commit ──── commit_and_log ──── ask ──── refuse
```

**Level semantics**

- `commit` — writes silently. Everything is still provenance-stamped and reversible.
- `commit_and_log` — writes, and appears in a browsable **recent writes** feed the owner can
  skim and undo. This is the level that absorbs most volume, and it is the mechanism that
  makes being wrong cheap — the exact lesson from the industry survey: *systems skip the inbox
  by making being wrong cheap (non-destructive versioning), not by trusting the LLM*
  (`ENTITY_GRAPH_INGEST_V2_PLAN.md:154-160`).
- `ask` — a conversational question, subject to the budget in §5. The write is held, not
  committed.
- `refuse` — never written by the agent; only a deterministic path may perform it. Reserve for
  S7 (pinned head), S14 (merge / distinct-from / retract), and any write the firewall would
  ratchet downward. `refuse` is not "ask harder"; it is "this class is not the agent's to do."

**Non-negotiables carried forward.** Whole-write atomicity on a structural violation (F1);
merges and identity folds never enacted by the model (F5); the domain floor computed from the
predicate, never from a model-supplied domain (F4 — this was settled by an independent
firewall red-team, `ENTITY_GRAPH_INGEST_V2_PLAN.md:734-760`); validity-time supersession (S9);
pinned-override immutability (S7).

**Where I disagree slightly with the prior.** "Self-report only ever lowers" is right and must
be enforced structurally (a ternary flag, not a float). But a pure never-raise design is what
produced the arbiter's eight backstops and the card flood — an unconditional ceiling with no
escape hatch over-asks on facts the note plainly states. The receipt channel (§0.3) is the
principled escape hatch: the model does not raise the ceiling, *the note does*, and the server
verifies. Keep it, and keep it as the **only** raise channel.

---

## 5. The question-worthiness bar

**A question must be all four of:**

1. **Owner-answerable.** The uncertainty must be *reducible by the owner* — underspecification
   or ambiguity in the note, not model difficulty. This is the aleatoric/epistemic split
   applied to agents: a dedicated channel for goal/reference ambiguity, distinct from a single
   confidence score that conflates ambiguity with task difficulty
   ([Uncertainty Decomposition for Clarification Seeking in LLM Agents,
   arXiv 2606.19559](https://arxiv.org/html/2606.19559)). "Which Bob?" qualifies. "Am I sure
   this is a `state` and not an `attribute`?" does not — never ask the owner a schema question.
2. **Consequential.** The wrong answer changes what a reader would believe or act on: a
   firewall-domain write, an identity fold, an overwrite of an existing head, a
   safety-relevant value.
3. **Not cheaply resolvable otherwise.** The attestation re-elicitation (§3d) runs first; only
   what survives it is asked.
4. **Within budget.**

**Budget (proposed, to be tuned — see §6):**

| Knob | Value | Rationale |
|---|---|---|
| Questions per note (median) | **0** | most notes should be silent |
| Questions per note (mean target) | **≤ 0.2** | ~1 note in 5 |
| Hard cap per note | **2** | a note that raises 5 questions is a note the agent should have partly refused, not interrogated over |
| Global open questions | **3** | the queue must be visibly finite |
| Question TTL | **7 days** | expires to the safe default (write held, not committed); never silently re-asked |
| Re-ask trigger | a *new* note re-raises it | recurrence is evidence it matters |

**Why a rate and not a threshold.** At ~10 notes/day **[A — the owner's actual capture rate is
unmeasured and is the single most load-bearing unknown here]**, 0.2 q/note ≈ 2 questions/day,
which a person answers. 1 q/note ≈ 10/day, which becomes a queue, which becomes approval
fatigue — the failure mode `ASSISTANT.md:1105-1112` names as a red-team threat against the
staging lever. Make the budget a hard constraint and let the ranking (expected harm ×
reducibility) decide *which* questions get spent, rather than letting a threshold decide *how
many*.

**Tuning it without the calibration harness.** The right frame is **selective prediction with
distribution-free risk control**, not threshold-by-vibes. With a small labeled holdout, split
conformal / conformal risk control gives a finite-sample, distribution-free bound on the
auto-commit error rate while minimizing the abstention (question) rate
([Mitigating LLM Hallucinations via Conformal Abstention, arXiv 2405.01563](https://arxiv.org/pdf/2405.01563);
[Taming Variability: Randomized and Bootstrapped Conformal Risk Control,
arXiv 2509.23007](https://arxiv.org/pdf/2509.23007);
[CAP: Conformalized Abstention Policies](https://openreview.net/pdf?id=DA1ELJTudh)). The
practical consequence is a hard, honest constraint that should be stated to the owner up front:
**the finite-sample bound is roughly α ≥ 1/(n+1)**, so with n = 56 corpus cases you cannot
certify better than ~1.8 % error; certifying a 1 % auto-commit error rate needs n ≳ 300
labeled facts. Fact-level labelling of the existing 56-case graded corpus plausibly yields
300–500 labels **[A]** — i.e. the labeled set the design needs probably already exists, unlabeled.

Also apply the cheap classical fix to whatever ordinal signal survives: **histogram binning /
isotonic regression on the holdout**, which reliably cuts ECE by an order of magnitude on
verbalized confidence (reported drops from 0.243 → 0.038/0.029 under Platt/isotonic in a
medical-VQA study, [arXiv 2604.02543](https://arxiv.org/html/2604.02543v1)).

---

## 6. Measuring calibration going forward

**The labeled set.** `backend/tests/eval/corpus/*.json` — 56 graded cases today [V: 19 domains,
16 identity, 17 lifecycle, 2 predicates, 2 temporal], each a note plus a machine-checkable
`expect` block, already run through the real production chain with both intent-level and
**DB-mode** (committed-graph) assertions (`backend/tests/eval/README.md`). Extend, don't
rebuild: add the 121-case adversarial battery, and relabel at **fact level** (each proposed
write gets `correct / wrong / harmful-if-wrong`).

**Metrics (all fact-level, all deterministic — no LLM judge, consistent with local-only):**

| Metric | Definition | Proposed gate |
|---|---|---|
| **Auto-commit precision** | correct ÷ auto-committed | ≥ 0.95 overall; ≥ 0.99 on firewall-touching writes |
| **Harmful-write rate** | writes labeled harmful that auto-committed | **0** on the adversarial set — a hard gate |
| **Question recall** | asked ÷ (writes that were wrong **and** harmful) | ≥ 0.90 |
| **Question precision** | questions whose answer changed the write ÷ questions asked | ≥ 0.5 (below this the owner is being used as a coin flip) |
| **Question rate** | questions ÷ notes | ≤ 0.2 |
| **Recall vs. today** | facts committed vs. the current pipeline on the same corpus | no tier-1 regression (the existing acceptance criterion, `ENTITY_GRAPH_INGEST_V2_PLAN.md:322-330`) |
| **Discrimination** | AUROC of the composite risk score vs. correctness | report; a score below ~0.7 means the gate is not separating |
| **Calibration** | ECE over risk buckets, plus a reliability diagram | report, don't gate (small n) |
| **Stability** | N=5 resample of the whole corpus: fraction of writes whose *ceiling* changes | ≥ 0.95 identical — this is the re-run-determinism answer |

Report precision *and* question rate together as a **risk–coverage curve**; a single number
hides the trade the owner actually cares about.

**What replaces `CALIBRATION_LOOP.md` as the CI quality guard.** Keep its two-track shape
(`CALIBRATION_LOOP.md:13-27`) and change what is frozen:

- **CI track (every PR, no model, no box).** *Frozen-transcript policy replay.* Record the
  agent's tool calls from on-box runs as JSON fixtures (one file per corpus case). CI replays
  them through the **pure risk-gate function** and asserts the metrics table above against
  committed baselines. Because the gate is pure, this is fast, deterministic, and gradeable on
  every PR — and it grades the thing that actually changed (the policy), not the model. Plus
  unit tests of the gate at **100 % coverage** (it is a security path under CLAUDE.md #5).
  This is a direct descendant of the existing `check_case`/`check_case_db` pattern, whose gate
  logic is already unit-tested in CI against a faked model
  (`backend/tests/eval/README.md`, `tests/unit/test_eval_assertions*.py`).
- **Box track (owner-triggered, real model).** Refresh transcripts when the prompt, the model,
  or the tool schema changes; run N=5 for the stability metric; produce the reliability
  diagram. Gate a `PROMPT_VERSION` bump on it, exactly as today.
- **The one thing CI cannot catch** and must be stated plainly: a *prompt* change that makes
  the model propose worse writes is invisible to frozen-transcript replay. That is why the box
  track's transcript refresh is a **required** step of any prompt PR, not an optional one — the
  same rule `CALIBRATION_LOOP.md` already applies to `PROMPT_VERSION`.

**Continuous, in-production signal (free, and the only one that reflects the real corpus):**
log every ceiling decision and its inputs; then measure (i) the owner's answer distribution on
asks — if 90 % of answers are "yes, commit it", the gate is over-asking on that signal, and
(ii) the undo rate in the `commit_and_log` feed, which is a *direct* precision estimate on real
notes with no labelling effort at all. Both feed the histogram-binning recalibration in §5.

---

## 7. Verified vs assumed — summary

**Verified in-repo:** every line citation in §1; the anti-inflation principle already exists
(`weight.py:1-8,74-87`); the weight review-gate is already retired (`weight.py:90-95`); no
logprob use anywhere in the LLM path; the serving stack is llama-swap + llama.cpp; gpt-oss-120b
is served at temp 1.0 / ~31 t/s; the 121-case battery results; the 56-case graded corpus and its
DB-mode runner; `predicate_known` no longer exists despite two docs citing it.

**Verified externally:** llama.cpp OAI chat logprobs, pre-sampling (PR #10783); the
overconfidence literature; Tian et al.'s verbalized-beats-logprob result for RLHF models;
Kadavath's zero-shot P(True) caveat; conformal abstention's small-calibration-set guarantee.

**Assumed / needs measurement:** the actual distribution of `self_confidence` in the owner's
`facts` table (one SQL query); the owner's notes-per-day rate; whether logprobs survive
llama-swap and this box's llama.cpp build (one debug-console probe); fact-level label count
recoverable from the 56 corpus cases; all self-consistency cost arithmetic; whether
`qwen3.8-27b-abliterated` co-resides with gpt-oss-120b or forces a swap (the catalog says the
27B twins co-reside, `local_catalog.py:798`, which contradicts the strict "one large model at a
time" framing and changes the vision-path cost).

---

## Open questions for the owner

1. **How many notes per day, really?** Every number in §5 is downstream of this. 3/day and
   1 question per note is fine; 30/day and 0.2 is already too many.
2. **Is `commit_and_log` + undo acceptable in place of most questions?** The design leans hard
   on "make being wrong cheap" rather than "ask more". That requires you to actually skim a
   recent-writes feed occasionally. If you won't, the budget has to shrink and the `refuse`
   class has to grow.
3. **What is the *one* thing you would rather never see wrong in the graph without being
   asked?** Name it and it becomes a `refuse`, not an `ask`. (My default guess: anything that
   overwrites an existing head on a person's health facts.)
4. **Should the float die?** Replacing `self_confidence: float` with a ternary flag is a
   schema-breaking change to the write contract and to `facts.self_confidence`. It is the
   single highest-leverage change in this document, and it invalidates the OCR guard's current
   threshold until logprobs replace it.
5. **Do you accept a question TTL that resolves to "not committed"?** The alternative — a
   question that waits forever — is the silent-queue failure mode; the cost is that a fact you
   meant to keep quietly does not land if you ignore the question for a week.
6. **Re-run determinism.** §2 records that full agentic ingestion was rejected partly because
   an agent that chooses its reads breaks idempotent re-analysis. The ceiling makes the *gate*
   deterministic but not the *proposal*. Is a stability floor (≥ 95 % identical ceilings across
   5 resamples) an acceptable substitute for the exact-idempotence the current pipeline has?
7. **Will you spend one box session labelling?** Fact-level labels on the 56-case corpus are
   the difference between a tuned gate and a guessed one, and n ≈ 300 is the difference between
   certifying 2 % and 1 % error. Nothing else in this design substitutes for it.

---

**Sources:** [Xiong et al. 2306.13063](https://arxiv.org/abs/2306.13063) ·
[Tian et al. 2305.14975](https://arxiv.org/abs/2305.14975) ·
[Kadavath et al. 2207.05221](https://arxiv.org/abs/2207.05221) ·
[LLMs Are Overconfident in Their Own Responses](https://arxiv.org/pdf/2606.03437) ·
[Wired for Overconfidence](https://arxiv.org/html/2604.01457) ·
[Asking Is Not Enough: Protocol Sensitivity](https://arxiv.org/pdf/2605.27752) ·
[Comparing Uncertainty Measurement and Mitigation Methods](https://arxiv.org/pdf/2504.18346) ·
[Systematic Evaluation of Uncertainty Estimation Methods](https://arxiv.org/pdf/2510.20460) ·
[Uncertainty Decomposition for Clarification Seeking](https://arxiv.org/html/2606.19559) ·
[Mitigating LLM Hallucinations via Conformal Abstention 2405.01563](https://arxiv.org/pdf/2405.01563) ·
[Conformal Risk Control 2509.23007](https://arxiv.org/pdf/2509.23007) ·
[CAP: Conformalized Abstention Policies](https://openreview.net/pdf?id=DA1ELJTudh) ·
[Self-Preference Bias of LLM Judges](https://arxiv.org/pdf/2604.22891) ·
[Humans or LLMs as the Judge?](https://arxiv.org/pdf/2402.10669) ·
[When Should a Language Model Trust Itself?](https://arxiv.org/pdf/2605.02915) ·
[Overconfidence and Calibration in Medical VQA](https://arxiv.org/html/2604.02543v1) ·
[llama.cpp PR #10783](https://github.com/ggml-org/llama.cpp/pull/10783) ·
[llama.cpp server README](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md)
