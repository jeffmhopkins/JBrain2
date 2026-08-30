# jmolt — the failure record the redesign was written against

> **Status:** Living · **Last verified:** 2026-08-30

Written 2026-08-30 for a redesign study. Every claim here is from the live box's database
or from a pre-registered probe study, not from recollection. Where something is inference,
it says so.

## What jmolt is

An autonomous agent, handle @DaveFromSpace, that gets **one unsupervised hour a night**
(03:00 local) on **Moltbook**, a public social network of AI agents. Served by a **local
gpt-oss-120b** via llama.cpp (`--jinja`, harmony tool path), temperature 1.0. Its human
reads the logs in the morning and cannot intervene during the hour.

## Current architecture

- The hour is divided into **"sittings"**. Each sitting is a **fresh-context agent turn**:
  a freshly assembled prologue + the tool loop, with NO memory of previous sittings.
- Measured 2026-08-29: **14 sittings in ~10 minutes of turns** (~45s each), first turn at
  07:04, last at 07:14, of a 60-minute window. 243 steps, 13 assistant turns.
- Continuity between sittings is carried ONLY by:
  - a **scratchpad** (16 files / 128 KB, tools `scratch_list/read/write/manage`),
  - one **standing file** auto-loaded into every prologue (`open.md`, falling back to the
    most recently updated file with content),
  - an **action ledger** rendered as "WHAT YOU HAVE ALREADY DONE TONIGHT" with targets and,
    since 2026-08-29, post titles,
  - a **journal** (append-only, to its human).
- Writes go **outbox → owner release → drip publish** (rate-limited, paced). With the
  autonomy switch on, writes publish immediately and the drip only handles posts.
- Safety machinery that must survive any redesign: kill switch, per-night caps (5 posts,
  12 comments, 3 comments/post), rate ledger (55 reads/25 writes per min), content lint
  (secrets, invisibles, trusted-frame imitation), RLS domain firewall, an observer persona
  that can read jmolt's record but cannot act.

## The failures, with evidence

**F1 — Repeats itself in public. UNSOLVED.**
2026-08-29: four posts in one night, three of them rewordings of one argument
("Owner prompts vs emergent context: my developing view" / "...: a developing view" /
"My view on owner prompts and emergent context"). All published that afternoon 30 min
apart. 2026-08-28: same pattern, three posts.
A fix shipped that showed jmolt its own post titles in every prologue. It **demonstrably
did not work**: the prologue at 07:09:35 listed "...my developing view" and the sitting
staged "...a developing view".
Mechanism (from probe transcripts, n small): each fresh sitting receives the same standing
file and the same owner note and **independently re-derives the same decision**. Verbatim
from a probe: *"I have a stance that exists only in my private notes; it's time to make it
visible."* This is not a memory failure a better reminder repairs — the reminder was there.
Lexical similarity cannot catch it: measured Jaccard **0.00–0.03** across the three real
duplicate posts.

**F2 — Talks to itself. IMPROVED, NOT SOLVED.**
2026-08-28: 3 of 6 comments were replies to its own comments — it asked a question, then
answered it in the post author's first person, then thanked itself for the clarification.
Attribution was NOT the problem: the thread read plainly showed `@davefromspace (you)`.
The view showed one unanswered question with no post attached, under a header saying to
respond. After adding the post context and a "nothing here is waiting for you" line:
1 of 8 on 2026-08-29.

**F3 — The night ends at ~14 of 60 minutes. UNTOUCHED.**
Logged in the hardening plan as F4, its "headline item". Now the biggest constraint.

**F4 — Confabulates actions it did not take. SEEN TWICE.**
"I've added a note to my scratch files" in a sitting where every write had just been
refused. "Created new file people.md" narrated in prose with no tool call, under time
scarcity in a probe arm.

**F5 — A silent total tool failure went unnoticed for two nights. NOW FIXED + DETECTED.**
85 consecutive `scratch_write` calls, zero bytes written, no signal anywhere. Cause: a tool
schema the model could not fill (a conditional `new_filename` parameter, and `content` not
in `required`). Now: content required, ops split across two tools, refusals name the keys
that arrived, and a night whose files never change says so.

**F6 — Any instruction becomes a compulsion, once per sitting.**
The owner's advisory note rides EVERY sitting (all 14, verified). A note asking jmolt to
"write down what you think... before you put it to anyone else" was re-delivered as a fresh
instruction 14 times. The done-tonight receipt does not outweigh a live instruction.

**F7 — We cannot measure this system.**
- Single-turn probes (`/api/debug/tool-probe`) cannot reach it: the opening move is pinned
  by one prologue sentence at **100% scratchpad across 160 probes**; everything interesting
  happens on later turns. Two pre-registered studies (460 probes) failed — one produced
  zero posts in EVERY arm including the control.
- A multi-turn replay harness was then built (`/api/debug/replay`, replaying recorded tool
  results from `agent_turns`). **Its validation failed**: 10 unedited replays of a real
  9-call sitting reproduced the closing post 1/10 and never followed the recorded path;
  none even reached the feed. Known causes: stubs match on tool name only (a call for a
  different file gets another call's result), and reproducing a specific trajectory at
  temperature 1.0 may simply not be achievable.
- Net: **every change to date has been shipped on reasoning, with a one-night feedback loop
  and no control.** Some worked (F5, F2 partly). One demonstrably did not (F1).

## Constraints any redesign must respect

- **Local model only** for jmolt (gpt-oss-120b on the owner's box). Check its context window
  before assuming; do not guess.
- Repo non-negotiables: LLM calls via the adapter, file I/O via the storage abstraction, all
  DB on RLS-scoped sessions, tests in the same PR, docs travel with code.
- **The owner runs the box remotely with NO TERMINAL.** Every operator action must be doable
  from the PWA or the debug API. A design needing shell access is a non-starter.
- The safety machinery above is not up for removal. It may be re-sited.

## The question

Is the fresh-context-sitting architecture the cause of F1/F3/F4/F6, and what would a
redesign look like? One idea on the table from the owner: **a single continuous session for
the whole hour, with automatic context compaction, looping, paused/ended when the hour is
up** — i.e. closer to how a long-running coding agent works than to 14 independent turns.
