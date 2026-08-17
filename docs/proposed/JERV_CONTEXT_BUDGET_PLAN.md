# jerv Context Budget — spend less on tool schemas, spend some on durable memory

> **Status:** Proposed · **Last verified:** 2026-08-17 · **Waves:** W1◻️ W2◻️ W3◻️

jerv carries **44 tool sidecars ≈ 28.7k tokens of serialized schema on every turn** (27.7k of
description + params, plus the examples `as_llm_tool` appends), before its 413-line persona
prompt and the conversation — and carries **nothing** across sessions except what happens to
be searchable in the video/report libraries. That is the budget backwards: the persona spends
heavily on a mostly-static tool menu and nothing on the standing context that would make each
turn land better.

*(44/28.7k is the fully-wired ceiling and matches the owner's stock stack. Fourteen of the 44
are config- or model-gated by handler absence — the image/whisper/OCR family plus the canvas
trio — so a box without ComfyUI, whisper, RapidOCR, and a grounding-capable vision model
carries 30 tools ≈ 19.2k. The ceiling is the right number to plan against here; it is not a
floor. The count grows: it was 41 when this plan was drafted and 48 before the W1 umbrellas,
which is the standing argument for the trim.)*

This plan **reclaims** schema tokens (W3, and the trim now homed in `TOOL_CATALOG_PLAN` W0b)
and **spends a small, capped slice** of them on a cross-session scratchpad (W2), plus fixes
the identity framing that made the on-box model distrust its own prompt (W1).

