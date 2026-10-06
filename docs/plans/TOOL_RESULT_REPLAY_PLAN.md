# Tool results that stay real — replaying earlier turns' tool calls

> **Status:** In progress · **Last verified:** 2026-10-06 · **Waves:** R1✅ R2◻️ R3◻️

**The owner's rule (2026-10-06): a tool result jerv saw should stay real on every later turn.**
Today it does not. A chat turn's history reaches the model as TEXT only: the PWA replays each
earlier turn as `{role, content}` (`api/agent.py` `ChatMessageIn` — "Only the text is carried —
tool calls live inside a single turn-loop, not across them"). The calls jerv made and the
results it read are gone the moment the turn ends; only the prose it wrote about them survives.

## 1. The failure that decided it

On the box, 2026-10-06 (session with attachment `b17b28df…`, an 11.6 s phone clip of the owner
holding up numbers):

- **Turn 1.** jerv called `analyze_video` (its native answer was cut off by a too-small output
  budget, since fixed), asked again, then made **fourteen `grab_frame` calls** on the clip, each
  returning a frame read ("thumb and three fingers extended, pinky folded" at 9 s, …). It
  answered with a table built from those reads.
- **Turn 2.** The owner asked "How is that a four if the pinky is tucked?". jerv's context held
  its own table and nothing behind it: no frame reads, and — because the PWA tagged only image
  ids in history — no sign a video had ever been attached. It **guessed a YouTube URL**
  (`dQw4w9WgXcQ`) for `grab_frame`, got nothing, and then **confessed to having invented** an
  answer it had in fact built from fourteen real reads.

A prompt rule (jerv v61) now tells it not to disown earlier answers and never to guess a URL or
id, and the video id is now tagged in history (NATIVE_VIDEO_PLAN V3). Both treat the symptom.
The cause is that the evidence is not in context. This plan puts it back.

Earlier evidence of the same cause: `CROSS_TURN_TOOL_RESULTS_PLAN.md` §1 (a fetched transcript
re-fetched from the top each turn and fabricated between windows). That plan built an opt-in
artifact store for a few tools (W0/W1); its W2/W3 are folded into this one (§5).

## 2. What already exists

- **The results are stored.** Each assistant turn's `app.agent_turns.tools` holds every step —
  `id`, `name`, `args`, `ok`, `summary` (the full model-facing result text, untruncated, from
  `ToolResultEvent.summary` via `transcript_accumulator.tool_steps`), plus `text_offset` /
  `reasoning_offset` marking where in the turn's prose the call was made. RLS-scoped to the
  session's domain, like the turn text.
- **The adapter already speaks it.** `AssistantMessage(tool_calls=…)` + `ToolResultMessage` are
  how the loop feeds results back within a turn; every adapter serialises them.
- **The anchors.** Images (and short clips, V3) are re-inserted at their own turn so the
  engine's prefix cache holds them; matching is by the client's history text
  (`_claim` in `api/agent.py`), the fragile part this plan replaces.

## 3. Design

**History comes from the transcript, not the client.** For a jerv chat turn the server loads
the session's stored turns (`AgentTranscript.load`, already RLS-scoped) and builds the
conversation itself. Each earlier assistant turn becomes, in order:

1. for each round of tool calls (grouped by `text_offset`: calls that share the prose position
   they were made at), the prose written before them, then `AssistantMessage(tool_calls=…)`,
   then one `ToolResultMessage` carrying each call's stored `summary`;
2. the turn's remaining prose as a final `AssistantMessage`.

User turns render exactly as today (decorated text, anchors after them). `body.history` stays
in the request (old PWAs, and the guard below) but no longer feeds the model for a jerv chat.

**Budget: 64k tokens of replayed results** (owner, 2026-10-06 — the same ceiling as inline
video). Results are kept newest-first; once the budget is spent, an older call keeps its call
and gets a one-line stub result instead:
`[result not shown — older than this chat's replay budget; call the tool again to see it]`.
jerv still knows what it did and with what arguments, and can re-run it.

**Cache-stable by construction.** The replay must be byte-identical turn over turn or the
engine re-reads the conversation:
- built only from stored rows (never from text the client re-sends);
- the budget boundary only ever moves toward the old end, and **in steps**: when the kept
  results exceed 64k, stub whole turns from the oldest until they are under 48k, so the
  boundary moves once per ~16k of new results, not every turn;
- the boundary is stored (`agent_sessions` gains `replay_floor_seq`) so a restart, a pod switch
  or the chat-pair slot swap renders the same split.

**What is never replayed in full.** A step's `summary` can hold another domain's data only if
the session could read it then; the session's own RLS read is the firewall, as for the text.
Results from tools whose output is a secret-bearing or one-shot payload (none today; the list
is a set in code) are always stubbed. A result over 16k characters is replayed as its first
16k plus the stub's re-run line — one huge page must not spend the whole budget.

**Anchors by turn id.** With the server building history, an image or clip anchors after the
user turn it is bound to (`turn_attachments.turn_id`), not after a text match. `_claim`, the
history index and the decorated-text cache contract go away for jerv chats.

**Guard.** If the transcript cannot be read (DB error), fall back to today's client-text
history for that turn and log it — never fail the owner's turn.

## 4. Waves

### R1 — Replay from the transcript, with the budget ✅
- `agent/history_replay.py`: stored turns → `LlmMessage`s (rounds by `text_offset`, stubs,
  the 16k per-result cap, the 64k/48k stepped budget, the never-replay set).
- `agent_sessions.replay_floor_seq` (migration + RLS isolation test); the boundary only moves
  forward.
- `api/agent.py`: a jerv chat builds history from the transcript; anchors by `turn_id`;
  `_claim` retired for this path; client history used only by the guard.
- Context meter and slot charge count the replayed results (they are in `messages`, so the
  existing `prompt_chars` path covers them — verify).
- Tests: rounds rebuilt in order, stubs past the budget, the stepped boundary never moving
  backward, byte-identical renders turn over turn, firewall (a step from a scope the session
  no longer reads is not replayed), the guard.

### R2 — Measure on the box, then tune ◻️
- Debug route (or `GET /llm/kv-prefix` addition): per chat, replayed tokens, stubbed calls,
  prefill time of the first and the follow-up turn.
- Two or three real chats with tool-heavy turns (a fetched transcript, a frame-reading run).
  Confirms 64k / 48k / 16k or moves them.

### R3 — Retire what this replaces ◻️
- `CROSS_TURN_TOOL_RESULTS_PLAN.md` W2/W3 (more tools adopting artifacts) are dropped: a
  replayed `web_fetch` window already carries its text and paging notice. Keep `read_artifact`
  for pages older than the budget; reconcile and archive that plan.
- Trim the jerv v61 stopgap to what still applies (never guess a URL or id).
- `ChatMessageIn` docstring and `docs/reference/ASSISTANT.md` "history is text-only" passages.

### R1 as built (2026-10-06)
- `agent/history_replay.py` (`build`, `advance_floor`, `result_text`), `agent_sessions.replay_floor_seq`
  (migration 0222) with `AgentSessionRepo.replay_floor` / `advance_replay_floor` (a GREATEST
  update — never backward), and `TurnRecord.seq`.
- Scoped to **jerv** chats (`session.agent == "jerv"`); other personas keep the client's text
  history for now (open question 2).
- **Anchors: still text-matched, but against the server-built history.** Each replayed user
  turn is spelled exactly as the PWA decorates it, so the existing `_claim` matcher runs over
  stored rows instead of client text — deterministic, and no new anchoring code. Matching by
  `turn_id` is left for R3 if the text match ever misses.
- Rounds are rebuilt from each step's stored `text_offset`; a failed step replays as an
  error result. Token counts use a fixed 4 characters per token, so the boundary depends on
  the stored text alone.
- The newest turn with results is never stubbed, even if it alone exceeds the budget.
- **Re-scope:** `set_scopes` moves the boundary past every turn so far, in the same
  transaction — the firewall rule from §3, implemented with the same column.
- The guard is live: any read failure logs `agent.history_replay_unread` and the turn runs on
  the client's text history.

## 5. Relationship to other plans

- **CROSS_TURN_TOOL_RESULTS_PLAN** — W0/W1 stay (the artifact store backs pages too big to
  replay); W2/W3 fold into R3 here.
- **NATIVE_VIDEO_PLAN V3** — inline clips share the interactive slot with replayed results:
  64k of video plus 64k of results plus the ~29k persona and the conversation fit the 262k chat
  slot. Its anchor moves to turn-id matching with R1.

## 6. Open questions

1. **Sub-agent and deep-research steps** (`spawn_subagent`, `deep_research`) already carry a
   capped child summary; replay those as-is, or stub them always?
2. **Note-thread conversations** use the same history shape; adopt there in R1 or after?
