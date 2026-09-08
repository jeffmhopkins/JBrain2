# B2 — The conversation data model for note-rooted agent ingest

> **Status:** Research · **Last verified:** 2026-09-08

Persistence design for the proposed replacement of the deterministic
extract → Integrator → arbiter → apply pipeline
(`backend/src/jbrain/analysis/integrate.py:1-12`,
`backend/src/jbrain/analysis/pipeline.py:426-445`) with a **note-rooted, resumable agent
conversation** that writes the entity/predicate graph through tools and asks the owner
where it is unsure.

Binding owner decisions this design is built against: auto first pass on capture;
commit-high-confidence / ask-low-confidence; no deterministic arbiter; disposable dev DB
(destructive reset fine); scope = owner notes + their attachments/OCR/media; questions land
in a **silent** queue (no push); inference is **local-only** (`gpt-oss-120b` text,
`qwen3.8-27b-abliterated` vision, one large model resident at a time on a 128 GB box); the
owner operates the box from a phone with no terminal (CLAUDE.md #10).

**Verified vs assumed.** Everything with a `path:line` citation was read in this repo at
head `4ad3026` (migration head `0186`). Every token budget, threshold, retention window,
and sweep cadence proposed in §6 is **assumed** — a starting point to be measured on the
box, not a measured figure. Anything else marked *(assumed)* inline.

---

## 1. Survey — what already exists

### 1.1 The agent's session/turn/run substrate

| Table | Defined | RLS | Shape |
|---|---|---|---|
| `app.agent_sessions` | `models/agent.py:26-66`, mig `0015_agent_sessions.py:60-83` | `is_owner()` only (`0015:76-82`) | The capability record: principal, persona (`agent`, `0070`), `domain_scopes`/`subject_ids` read scope, status (`active`/`ended`/`archived`, `0025`), sub-agent lineage `parent_session_id`/`depth`/`no_memory` (`0105`), and a `context_tokens`/`context_window` meter. |
| `app.agent_turns` | `models/agent.py:160-182`, mig `0020_agent_turns.py:26-39` | **`is_owner()` only** (`0020:44-50`) | `seq` (identity), `session_id` CASCADE, `run_id` SET NULL, `role IN ('user','assistant')`, `content`, `tools jsonb` (a *display* blob for the "Worked" strip), `reasoning` (`0071`). |
| `app.runs` / `app.run_steps` | `models/agent.py:69-127` / `:130-157`, mig `0016`, unified `0037` | `is_owner()` (`0016:63-68`) | One run per turn-loop; `kind='agent'` CHECK requires `session_id` + `prompt_version` (`models/agent.py:83-86`); `call_stamp jsonb` (`0166`) records model/provider/effort/window/tools — but is documented as *display-only, never queried by field* (`models/agent.py:120-125`). Steps carry `kind`, `name`, `tool_version`, `ok`, `cost_tokens`, `detail` (a structlog array). |
| `app.turn_attachments` | `models/agent.py:185-216`, mig `0074`/`0085` | domain (`domain_code`) | Chat files uploaded to a session, bound to a turn afterwards. Explicitly a **new** table because `app.attachments.note_id` is NOT NULL. |
| `app.turn_tool_artifacts` | `models/agent.py:219-250`, mig `0151:73-74` | `has_domain_scope(domain_code)` | The cross-turn tool-result cache: heavy text content-addressed in the blob store, the row holds metadata + a paging cursor. |
| `app.agent_session_plans` | `models/plan.py`, mig `0155:28-53` | `is_owner()` | 1:1 with a session; the per-conversation plan + `awaiting_owner` + auto-continuation bookkeeping. **The structural precedent for a per-conversation side table.** |
| `app.agent_memory` | `models/agent.py:253-279`, mig `0017:54-85` | `is_owner() AND has_domain_scope(domain_code)` | Working/behavioral memory, owner-confirmed-write only. |
| `app.agent_episodes` / `_refs` | `models/agent.py:282-329`, mig `0017:89-145` | `is_owner() AND has_all_domain_scopes(domain_scopes)` / via-parent EXISTS | **The multi-domain fail-closed precedent.** An episode is stamped with the *set* of domains its turn touched and is visible only to a session holding **all** of them (`0017:36-46`). Refs are pointers (note/fact/entity id), never copies, and cascade with the episode. |
| `app.proposals` / `_nodes` | `models/proposals.py:16-64`, mig `0018:34-96` | `is_owner() AND has_domain_scope(domain_code)` / via-parent EXISTS | The staged-work tree; `provenance jsonb` already names "the conversation/notes/attachments that prompted this". `0130:26` added `decision_note` — the owner's free-text reason on a decline, folded into the outcome the agent sees. |
| `app.events` / `triggers` / `pipelines` / `schedules` | `models/workflow.py:30-120`, mig `0036` | `events` domain-firewalled | Phase-5 engine. `note.created` / `note.ingested` are the emitted events (`workflow/events.py:47-52`); `integrate_note` is the registered action (`workflow/registry.py:181-193`). |
| `app.review_items` | `models/analysis.py:208-223` | `has_domain_scope(domain_code)` | The existing **silent** inbox: `kind` + `payload jsonb` + `status` + `resolution jsonb`. Purged on note delete (`analysis/purge.py:94`). |
| `app.resolution_pin` | `models/workflow.py:123-163` | domain, cascades with note | The Integrator's committed identity/predicate-key decisions, keyed `(note_id, chunk_id, occurrence_index, decision_kind)`. Dies with the pipeline unless deliberately kept. |

### 1.2 The RLS machinery the design must reuse

- `SessionContext` sets six GUCs per transaction (`db/session.py:19-45`), applied by
  `scoped_session` with `set_config(..., is_local := true)` (`db/session.py:159-171`).
- `app.has_domain_scope(code)` — owner short-circuits **unless** `app.owner_scoped='true'`
  (mig `0015:31-43`). This is what makes a narrowed agent session a real firewall.
- `app.has_all_domain_scopes(codes text[])` — `codes <@ session scopes` (mig `0017:36-46`).
  Note the fail-open corner: **an empty array is a subset of everything**, so `{}` is
  visible to any owner session.
- `narrowed_context(principal_id, domain_code)` is **fail-closed on a partial stamp** —
  a half-stamp raises `ScopeStampError` rather than widening to system
  (`db/session.py:56-88`). Jobs are stamped `(principal_id, domain_code)`; unstamped jobs
  run as the all-domains `SYSTEM_CTX` (`queue.py:24`, `worker.py:116-122`).
- Two pure domain policies already exist and are the right ones to reuse verbatim:
  `agent/classifier.py:19-41` (`_SENSITIVITY` ranking + `episodic_scopes`: touched ∩
  session scopes, falling back to the full session scope set rather than minting a bare
  `general` row) and `analysis/extraction.py:158-207` (`_DOMAIN_BY_PREDICATE` /
  `domain_floor` — a deterministic per-predicate floor; `ratchet_domain` — up is free, down
  needs review; `RESTRICTED_DOMAINS` at `:28`).

### 1.3 Verdict: reuse vs. new

**Reuse as-is.** `agent_sessions` (the conversation *is* a session — persona, principal,
read scope, archive, cascade, and everything already keyed off `session_id`: runs,
proposals, attachments, tool artifacts, plans, the Chats surface, live-run reattach).
`runs`/`run_steps` (cost + audit). `turn_attachments` and `turn_tool_artifacts`.
`proposals` (for the writes that stay owner-approved). `has_domain_scope` /
`has_all_domain_scopes` / `narrowed_context`. `agent/classifier.py` and
`analysis/extraction.py`'s domain floor + ratchet. `events`/`triggers` to fire the first
pass. `weight.py`'s ceilings (`:28-46`) as the **deterministic** half of
commit-vs-ask — the model's self-confidence may only ever *lower* a ceiling
(`analysis/weight.py:1-18`), and that rule must survive the arbiter's removal.

**Extend.** `agent_turns` — it is the right table but is missing a domain column, a model /
prompt stamp, a note root, and a role vocabulary wider than `user|assistant`.

**New.** A conversation root table; a typed tool-call table; a pending-question table; a
graph-write provenance table. §2 argues why each is new rather than a column somewhere.

---

## 2. Where the existing chat model is the wrong shape — honestly

`agent_sessions` is **right** and should be reused unchanged. `agent_turns` is *nearly*
right and should be extended. But five properties of today's chat transcript are wrong for
a long-lived, note-rooted, resumable conversation, and inheriting them silently would be
the design's biggest mistake:

1. **The transcript is a replay artifact, not the context.** The context a turn actually
   runs on is **client-supplied**: `ChatRequest.history: list[ChatMessageIn]`
   (`api/agent.py:126-137`), and `ChatMessageIn` is documented as carrying "only the text …
   tool calls live inside a single turn-loop, not across them". `AgentTranscript.load`
   (`agent/transcript_store.py:180-208`) exists to seed *the PWA surface*. A note-rooted
   conversation's first pass has **no client** — it is a background job on capture — so the
   server must build its own replay from the DB. That is net-new code, and it is the
   single most important consequence of this whole redesign.
2. **`agent_turns` has no domain column at all.** Its policy is bare `is_owner()`
   (`0020:44-50`), justified in the migration's own docstring as "owner-only metadata …
   never in-scope note content". That justification **dies** the moment a turn's content is
   a health note's body and the owner's answer about a medication. Every other
   content-bearing agent table carries a firewall column (`agent_memory`, `agent_episodes`,
   `turn_attachments`, `turn_tool_artifacts`). A note-rooted turn must too.
3. **Tool calls are stored as a display blob with no argument fidelity.**
   `agent_turns.tools jsonb` is the "Worked" strip; the wire shape it is built from
   (`ToolCallEvent` / `ToolResultEvent`, `agent/contracts.py:235-258`) carries arguments,
   but `run_steps` — the *audit* record — persists only `kind/name/tool_version/ok/
   cost_tokens` (`agent/runlog.py:98-121`). **Nowhere in the DB today can you answer "which
   tool call, with which arguments, wrote this fact".** That is exactly the provenance the
   new pipeline needs, since `facts.extractor`/`prompt_version` stop being sufficient.
4. **No model/prompt stamp survives on the turn.** It is on the run
   (`runs.prompt_version`, and `call_stamp` which the model comment explicitly says is
   never queried by field — `models/agent.py:120-125`), and `agent_turns.run_id` is
   `ON DELETE SET NULL` (`0020:30`) — the schema already assumes a run can vanish while its
   turn persists. `workflow/runlog.py:153-182` really does `DELETE FROM app.runs` for idle
   runs. A conversation resumed in six months on a different model needs the stamp on the
   turn.
5. **No question state, no note root, no lifecycle.** There is no way to ask "which notes
   have unanswered questions", to link an owner answer back to the question it settles, or
   to detect that the note a conversation read has since been edited.

**Recommendation (R1):** reuse `agent_sessions` + `agent_turns`; fix (2), (4), (5-partial)
by extending `agent_turns`; add four new tables for (3) and (5).

**Rejected alternative:** a parallel `note_conversations` + `conversation_turns` store
disjoint from the chat tables. It is cleaner on paper and it is the wrong call here: the
owner answers these questions **in the chat UI on their phone**, so a fork means the Chats
list, the omnibox, attachments, tool artifacts, proposals, live-run reattach, the context
meter and the transcript loader all have to union two stores. The cost of extending one
table is four columns and one policy swap.

---

## 3. Proposed schema

### 3.1 `app.note_conversations` — the root (new, 1:1 with a session)

Modeled on `agent_session_plans` (mig `0155:28-53`): a session-keyed side table, so the
generic `agent_sessions` row stays generic and every chat is not carrying five NULL
ingest columns.

```
session_id        uuid PRIMARY KEY REFERENCES app.agent_sessions(id) ON DELETE CASCADE
note_id           uuid NOT NULL     REFERENCES app.notes(id)          ON DELETE CASCADE
domain_floor      text NOT NULL     REFERENCES app.domains(code)   -- the note's domain at open
touched_domains   text[] NOT NULL DEFAULT '{}'                     -- the monotone ratchet (§4)
state             text NOT NULL DEFAULT 'open'
                  CHECK (state IN ('open','awaiting_owner','settled','stale','abandoned','superseded'))
note_body_sha     text NOT NULL      -- the body the last pass actually read
pass_count        int  NOT NULL DEFAULT 0
last_model        text NOT NULL DEFAULT ''
last_prompt_version text NOT NULL DEFAULT ''
supersedes_session_id uuid REFERENCES app.agent_sessions(id) ON DELETE SET NULL  -- continuation chain (§6.4)
opened_at, last_pass_at, settled_at timestamptz
```

- `UNIQUE (note_id) WHERE state <> 'superseded'` — one live conversation per note; a
  continuation supersedes rather than competes.
- `touched_domains` is the conversation-level high-water mark, **not** a single code: a
  conversation can legitimately reach both `health` and `finance`, which one column cannot
  express. This is the `agent_episodes.domain_scopes` shape (`0017:93`) and is gated with
  the same `has_all_domain_scopes`.
- `note_body_sha` is what makes "the note was edited under me" detectable without diffing
  prose; it mirrors the `integrated → stale` flip ingest already does
  (`ingest/pipeline.py:118-131`).

### 3.2 `app.agent_turns` — extended (four columns + a policy swap)

```
ALTER TABLE app.agent_turns
  ADD COLUMN kind text NOT NULL DEFAULT 'message'
      CHECK (kind IN ('message','note','question','answer','notice','ledger')),
  ADD COLUMN domain_scopes text[] NOT NULL DEFAULT '{}',
  ADD COLUMN model text NOT NULL DEFAULT '',
  ADD COLUMN prompt_version text NOT NULL DEFAULT '',
  ADD COLUMN source_note_id uuid REFERENCES app.notes(id) ON DELETE CASCADE;
-- widen the role vocabulary for the server-authored channel
ALTER TABLE app.agent_turns DROP CONSTRAINT agent_turns_role_check;
ALTER TABLE app.agent_turns ADD CONSTRAINT agent_turns_role_check
  CHECK (role IN ('user','assistant','system'));
```

- **Turn 0 is a reference, not a copy.** `kind='note'`, `role='system'`, `content=''`,
  `source_note_id` set. The note body is rendered into the prompt *at replay time* from the
  live `app.notes` row. This is the "pointers, not copies" rule that
  `agent_episode_refs` already encodes (`0017:16-19`, ASSISTANT.md #2), and it is what makes
  note-edit and note-delete tractable (§5). Copying the body into turn 0 would create a
  second source of truth that drifts and that purge has to hunt.
- `kind` is the *semantic*; `role` stays the *LLM channel*. A question the agent asks is
  `role='assistant', kind='question'`; the owner's reply is `role='user', kind='answer'`;
  a server-authored "this conversation resumes on a different model" line is
  `role='system', kind='notice'` — the same DATA-framed, answer-only shape
  `record_answer` already ships for the deferred-analysis resume
  (`agent/transcript_store.py:115-138`).
- The policy swap (§4.3) is the load-bearing half. `DEFAULT '{}'` keeps every legacy chat
  turn visible exactly as today (`{} <@ anything` is true), so the change only ever
  *tightens*, never hides existing rows.

### 3.3 `app.turn_tool_calls` — the typed call + result (new)

```
id            uuid PRIMARY KEY DEFAULT gen_random_uuid()
turn_id       uuid NOT NULL REFERENCES app.agent_turns(id) ON DELETE CASCADE
run_step_id   uuid          REFERENCES app.run_steps(id)   ON DELETE SET NULL
idx           int  NOT NULL
tool_name     text NOT NULL
tool_version  int  NOT NULL
arguments     jsonb NOT NULL DEFAULT '{}'
result        jsonb                              -- structural result; heavy text → turn_tool_artifacts
ok            boolean NOT NULL
error         text
domain_code   text NOT NULL REFERENCES app.domains(code)   -- most-restrictive domain this call touched
created_at    timestamptz NOT NULL DEFAULT now()
UNIQUE (turn_id, idx)
```

**Why not just add `arguments`/`result` to `run_steps`?** Considered and rejected:
`run_steps` is written by six-plus drivers and is a hot audit table; two heavy JSONB columns
tax all of them. More decisively, **run rows are deletable while turns persist** —
`agent_turns.run_id` is `ON DELETE SET NULL` (`0020:30`) and `reap_idle_run` really deletes
runs (`workflow/runlog.py:153-182`), with a stranded-run sweep at
`agent/runlog.py:38`. Provenance must outlive the audit row, so it hangs off the durable
turn and keeps `run_step_id` as a nullable join for cost.

**Result bodies.** `result` holds the *structural* result the graph write needs (ids
written, entity candidates). Anything large reuses the existing by-reference store
(`turn_tool_artifacts`, `models/agent.py:219-250`) rather than growing this row.

### 3.4 `app.conversation_questions` — the silent queue (new)

```
id              uuid PRIMARY KEY DEFAULT gen_random_uuid()
session_id      uuid NOT NULL REFERENCES app.agent_sessions(id) ON DELETE CASCADE
note_id         uuid NOT NULL REFERENCES app.notes(id)          ON DELETE CASCADE
asked_turn_id   uuid NOT NULL REFERENCES app.agent_turns(id)    ON DELETE CASCADE
answer_turn_id  uuid          REFERENCES app.agent_turns(id)    ON DELETE SET NULL
question        text NOT NULL
kind            text NOT NULL CHECK (kind IN
                  ('entity_identity','predicate_value','domain','temporal','merge','other'))
proposed        jsonb NOT NULL DEFAULT '{}'   -- the structural candidate answer(s)
status          text NOT NULL DEFAULT 'open'
                CHECK (status IN ('open','answered','withdrawn','expired'))
domain_code     text NOT NULL REFERENCES app.domains(code)
asked_at, answered_at timestamptz
```

- **`question → answer` linkage is a turn FK, not a payload field.** That is the whole
  argument for a new table over `review_items` (`models/analysis.py:208-223`):
  `review_items.resolution jsonb` feeds a closed registry of resolution handlers with
  deterministic DB effects, whereas answering an agent question has *no* deterministic
  effect — it re-runs the agent, whose next turn is the resolution. Modeling that as a
  `review_items` payload would put a foreign lifecycle inside a table whose contract is
  "resolve → structural effect".
- **`proposed` carries the candidate structurally, never in prose** — the `propose_merge`
  discipline (ASSISTANT.md: "the leaf carries them structurally in its preview, never in
  prose"), so a one-tap "yes" is a structural commit and not a re-parse of the question text.
- **Silent by construction:** nothing here calls `notify_owner` (`notify/bus.py:58`). The
  queue is a pull surface (a badge on the note, a list view), exactly like `review_items`.
  *Assumed:* the PWA inbox unions `review_items` and `conversation_questions` into one list —
  two tables, one surface.
- Partial index `(domain_code, asked_at DESC) WHERE status='open'` — the queue view.

### 3.5 `app.graph_write_provenance` — fact/edge → the turn that wrote it (new)

```
id             uuid PRIMARY KEY DEFAULT gen_random_uuid()
fact_id        uuid REFERENCES app.facts(id)            ON DELETE CASCADE
entity_id      uuid REFERENCES app.entities(id)         ON DELETE CASCADE
mention_id     uuid REFERENCES app.entity_mentions(id)  ON DELETE CASCADE
alias_id       uuid REFERENCES app.entity_aliases(id)   ON DELETE CASCADE
CHECK (num_nonnulls(fact_id, entity_id, mention_id, alias_id) = 1)
note_id        uuid NOT NULL REFERENCES app.notes(id)          ON DELETE CASCADE
session_id     uuid NOT NULL REFERENCES app.agent_sessions(id) ON DELETE CASCADE
turn_id        uuid          REFERENCES app.agent_turns(id)    ON DELETE SET NULL
tool_call_id   uuid          REFERENCES app.turn_tool_calls(id) ON DELETE SET NULL
question_id    uuid          REFERENCES app.conversation_questions(id) ON DELETE SET NULL
op             text NOT NULL CHECK (op IN
                 ('create_entity','write_fact','supersede_fact','retract_fact',
                  'merge_entity','add_alias','link_mention'))
committed_by   text NOT NULL CHECK (committed_by IN ('agent_auto','owner_answer','owner_direct'))
model          text NOT NULL DEFAULT ''
prompt_version text NOT NULL DEFAULT ''
tool_version   int
confidence     real
domain_code    text NOT NULL REFERENCES app.domains(code)
committed_at   timestamptz NOT NULL DEFAULT now()
```

- The exactly-one-target `num_nonnulls` CHECK is lifted verbatim from
  `agent_episode_refs` (`0017:128`). Every target FK is `ON DELETE CASCADE`, so a purge or a
  merge that removes a fact takes its provenance with it and no orphan survives.
- `question_id` closes the loop the owner cares about: *"this value is here because you told
  me so on 14 June"*.
- **`facts.extractor` / `facts.prompt_version` stay NOT NULL and stay meaningful**
  (`models/analysis.py:183-184`, mig `0006:188-189`). `extractor` already means "which model
  produced this" and a conversation-written fact still has one — stamp
  `f"{provider}:{model}"` of the writing turn and `prompt_version` of that turn's prompt.
  Two consequences worth stating: (a) **no migration is needed** to relax those columns, and
  (b) every existing query, eval, and re-extraction path that keys on `prompt_version`
  (ANALYSIS.md "Reprocessing": *"`prompt_version` makes corpus re-runs a planned, budgeted
  migration"*) keeps working. The provenance table is the *precise* pointer layered on top,
  not a replacement.
- **`facts.note_id` is NOT NULL** (`models/analysis.py:179`) and stays so: a
  conversation-written fact stamps the root note. Good — it means the existing purge already
  finds it.

### 3.6 Known blocker: `entity_mentions` cannot represent an agent-created entity

`entity_mentions.chunk_id` and `.note_id` are both NOT NULL (mig `0006:89-104`,
`models/analysis.py:72-88`), and span anchoring is what makes merges reversible
(`0006:94-96`). An agent that creates an entity from an owner's *answer* — "no, that's my
cousin Dana, not Dana Cruz from work" — has no chunk to anchor to: the answer lives in a
turn, not in a note's chunk stream. Options:

- **(a)** make `chunk_id` nullable and add `answer_turn_id uuid REFERENCES app.agent_turns(id)`
  as the alternative anchor, with a CHECK requiring one of the two. Merges stay reversible
  because the turn is as durable as the chunk. **Recommended.**
- **(b)** synthesize a chunk from the answer text. Rejected: it would put conversation text
  into the searchable corpus, which is exactly what "notes are the sole sources of truth"
  forbids.
- **(c)** require every agent-created entity to be anchored in the note. Rejected: it makes
  the owner's correction unrepresentable, which is the whole point of the redesign.

---

## 4. RLS and domain scoping

### 4.1 The problem in one sentence

A conversation opens on a `general` note; at turn 5 the owner's answer, or a tool read,
pulls in `health`. Turns 0–4 are already stamped `{general}` and are readable by a
`general`-only session. What happens?

### 4.2 The rules

1. **The session's read scope is the ceiling, and only the owner widens it.** A note
   conversation opens with `agent_sessions.domain_scopes = [note.domain_code]` and
   `owner_scoped=true` — a real firewall (`0015:31-43`). Widening is the existing
   owner-only scope-adjust endpoint (ASSISTANT.md "Chat lifecycle": read scope is
   adjustable after start, owner-only). **The agent never widens its own scope** (#8, and
   `narrowed_context`'s fail-closed partial-stamp rule, `db/session.py:56-88`).
2. **Per-turn stamp = most-restrictive-set-touched, bounded by the session scope.** Reuse
   `agent/classifier.py:27-41` (`episodic_scopes`) unchanged: `touched ∩ session_scopes`,
   falling back to the *full* session scope set when nothing domain-specific was observed,
   because #4 forbids decomposing a multi-scope turn into a bare `general` row.
3. **The conversation-level ratchet is monotone and enforced in Postgres.**
   `note_conversations.touched_domains` only ever grows:
   `touched_domains := touched_domains ∪ turn.domain_scopes`. CLAUDE.md #3 says firewalls
   are enforced in Postgres, so this is a **BEFORE UPDATE trigger** that raises on any
   update where `NEW.touched_domains` is not a superset of `OLD.touched_domains`, not an
   application convention.
4. **Earlier turns are NOT retro-stamped.** Two options were weighed:
   - *(i) retro-ratchet* — rewrite every earlier turn's `domain_scopes` to the high-water
     mark. Fail-closed, but it rewrites history (a turn's stamp stops recording what that
     turn actually touched) and destroys the audit.
   - *(ii) conversation-level gate* — leave per-turn stamps truthful and gate the
     conversation as a whole on `touched_domains`. **Recommended.**
   The leak (ii) must close is a direct query on `agent_turns` that bypasses the
   conversation "door". So the door is put **in the turn policy itself**, using the
   via-parent EXISTS subquery pattern `proposal_nodes` (`0018:89-95`) and
   `agent_episode_refs` (`0017:138-144`) already use.
5. **Deterministic floors decide domain, not the model.** Every predicate the agent writes
   goes through `analysis/extraction.py:189-192` (`domain_floor`) first: a `bloodpressure`
   write is `health` whatever the model claims. `ratchet_domain` (`:195-207`) then applies —
   up is free, down (or across restricted domains) is **never** a silent commit; it becomes
   a question in the queue (`kind='domain'`). This matters more without an arbiter: the
   conversation is untrusted-content-driven, so the *decision* to leave the restricted set
   must never be the model's.
6. **A write may target only an in-scope domain at or above the note's floor.** The
   existing rule ("you cannot stage a write to a domain the session cannot read",
   `0018:56-57`) applies unchanged; the new half is the floor.
7. **The first-pass job's scope stamp is the note's floor.** The capture-triggered pass is
   a stamped job → `narrowed_context(principal, note.domain_code)` (`worker.py:116-122`).
   It cannot self-widen. A pass that *needs* a wider scope files a question and stops; the
   owner widening the conversation's scope is what unblocks it. This is the honest answer
   to "what if the agent needs to look at the health graph to place a general note" — it
   asks.

### 4.3 The policies

```sql
-- note_conversations: gated on the ratchet, owner-only
CREATE POLICY note_conversations_scopes ON app.note_conversations
  USING      (app.is_owner() AND app.has_all_domain_scopes(touched_domains))
  WITH CHECK (app.is_owner() AND app.has_all_domain_scopes(touched_domains));

-- agent_turns: own stamp AND (if it belongs to a note conversation) its ratchet
DROP POLICY agent_turns_owner ON app.agent_turns;
CREATE POLICY agent_turns_scopes ON app.agent_turns
  USING (
    app.is_owner()
    AND app.has_all_domain_scopes(domain_scopes)
    AND NOT EXISTS (                              -- the door, enforced per row
      SELECT 1 FROM app.note_conversations c
      WHERE c.session_id = agent_turns.session_id
        AND NOT app.has_all_domain_scopes(c.touched_domains)
    )
  )
  WITH CHECK (same);
```

The `NOT EXISTS` is written so an *ordinary chat* turn (no `note_conversations` row) is
unaffected and keeps today's behaviour — the only change for legacy chats is the
`domain_scopes '{}'` stamp, which is a subset of everything.

`turn_tool_calls` and `graph_write_provenance` carry `domain_code` and take the standard
`is_owner() AND has_domain_scope(domain_code)` policy (the `agent_memory` shape,
`0017:78-84`). `conversation_questions` likewise. **Do not** give the tool-call table a
via-parent-only policy: a call's touched domain can be *more* restrictive than its turn's
overall stamp only in the retro-ratchet design; in the recommended design they agree, but
carrying the column keeps the queue filterable without a join and is the
`turn_tool_artifacts` precedent (`0151:73-74`).

### 4.4 What actually happens on a ratchet — worked

Turn 5 touches `health` in a conversation whose session scope is `[general]`:

1. `domain_floor('bloodpressure') = 'health'`, which is **not** in the session's scopes, so
   the write is refused by RLS's `WITH CHECK` before any policy in application code runs.
   The tool returns a structured error (the "tool errors are structured observations"
   contract, ASSISTANT.md "Agent runtime").
2. The agent files a `kind='domain'` question: *"this note mentions a blood-pressure
   reading; this conversation is scoped to general. Widen it to health?"* — stamped
   `domain_code='general'` (it contains no health *content*, only the fact that some
   exists). **This is the leak boundary and the sharpest open question** (§8, Q2): a
   badly-worded question could itself carry the health detail into a general-scoped row.
   Mitigation: question text for a `domain` question is **server-templated**, not
   model-authored.
3. The owner widens the scope (owner-only endpoint). The next pass runs with
   `domain_scopes=[general,health]`, its turn stamps `{general,health}`,
   `touched_domains` ratchets, and from that moment a `general`-only session can no longer
   open the conversation at all — while turns 0–4 keep their truthful `{general}` stamp for
   the audit.
4. **Nothing is retro-hidden and nothing is retro-rewritten.** A narrower session simply
   loses the door.

---

## 5. Lifecycle

| Event | Today's mechanism | Proposed conversation behaviour |
|---|---|---|
| **Note edited** | `notes/repo.py:136-169` sets `ingest_state='pending'`; re-ingest flips `integrated → stale` (`ingest/pipeline.py:118-131`) | `note_body_sha` mismatch → `state='stale'`. On resume, turn 0 **re-renders from the live note** (it is a reference, §3.2) and a `kind='notice'` turn states that the body changed. Committed facts are not torn down — the existing incremental re-run semantics apply (ANALYSIS.md "Reprocessing": full unwind-on-re-run was **rejected**). Open questions whose premise the edit removed are `withdrawn`. |
| **Note deleted** | **Soft delete** — `notes/repo.py:174-194` sets `deleted_at` and calls `purge_note_artifacts` (`analysis/purge.py:65`) explicitly, then hard-deletes chunks | ⚠️ **The FK cascades in §3 will never fire**, because the note row survives. Every conversation artifact must be purged **explicitly** inside `purge_note_artifacts`, in the same transaction: `graph_write_provenance` → `conversation_questions` → `turn_tool_calls` → `agent_turns` (where `session_id` ∈ the note's conversations, plus any turn with `source_note_id = note`) → `note_conversations` → the `agent_sessions` row itself. `purge.py` already imports `AgentEpisode`/`AgentEpisodeRef` for exactly this reason (`purge.py:32`). ASSISTANT.md #11 ("purge is total") makes this non-negotiable, with the test it names. |
| **Note re-analyzed** (`POST /api/notes/{id}/analyze`) | a plain `integrate_note` job (ANALYSIS.md "Reprocessing") | A **new pass in the same conversation** — new run, new turns, `pass_count += 1` — never a new conversation. Rationale: the Q&A history *is* the accumulated correction record; discarding it re-asks questions the owner already answered. Prior answers replay as DATA (§6.2). |
| **Conversation abandoned** | — | No explicit action. A nightly sweep (the `0038_seed_nightly_sweeps` pattern) sets `state='abandoned'` after *N* days with `open` questions and no owner reply (*assumed:* N = 30). Abandoning **never retracts committed facts** — they came from the note, which still exists. Open questions go `expired`. |
| **Conversation resumed months later** | — | `state` back to `open`; a `kind='notice'` turn records model/prompt discontinuity; per-turn `model`/`prompt_version` make it auditable. **No auto re-litigation of already-committed facts** — that would churn the graph on every model upgrade, which is precisely the "no corpus re-run" rule (`ENTITY_GRAPH_REFOCUS_PLAN.md` §0). A deliberate re-pass is the owner's explicit re-analyze. |
| **Session/conversation deleted by the owner** | `0021` made `runs.session_id` CASCADE and granted DELETE on `agent_sessions` | Deleting the conversation cascades turns, tool calls, questions, and provenance rows — but **not** the facts, which belong to the note. Deliberate asymmetry: it loses the *explanation*, not the *truth*. Worth a confirm dialog. |

---

## 6. Retention and context

### 6.1 The constraints, verified

- `gpt-oss-120b`: `context_window=131072`, MXFP4, ~59 GB weights, ~31 t/s
  (`llm/local_catalog.py:569-590`). One large model resident at a time.
- **There is no compaction, summarization, or history truncation anywhere in the backend**
  (`docs/proposed/CONTEXT_COMPACTION_PLAN.md` §1). `context_tokens`/`context_window` on
  `agent_sessions` drive only the PWA meter.
- The cold review of that plan (§0, 2026-08-25) **reversed** the summarize-in-place design:
  a fabrication-prone local 120B in every turn's hot path re-imports the failure this repo
  engineers around; the verdict is **Tier 0 verbatim offload + Tier 1 deterministic,
  LLM-free structured eviction**, with the summarizer demoted to a gated, off-by-default
  emergency backstop. **This design must obey that verdict** — it is the same box, the same
  model, and a *worse* threat position (the content is a raw note).
- jerv's primed prefix is ~32k tokens (ASSISTANT.md "Chat lifecycle") — the persona +
  tool-schema floor is not small.

### 6.2 The replay budget (all figures *assumed*, to be measured)

Against 131,072:

| Block | Budget | Source |
|---|---|---|
| system prompt + tool schemas | ~15k | the ingest persona's allowlist |
| turn 0 — live note body + attachment OCR/caption text | ≤ 10k, truncated with a marker | re-rendered from `app.notes` + `attachment_extracts` |
| graph context (entities/facts the note touches) | ≤ 8k | reuse `analysis/graph_context.py` |
| **the ledger** (§6.3) | ≤ 6k | generated from `graph_write_provenance` |
| Q&A pairs — **always verbatim, never evicted** | ≤ 25k | `conversation_questions` + their two turns |
| last K message turns verbatim (*assumed* K = 6) | ≤ 30k | `agent_turns` |
| index lines for evicted middle turns | ≤ 2k | one line per turn |
| headroom for the turn's own tool results | the remainder (~35k) | |

### 6.3 The compaction strategy — deterministic, no summarizer

In priority order:

1. **Never evict**: turn 0, every `question`/`answer` pair, and the last K message turns.
   An owner answer is the single most expensive token in the system to lose; it cost a human
   interaction.
2. **Tool calls replay as one typed line each** — `tool · target · ok` from
   `turn_tool_calls` — never a result body. This is exactly the "Worked"-strip
   `action · target` discipline the repo already requires of every tool
   (ASSISTANT.md, `frontend/src/agent/toolSummary.ts`). Bodies stay in
   `turn_tool_artifacts` and are re-readable by id (`read_artifact`).
3. **Committed writes replay as a generated ledger, not as prose.** A `SELECT` over
   `graph_write_provenance` for this note renders `op · address · value · confidence · why`.
   This is the real compaction win: N turns of tool chatter collapse into a K-line block
   that is **derived from the database**, so it is lossless in the only dimension that
   matters (what did I already write?), costs zero model calls, and cannot hallucinate. It
   also directly prevents the failure mode the repo has already hit twice — an agent losing
   sight of finished work and denying it happened (ASSISTANT.md, the deep-research and
   `web_fetch` cross-turn reference lines).
4. **Middle message turns** beyond K: drop `content`, keep one index line
   (`turn 12 · owner answered about Dr. Chen`).
5. **Overflow of last resort**: do **not** summarize. Open a **continuation conversation**
   (`supersedes_session_id`), seeded with turn 0, the ledger, and the open questions. Bounded,
   terminating, and no model on the critical path. The old conversation goes `superseded`
   and stays readable.

### 6.4 Resume is a background job, not a foreground request

A resume months later is always a **cold KV prefill** — the priming machinery
(`agent/priming.py:1-18`) keeps a warm prefix for the *interactive* persona only, and the
box holds one large model at a time. A 100k-token cold prefill is tens of seconds to
minutes on this hardware (the ~100 s cold-prefill incident is recorded in ASSISTANT.md
"Chat lifecycle"). So the resume path must be a **queued job whose output lands in the
silent queue**, not a request the phone waits on. This also satisfies #10:
untrusted-origin content never triggers a background job *on its own* — the owner's answer
is owner-originated, and the capture-time first pass is triggered by the owner's own
capture.

---

## 7. Migration sketch and the isolation tests each needs

Head is `0186` (`0186_aprs_audio_level.py`). Four migrations, in dependency order.

### 0187 — `note_conversations` + the monotone ratchet trigger

```sql
CREATE TABLE app.note_conversations ( … §3.1 … );
CREATE UNIQUE INDEX note_conversations_live_note_idx
  ON app.note_conversations (note_id) WHERE state <> 'superseded';
CREATE INDEX note_conversations_open_idx
  ON app.note_conversations (state, last_pass_at DESC) WHERE state IN ('open','awaiting_owner');

CREATE FUNCTION app.note_conversation_ratchet() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NOT (OLD.touched_domains <@ NEW.touched_domains) THEN
    RAISE EXCEPTION 'domain ratchet may only widen (% -> %)',
      OLD.touched_domains, NEW.touched_domains;
  END IF;
  IF NEW.domain_floor <> OLD.domain_floor THEN
    RAISE EXCEPTION 'domain_floor is immutable';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER note_conversations_ratchet BEFORE UPDATE ON app.note_conversations
  FOR EACH ROW EXECUTE FUNCTION app.note_conversation_ratchet();

ALTER TABLE app.note_conversations ENABLE  ROW LEVEL SECURITY;
ALTER TABLE app.note_conversations FORCE   ROW LEVEL SECURITY;
CREATE POLICY note_conversations_scopes ON app.note_conversations
  USING (app.is_owner() AND app.has_all_domain_scopes(touched_domains))
  WITH CHECK (app.is_owner() AND app.has_all_domain_scopes(touched_domains));
GRANT SELECT, INSERT, UPDATE, DELETE ON app.note_conversations TO jbrain_app;
```

**`tests/integration/test_note_conversations_rls.py`** —
(a) a `[general]`-scoped owner session reads a `{general}` conversation and **cannot** read
one whose `touched_domains` is `{general,health}`; (b) a full-scope owner reads both;
(c) a non-owner (`intake_context`, `device_context`) reads **none**;
(d) the ratchet trigger **raises** on an update that removes a domain, and succeeds on one
that adds; (e) `domain_floor` is immutable; (f) the partial unique index rejects a second
live conversation on the same note.

### 0188 — `agent_turns` extension + the policy swap

Columns of §3.2, the widened role CHECK, then the policy swap of §4.3, plus
`CREATE INDEX agent_turns_kind_idx ON app.agent_turns (session_id, kind, seq)`.

**`tests/integration/test_agent_turns_rls.py` (extended — the file exists)** —
(a) **regression:** a legacy chat turn with `domain_scopes = '{}'` stays visible to a
narrowed owner session (no silent hiding on upgrade); (b) a turn stamped `{health}` is
invisible to a `[general]` session and visible to `[general,health]`; (c) **the door:** a
turn stamped `{general}` inside a conversation whose `touched_domains` is `{general,health}`
is **invisible** to a `[general]`-only session — the retro-leak this design exists to close;
(d) `WITH CHECK` refuses an insert stamping a domain the session does not hold;
(e) an ordinary chat session (no `note_conversations` row) behaves exactly as before.

### 0189 — `turn_tool_calls` + `conversation_questions`

Both tables of §3.3/§3.4 with `is_owner() AND has_domain_scope(domain_code)` policies,
`UNIQUE (turn_id, idx)`, and the partial open-question index.

**`tests/integration/test_conversation_questions_rls.py`** —
(a) a health-stamped question is invisible to a `general` session;
(b) `WITH CHECK` refuses filing a question in an out-of-scope domain;
(c) a non-owner principal sees none;
(d) answering links `answer_turn_id` and flips `status`, and the answer turn's own stamp is
independently enforced;
(e) **purge:** deleting the note removes every question row (§5).

**`tests/integration/test_turn_tool_calls_rls.py`** —
(a) domain isolation on `domain_code`;
(b) `run_step_id` survives as NULL when its run is reaped (`reap_idle_run`), and the row
still reads — the durability claim of §3.3;
(c) cascade with its turn;
(d) arguments round-trip as JSONB with no truncation.

### 0190 — `graph_write_provenance` + `entity_mentions.chunk_id` relaxation

```sql
CREATE TABLE app.graph_write_provenance ( … §3.5 … );
CREATE INDEX gwp_fact_idx    ON app.graph_write_provenance (fact_id)    WHERE fact_id    IS NOT NULL;
CREATE INDEX gwp_entity_idx  ON app.graph_write_provenance (entity_id)  WHERE entity_id  IS NOT NULL;
CREATE INDEX gwp_note_idx    ON app.graph_write_provenance (note_id);
CREATE INDEX gwp_session_idx ON app.graph_write_provenance (session_id, committed_at);
-- §3.6 option (a)
ALTER TABLE app.entity_mentions ALTER COLUMN chunk_id DROP NOT NULL;
ALTER TABLE app.entity_mentions ADD COLUMN answer_turn_id uuid
  REFERENCES app.agent_turns(id) ON DELETE CASCADE;
ALTER TABLE app.entity_mentions ADD CONSTRAINT entity_mentions_one_anchor
  CHECK (num_nonnulls(chunk_id, answer_turn_id) = 1);
```

**`tests/integration/test_graph_provenance_rls.py`** —
(a) domain isolation on `domain_code`;
(b) the `num_nonnulls(...) = 1` CHECK rejects zero and two targets;
(c) deleting a fact cascades its provenance row (no orphans);
(d) **purge totality (ASSISTANT.md #11):** after `purge_note_artifacts` for a note, **zero**
rows remain in `graph_write_provenance`, `conversation_questions`, `turn_tool_calls`,
`agent_turns` (for that conversation), and `note_conversations` — the test the invariant
literally names, extended to the new tables;
(e) a mention anchored to `answer_turn_id` round-trips and its merge-reversal path still
resolves.

Plus one **non-RLS** integration test: `test_conversation_replay.py` — a 200-turn
conversation replays within the §6.2 budget, Q&A pairs are never evicted, and the ledger
block reflects every `graph_write_provenance` row for the note.

---

## 8. Open questions for the owner

1. **One conversation per note, forever?** The recommendation is one live conversation per
   note (`UNIQUE (note_id) WHERE state <> 'superseded'`), with re-analysis as a *new pass*
   inside it. The alternative is one conversation per pass, which keeps each pass clean but
   re-asks answered questions. Confirm the single-conversation model.
2. **Question text for a cross-domain question — templated or model-authored?** §4.4 step 2
   is the one place a general-scoped row could carry a hint of health content. The
   recommendation is a **server-templated** question for `kind='domain'` (the model supplies
   only the predicate name, which `domain_floor` already knows). Accept the loss of
   naturalness there?
3. **Does the agent get to widen its own read scope on the owner's "yes"?** Recommended: no
   — widening stays the owner-only endpoint, and the "yes" merely unblocks. But that makes
   the flow two taps instead of one. Is one tap worth letting an owner answer carry a scope
   change?
4. **Silent queue: one inbox or two?** `review_items` already exists and is already silent.
   Recommendation is two tables, one PWA surface. Or should agent questions *be*
   `review_items` rows with a new `kind`, accepting the resolution-handler mismatch (§3.4)?
5. **Abandonment window.** *Assumed* 30 days before `state='abandoned'` and open questions
   `expired`. Too aggressive for a note captured on holiday and answered in September?
6. **Does an unanswered question block anything?** Recommendation: no — high-confidence
   writes commit regardless, and the question only gates the uncertain ones. The alternative
   (hold the whole note's graph until answered) is safer and much more annoying.
7. **Do committed facts get re-litigated on a model upgrade?** Recommendation: **no** — the
   `prompt_version` stamp makes a deliberate, budgeted re-run possible (ANALYSIS.md
   "Reprocessing") but nothing automatic. Confirm you want the graph stable across model
   swaps rather than self-correcting.
8. **Deleting a conversation.** It loses the explanation but keeps the facts (§5). Should
   deleting a conversation instead be forbidden while it has provenance rows, so the
   *why* is never separable from the *what*?
9. **Does `resolution_pin` (`models/workflow.py:123-163`) survive?** It exists to make the
   Integrator's identity/predicate decisions converge across re-runs. With the Integrator
   deleted, does the conversation's own ledger replace it, or does the agent still write
   pins so a re-pass is deterministic?
10. **`entity_mentions.chunk_id` relaxation (§3.6).** It touches the table that makes entity
    merges reversible. Confirm option (a) — a turn as an alternative anchor — before any of
    this is built, because it is the one change that reaches into settled Phase-3 structure.