Reconciled with the root `CLAUDE.md` non-negotiables: no provider SDK and no new model call
(#1); the scratchpad is a **DB table, not a file area** — no storage-abstraction bypass (#2);
it is a new owner-only table and needs an RLS isolation test whose assertions are spelled out
in §3 (#3); tests land with the code (#5); it must be readable and editable **from the PWA**
(#10, and see §2); docs travel with the waves (#9). Invariant #3's *owner-confirmed-write*
rule is addressed head-on in §2 rather than stepped past.

**This document has been through a four-lens adversarial review** (security red-team,
prior-art/architecture, contrarian, fact-check). §8 records what changed and what was
inherited rather than fixed.

## 1. Where this came from

A tool-selection probe of the live jerv surface (the owner asked the on-box model what in its
own toolset didn't make sense) plus two follow-up questions about code execution and
persistent state. The probe found two real defects, **already fixed and merged** — they are
not part of this plan, but they are why it exists:

- `decompose_research` was in `JERV_TOOLS` purely so the parent⊆child clamp could pass it
  down; it refuses at depth 0, so every call an interactive turn could make would fail.
- `render_bars`' description steered to `render_chart`, which jerv did not hold — so the model
  was pointed at a tool it could not call, and had no way to plot a trend at all.

Both are the same shape: **an allowlist is a prompt, and it carried entries the model could
not act on.** That generalizes into the discipline this plan builds out.

**How much to discount the probe's capability claims.** The model argued for a Python sandbox
partly from "the chart cluster looks planned or trimmed." That inference was wrong in kind —
`render_chart`, `chart_measurements`, and `read_labs` are all fully implemented, and the model
cannot distinguish *not built* from *not granted to me*. But it was not wrong that a gap
exists, and the first draft of this plan overcorrected into "the chart hole never existed."
The accurate statement: `render_chart` and `render_bars` cover **time-series line/area and
categorical bars, ≤6 series**; `charttools.py:115` hardcodes `x_kind: "time"`, `_parse_x`
reads a bare number as epoch-ms, and points are sorted and connected — so **non-date x-axis,
scatter, correlation, histogram, and pie are impossible by construction**, and
`chart_measurements`/`read_labs` are permanently out of jerv's reach (empty read scopes). The
hole was smaller than the model said and is now partly fixed. See §5 row 1 — where the
`htmlrender` service that landed with `AGENT_CANVAS_PLAN` W1b changes what closing the rest of
it would cost.

## 2. The load-bearing constraint: sanction, not framing

The first draft said the scratchpad would be injected DATA-framed, never with follow-this
framing — and then defined its flagship section as `Preferences` (reply style, briefing shape,
what "the usual" means). **Those are instructions.** The design demanded a banner telling the
model not to act on the content whose entire purpose is to change how it acts. Either the
model honours the framing and the feature is inert, or it ignores the framing and the
anti-injection defence is decorative. That contradiction is the single most important thing
the review found, and it is resolved here.

`_plan_blocks` (`api/agent.py:449`) already shows the missing piece. It does not discriminate
by *shape* (data vs instruction); it discriminates by **owner sanction** — "An owner-approved
plan IS a sanctioned instruction; ordinary tool/web output is still not" — and it refuses to
inject an unsanctioned draft at all, because "an injected web page can talk jerv into drafting
a plan, never into approving one."

**So the scratchpad splits by write authority, and framing follows authority:**

| Section | Written by | Injected as |
|---|---|---|
| `Threads`, `Watch` (dated, volatile) | the **agent**, via tool | **data** — inside the sentinel envelope, explicitly non-executable |
| `Preferences`, `Corrections` (standing rules) | the **owner only**, from the PWA | **sanctioned instruction**, like an approved plan |

This is why the review's threat model collapses: a malicious page can, at worst, get a dated
`Threads` line written — read as data, expiring, and visible in the owner's pane. The durable
standing-order prize (`Corrections` = "things never to get wrong again" is a rule by
construction) is simply outside the agent's reach. It also settles invariant #3 without a
special pleading: behavioural memory keeps its owner-confirmed-write property, and only the
metacognitive, dated half is agent-auto-write — the same split `ASSISTANT.md:850-853` already
draws between owner-confirmed core memory and the agent-auto-write task scratchpad.

**Prose framing alone is below this repo's own standard, so the agent-written half reuses the
mechanism that already exists.** `briefs.py:105-133` defines a pinned `untrusted_external_data`
sentinel plus `neutralize_boundary()`, which strips any sentinel the untrusted text contains —
built because "a summary could emit its own closing tag and break out of the envelope, landing
its payload as apparent top-level instruction." The `Threads`/`Watch` block is wrapped in that
envelope and run through `neutralize_boundary()` **at inject time, not write time** (the DB is
persistence; a write-time filter is bypassed by a later PWA edit), with the matching
inert-boundary clause pinned in `jerv.prompt`.

**What this plan inherits and does not fix.** The review found that `_plan_blocks` itself
launders agent-written, web-derived text into its instruction envelope: `write_plan(body=…)`
with `status=None` rewrites an approved plan in place without demoting (`plantools.py:163-165`,
`models/plan.py:299`), and `write_plan_result` notes render into the same sanctioned block. That
is a real, pre-existing defect. This plan borrows `_plan_blocks`' **principle** (sanction is the
discriminator) and not its implementation; the laundering defect is filed in §8 as separate
work, and W2 must not be read as depending on it being clean.

**On the owner surface.** `archivist_memory` has no PWA surface at all — repo-wide grep finds
only a tool-step label in `frontend/src/agent/toolSummary.ts:52-53`. It *is* readable without a
terminal via `POST /api/debug/sql` (`debug.py:603`), a sanctioned no-terminal path under #10 —
so the accurate charge is that it is **invisible in the PWA and not editable at all**, not that
it is unreachable. That is still the wrong shape for state that shapes every session, and under
the split above the owner surface is no longer a convenience: it is the **write path** for
`Preferences`/`Corrections` and therefore the sanction mechanism itself. It is in-scope for W2,
not a follow-up.

## 3. Waves

Execution order. The trim that was W3 in the first draft now lives in
`TOOL_CATALOG_PLAN.md` as **W0b** (see §8) and must land **between W1 and W2** — see §4.

### W1 — the identity paragraph

`jerv.prompt:388-402` is *literally accurate* — jerv reads no notes, entities, lists, or
appointments — but incomplete: it never says jerv **does** own the analysed-video corpus, the
research-report library, and host metrics. The probe read that as a stale prompt and said it
would "trust the tools" over it. A model discounting its system prompt is discounting the
**location firewall**, which lives in that same paragraph.

*(The first draft also claimed the anti-injection rule was in that paragraph. It is not — it is
a separate final paragraph at `:411-413`. The location-firewall half of the risk stands; the
anti-injection half does not, and the argument is not stronger for overstating it.)*

Rewrite to state both halves — what jerv owns, what remains out of reach — and to name the
scratchpad from W2 as its third local store. Bump the prompt version.

**Gate.** `scripts/prompt-eval.sh` is **not** a gate for this: it runs `evals/run.py`, which is
`note.extract`-only, and `grep -rn jerv backend/evals/` returns nothing. Editing the paragraph
that carries the location firewall with zero behavioural coverage is not acceptable, so W1
ships a new scenario in the `adv_prompt_injection_*` harness family asserting (a) the location
firewall holds under a poisoned page and (b) an injected standing-order line is not obeyed.
Building that scenario is part of W1, not a follow-up.

### W2 — the scratchpad

**Generalized, not cloned.** The first draft proposed `app.jerv_memory` as a byte-for-byte copy
of `0094_archivist_memory` — which would build the §2 framing fix, the owner surface, the RLS
test, and the repo for persona #2 while leaving persona #1's identical gaps open. Instead:

- **Migration** — `app.agent_scratchpad (principal_id text, persona text, content jsonb NOT
  NULL DEFAULT '[]', updated_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY (principal_id,
  persona))`, `ENABLE`+`FORCE` RLS, an `app.is_owner()` USING+CHECK policy, SELECT/INSERT/
  UPDATE/DELETE to `jbrain_app`. The archivist's existing row migrates into it in the **same
  migration**, and `archivist_memory` is dropped. One repo, one RLS test, one injection helper
  parameterized by persona, one PWA pane — and the §2 framing fix lands for both personas.

- **Why not the existing memory substrate.** `agent_memory` (`0017`) is neither KB-coupled nor
  embedding-backed — the vectors are on `agent_episodes`. What disqualifies it is the RLS:
  `USING (app.is_owner() AND app.has_domain_scope(domain_code))`, and under `owner_scoped=true`
  a jerv session with `read_scopes = ()` (`api/agent.py:720`) can read and write **exactly zero
  rows**. `archivist_memory`'s bare `is_owner()` policy is why the scope-less personas can use
  it at all. That, not "mirroring 0094," is the reason for a separate substrate.

- **Structured entries, not a prose blob.** `content` is a JSONB array of
  `{section, text, as_of}`, and the write tool takes **add / update / remove** ops. This is not
  a preference: `ASSISTANT.md:863-865` and `memory.py:7-10` both mandate **ACE-style delta
  edits, never full rewrites**, because "full regeneration rots accumulated self-knowledge
  (brevity bias / context collapse)" — and the staleness failure this feature exists to prevent
  ("confidently used a watch item from three months ago") *is* context collapse under another
  name. Delta ops also avoid re-emitting the whole document on every update (a ~750-output-token
  rewrite on the local model, with silent drops on a model documented to fabricate precision),
  and turn "prune what you know is done" from a prose hope into `WHERE as_of > now() - interval`.

- **Cap: 3 KB rendered**, ~750 tokens on the volatile suffix — ~3% of schema spend, which
  `TOOL_CATALOG_PLAN` W0b pays back several times over. Over the cap, an **add** is refused
  naming the oldest prunable entries, rather than forcing a summarizing rewrite.

- **Write authority and framing** — exactly as §2 specifies. Agent tool writes `Threads`/`Watch`
  only; `Preferences`/`Corrections` are owner-write-only from the PWA. The agent-written block
  is sentinel-wrapped and `neutralize_boundary()`-filtered at inject time.

- **Injection scope.** jerv turns run on three paths — `/chat` (`api/agent.py`), headless Tasks
  (`tasks/runner.py:246`), and plan continuations (`continuation.py:304`) — and only the first
  would call a `_plan_blocks` sibling. Injecting on `/chat` alone means a 6am Task can poison
  memory that lands in the next interactive chat; injecting everywhere means the unattended loop
  gains standing context with nobody watching. **Resolution: read on all three; the write tool
  refuses when there is no live owner turn.** A background run may use memory, never write it.

- **Permission class — acceptance criteria, not an implementation detail.** `permission: web`,
  both tool names added to `NEVER_DEFAULT`, and a unit test asserting
  `registry.allowed_names(scopes, allow=None)` excludes them. `deep_produce` is the precedent
  for why this must be explicit: it is `read`-class and needed a `NEVER_DEFAULT` entry to stay
  out of curator's wildcard (`toolregistry.py:36-38`). Get this wrong and a health-scoped
  curator turn silently gains a write path into a table jerv reads every turn — and per the RLS
  note below, Postgres will not stop it.

- **RLS isolation test — what it must assert.** The `0094` test is the floor, not the ceiling:
  (1) a non-owner sees zero rows and fails to write, extended beyond `capability_token` to
  `intake_link`, the one non-owner kind that actually drives an agent turn; (2) both
  `relrowsecurity` and `relforcerowsecurity` are true; (3) **a health-only `owner_scoped=true`
  context CAN still read and write** — asserting the true behaviour, so the absence of a domain
  firewall on this table is a documented, tested decision rather than an assumption someone
  later mistakes for isolation; (4) `principal_id` comes from `ctx.session.principal_id` only,
  never from tool arguments.

- **Sub-agent invariant.** No child persona holds these tools today (the clamp is a plain
  intersection, `spawn.py:148-154`), but `DEEPEST_RUN_TOOLS` derives from `JERV_TOOLS`, so the
  ceiling widens automatically. One test asserting `MEMORY_TOOLS ∩ persona.tools == ∅` for every
  persona in `SUBAGENT_PERSONAS` converts an accident into an invariant. This matters because
  `spawn.py:968` states that a child's brief **is** its instruction — so a memory line folded
  into a brief is a one-hop data→instruction laundering path if a child ever holds the tools.

- **Location firewall.** The write tool refuses to persist coordinates or a street address.
  `presencetools.py:98-112` can return both on request; memory would convert a tool-gated,
  per-turn read into ambient context in every future prompt.

### W3 — config-derived lists in the tool schemas

Two "call it blind" complaints share one cause: facts known to the process are not in the
schema the model reads. `news_feed`'s categories come from `Settings.news_feeds`;
`portal_search`'s jurisdiction/kind pairs come from the wired resolvers, whose steering message
only fires *after* a call that omitted them.

Three corrections from review, all of which change the design:

1. **The seam is `schemas_for()`, not registry load.** `as_llm_tool()` is called per turn from
   `ToolRegistry.schemas_for` (`toolregistry.py:164`), not at load. `RegisteredTool` is a frozen
   `(toolfile, handler)` pair and `load_registry` takes no settings — the config lives one level
   up in `build_registry` (`readtools.py:678-712`). Threading it is real work this plan must
   budget, and computing per-turn is *better*: `news_feeds` becomes owner-editable from PWA
   Settings in `NEWS_FEED_PLAN` Wave C, which a load-time snapshot could not track.
2. **Prose list, never a JSON-Schema `enum`.** `analyze_stream.tool:5-12` documents that an
   `enum` on a property of a many-optional-property object **deterministically segfaults** the
   gpt-oss harmony/GBNF path — bisected via the tool-probe, with a regression test.
   `portal_search` is a six-property, one-required object: structurally the class implicated.
3. **Reuse the existing helper.** `advertised_capabilities(resolvers)` already computes the
   portal jurisdiction/kind list (`portaltools.py:30,64`); W3 calls it rather than re-deriving.

**Honest scoping of the win.** `news_feed.tool` already enumerates all six categories and they
match config exactly today, so the defect there is **drift risk**, not a forced discovery round
trip. Only `portal_search` has the discovery problem — and W3 also retires the static portal
list committed as a stopgap, which is exactly the hardcoded-fact rot `DOC_LIFECYCLE` R1 warns
about.

**Digest governance — an open question this inherits.** `ToolFile.digest` hashes what
`load_tool` reads from disk, and the pin test calls `load_tool` directly, so
dynamically-injected prose **does not break the pin** — it escapes it. That routes model-facing
text through a path the CI version guard is structurally blind to. `TOOL_CATALOG_PLAN` §5 already
posed this question for `summary`/`family` and deferred it to its W0; W3 must not ship before
that decision is taken there. See §7.

## 4. Sequencing

**W1 → `TOOL_CATALOG_PLAN` W0b → W2 → W3.**

- **W1 first** because it costs one paragraph plus a harness scenario, and it fixes the defect
  that contaminates every other measurement. Probing tool selection while the model is
  discounting its own system prompt measures the wrong thing.
- **W0b (the trim) before W2** for a better reason than the first draft's token arithmetic. W0b's
  acceptance is a before/after comparison on the assembled prompt; W2 injects a new
  always-present block into that same prompt. Ship W2 first and W0b's baseline has moved. The
  "~750 tokens are affordable regardless" argument is true and irrelevant.
- **W2 third** because it is the only wave with a migration, a data migration of another
  persona's row, an RLS isolation test, and a PWA surface — the wave most likely to slip, and
  the one that should not hold the cheap wins hostage.
- **W3 last** because its digest-governance question is a decision owed by another document.

## 5. What this deliberately does not build

Recorded so review attacks the *rejections* too. Three of these rows reached a defensible
conclusion via evidence that did not survive contact with the code; the evidence is corrected.

| Rejected | Why |
|---|---|
| **Python + file-execution sandbox** | Rejected on **cost**, not on absence of a gap — the first draft's "the chart hole never existed" was wrong (§1): scatter, non-date x-axis, correlation, and >6 series are impossible by construction, and the number-invention failure class is documented, recurring, and unfixed after three prompt versions (`DEEP_RESEARCH_SCRATCHPAD_PLAN.md:46-49,147-150`) in the exact model that `render_chart` invites to plot "a count you tallied." The cost argument stands on its own: #2 makes an exec tool a by-construction hole in the storage abstraction, and #10 makes its failure modes undebuggable without a terminal. **The rejection got stronger while this plan was in review.** `AGENT_CANVAS_PLAN` W1b shipped `htmlrender` — a general HTML+CSS→PNG sidecar on an `internal: true` network, explicitly "the sanctioned path for any tool wanting rich visual output," which never sees owner images. That is a route to *every* missing plot shape with no storage-abstraction hole and no terminal dependency, and its reasoning is this plan's §2 in another key ("the model gets a language it is fluent in, and the PWA only ever receives an image — pixels cannot execute"). So the plot-shape half of the Python case is now servable by an existing sanctioned path. **Revised trigger:** the only surviving argument is the *deterministic-arithmetic* one — revisit if a mechanical backstop is ever chosen over the prompt-discipline lineage that owns number-invention today. A missing chart shape is now an `htmlrender`-backed tool, not a reason for an interpreter. |
| **Skills/plugins authored into the scratchpad** | Procedures need to be inspectable, testable, diffable — the inverse of a memory blob, in a codebase that version-pins and digest-guards model-facing prose. The one real tension: `Corrections` *is* procedural content. Under §2 it is owner-written, which bounds it; the plan should still say what stops it becoming a skills layer by accretion. |
| **Per-persona sidecar variants** (e.g. hiding `deep_produce`'s EMR params from jerv) | The security half is right and verified: `deep_research.py:1685-1689` fails closed unless health-scoped **owner**, and jerv's scopes are empty. But the first draft's disposal — "trim it in W3 instead" — **is not possible**: curator holds `deep_produce` via `extra_tools` and needs those params, and the sidecar is shared, so removing them breaks curator. Hiding them from jerv only *is* a per-persona variant. Corrected position: the ~384 tokens of always-refusing prose are an **accepted cost** until the catalog work makes per-persona description assembly cheap. Noted as an inconsistency with §1's own discipline, and accepted knowingly rather than waved away. |
| **`anyOf` schema for "question required unless preset"** | Right answer, wrong evidence. The first draft claimed `TOOL_CATALOG_PLAN` "records that gpt-oss-120b fills flat fields reliably and little beyond." It records the opposite-leaning *concern* (a discriminated param is "harder for a weak local model to fill", filed under risks) and its only measurement reports W1's umbrellas shipping with "no measurable tool-selection regression." Shipped sidecars already carry three-level nesting and enums successfully. The real reason is narrow and specific: the `analyze_stream` GBNF segfault class (§3.2), and `deep_research` is a six-property, five-optional object — near-identical shape. **Not "closed"** — reopen if the grammar-builder crash is ever fixed upstream. |
| **`TOOL_CATALOG_PLAN` W2/W3** (always-on menu, hot core, `tool_guide`-before-call) | That plan gates the catalog machinery behind a selection-accuracy eval and an unresolved §7 contradiction, correctly. Note honestly that W3 here is *adjacent* to that machinery — dynamic description assembly — and is admitted only because it is a bounded, config-derived list with a named crash constraint, not a general disclosure mechanism. |

## 6. Why the probe is trusted here and not there

The probe is a good detector of tools it **cannot successfully call** — it observes its own
refusals, which is ground truth. It is a poor judge of what is **missing**, because it cannot
distinguish "not built" from "not granted to me." This plan acts on the first class and treats
the second as a prompt to go read the registry.

The worked example is instructive in both directions: the model was wrong that the chart tools
were unbuilt, *and* right that it could not draw a trend — and the first draft then overcorrected
into denying any gap existed (§1). A capability claim from the probe is a lead to verify against
the registry, and a *dismissal* of one is equally a lead.

## 7. Open decisions

1. **Digest governance for injected prose** (§3) — owed by `TOOL_CATALOG_PLAN` §5. W3 blocks on it.
2. **Cap value** — 3 KB rendered is asserted, not measured. Note that review judged this *not* a
   security question (a 200-byte payload is enough to matter), so it should not consume review
   time on those grounds.
3. **PWA surface shape** — Settings pane vs a chat-adjacent card. Under §2 this is the sanction
   mechanism, so the choice is about how visible the owner's write action is, not cosmetics.
4. **Scope of `TOOL_CATALOG_PLAN` W0b** — **sixteen** tools now exceed 2.5k, not the three the
   first draft named, and the fattest five have changed: `canvas` (6,723) landed straight into
   second place, displacing `analyze_stream`. Two of the unnamed (`render_bars`,
   `render_chart`) were rewritten this week, so re-trimming them is churn to price. Decided in
   that document.
5. **What stops `Corrections` becoming a skills layer by accretion** (§5 row 2).

## 8. Review record

Four independent reviewers (security red-team, prior-art/architecture, contrarian, fact-check)
read the first draft against the code.

**Changed in response:** the §2 data-framing/`Preferences` contradiction (replaced with the
sanction split); prose framing replaced by the `briefs.py` sentinel + `neutralize_boundary`;
full-document rewrite replaced by ACE delta ops per the binding rule; the cloned table
generalized to `app.agent_scratchpad` with the archivist migrated in; the real
`agent_memory` disqualifier (domain-scoped RLS vs. a scope-less session) stated; W1's
non-existent `prompt-eval.sh` gate replaced with a harness scenario; the trim rehomed to
`TOOL_CATALOG_PLAN` W0b with a realistic 15–20% target and its "the gate" claim withdrawn
(`/api/debug/tool-probe` has no baseline, threshold, or case set, and never runs handlers);
W3's seam, `enum` hazard, and helper reuse corrected; rejection rows 1, 3, and 4 re-evidenced;
the token figure corrected upward to 26.5k with the config-gated floor noted; the
"unreadable without a terminal" and "same paragraph" claims corrected.

**Inherited, not fixed — filed as separate work:**

- **`_plan_blocks` laundering** (§2). `write_plan(body=…)` with `status=None` rewrites an
  approved plan without demoting, and `write_plan_result` notes render inside the sanctioned
  envelope — so agent-written, web-derived text reaches instruction framing. Suggested fix:
  sentinel-wrap `plan.body` and `format_plan_results` inside the block, and demote to
  `not_approved` on any body rewrite. Belongs to the planning tool, not here.
- **Unguarded href branch** in `frontend/src/agent/markdown.tsx:609-620` — the fullwidth-bracket
  branch pushes `href={url}` with no `SAFE_URL` check. Pre-existing; W2 adds a surface that
  renders agent-written text, so it should be fixed before that surface ships.

**Judged too cautious by review, and left alone deliberately:** the PWA edit surface is the
plan's best security property, not an attack surface; cap size is not a security question;
cross-persona leakage into a health-scoped curator turn is not live today and needs a test, not
machinery; the `0094` DDL shape is correct as copied.
