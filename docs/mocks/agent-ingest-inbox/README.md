# The review inbox becomes two tabs — where that lives, and how loud it is

> **Status:** GUI gate **OPEN** — three variants for the owner to choose from. Nothing is
> built. The chosen mock becomes the binding spec (`docs/reference/PROCESS.md` "GUI gate").
> Build plan: `docs/plans/AGENT_INGEST_CONVERSATION_PLAN.md` **D4/D5**, wave **W3**.

## What is already decided (not up for a vote here)

The review inbox is **not** deleted. Under the agent-conversation ingestion change the agent
commits its reading and shows you what it did, so the arbiter-derived ingest cards
(`low_confidence_inference`, `ambiguous_mention`, `new_predicate`) stop being filed — there is
nothing left to approve. What survives is **two tabs**:

- a **notes** tab — note conversations with an open `ask_owner` question, "threads waiting on
  you". These are **not** review cards. Nothing is approved; the answer is a reply, and it
  belongs in the thread. This tab exists because a question otherwise only exists inside its
  own conversation, and there would be no way to find one you hadn't opened;
- a **wiki** tab — findings that never start from a note and so have no thread to live in:
  - `wiki_contradiction` and `wiki_stale_claim` from the nightly linter
    (`backend/src/jbrain/wiki/lint.py:536,671`),
  - the EMR importer's failures — `emr_unrecognized_source` (no parser fingerprint matched,
    `ingest/emr/import_handler.py:282`) and `emr_intake_failed` (the archive would not open,
    `ingest/emr/intake_handler.py:186`),
  - and the **location firewall's catch** (`ingest/emr/firewall.py`) — the Layer-2 guard that
    keeps a home address out of the health domain. It holds the fact and never commits it;
    W4 gives that catch its card, and this tab is where it lands.

  These *are* real review cards with real payloads, and they keep the shipped list → detail,
  the typed block registry (`frontend/src/review/blocks/registry.ts`) and the undo snackbar.

**Binding on all three variants:** no push notifications and no nagging badges. A passive
count or a quiet dot is allowed — the constraint was about push and nagging, not about
in-app state, and the Tasks unviewed-recognition pattern (`docs/reference/DESIGN.md` "Tasks —
the result band": a `--steel` left-edge bar, a NEW pill, and a launcher tile badge off a
device-local viewed map) is the shipped precedent. Nothing here may read as an unread-mail
counter demanding zero.

## The three variants

Each stages the **same content** so they compare directly: three threads waiting (2 days,
6 hours, and a deliberately stale 9 days), one conversation still on its first pass
("nothing written yet"), and five wiki findings — a linter contradiction with both sides'
edges and the source chunks, a stale claim, the firewall catch, an unrecognised EMR source,
and a failed archive.

| | File | Where it lives | How the two tabs relate | The signal |
|---|---|---|---|---|
| **A** | `a-inbox-holds.html` | The **Review launcher tile**, unchanged. | **Peer tabs** on one screen: the `pending · decided` segmented filter becomes `Notes · Wiki`. Decided hangs off the Wiki tab only. | The existing **tile badge** (`8`), polled only while the launcher is open. Nothing counts on home. |
| **B** | `b-waiting-bucket.html` | **A fourth bucket on the chats picker** — `Waiting · Today · Older · Archived`. The Review tile is retired. | **Nested**: the two tabs are a quiet underline toggle *inside* the Waiting bucket — `Threads` / `Wiki`. | **None, anywhere**, until you open Chats and land on that segment. The quietest of the three. |
| **C** | `c-waiting-sheet.html` | **Global chrome** — a "Waiting on you" **sheet** raised from a steel dot in the top bar's status cluster, on every screen. The Review tile stays as the second door. | **Peer tabs inside the sheet**; a wiki finding **expands in place** rather than pushing a detail (a sheet stays one level deep). | **One 8px `--steel` dot. Never a number.** Absent entirely when nothing waits. |

### What choosing each one costs

- **A — "the inbox holds".** Costs discoverability: Review is still a place you have to
  remember to go, and a question on a nine-day-old note is only found by someone who went
  looking. It also asks one screen and one word to carry two things that behave differently —
  a thread you reply to and a card you decide.
- **B — "waiting lives with the conversations".** Costs the destination: the Review tile
  retires, and with it the one place the system could say "there are things outstanding"
  without being asked. Wiki findings and a failed medical import end up homed inside a screen
  called *Chats*, where they have no natural business — and micro-row density means you read
  the gist of a question, never the whole thing, before opening it. **It also depends on the
  sibling gate**: B only has a host if note conversations are listed in the chats picker at
  all (`docs/mocks/agent-ingest/`, option C). If that gate lands on A or B instead, this
  variant has nowhere to live.
- **C — "waiting on you, one dot".** Costs principle: a persistent mark in global chrome is
  the closest of the three to the line the owner drew, even without a number attached — this
  is the variant to reject if "quiet" means "invisible until asked". It costs structure too:
  a sheet is shallow, so findings expand in place instead of getting the shipped list → detail
  with prev/next, and the full Review screen has to keep existing behind the launcher tile for
  the cases that need it.

## How they were built

All three are standalone, no build step, phone-viewport first, correct in **both** themes
(toggle in the demo bar), on the `docs/reference/DESIGN.md` token sheet with **no raw hex
outside the token block**, ≥44px targets, `prefers-reduced-motion` honoured. The tabs really
switch; rows really open. Colour stays informational — green committed, amber an open
question, steel agent/info, rose health, teal location.

They reuse rather than invent: the list rows are `ReviewScreen`'s `.rlist2/.rrow2`, the
contradiction detail is the shipped `claim:contradiction` block's shape (source chunk as hero,
each side's `predicate → statement` beneath — the settled reading from
`docs/mocks/review-wiki-contradiction-b-source.html`), the escape hatches are the inbox's own
**defer** and **talk it over**, and B's picker is `frontend/src/agent/SessionsPanel.tsx:165-170,254-271`
with one bucket added.

## Two things the owner should settle while choosing

1. **Does the firewall catch's second option exist?** All three mocks offer *"file it under
   Location"* beside *"keep it out"*. That is a real graph write of a held address into
   another domain, from the inbox. If the answer is no, the card is informational only and
   its single action is *dismiss*.
2. **A doc correction rides along either way.** `docs/reference/DESIGN.md:899-953` still
   describes a **three-lane** review inbox (`pending · deferred · decided`); only two lanes
   shipped. Whichever variant wins, that section is rewritten to the two tabs and the drift is
   corrected in the same PR (`docs/DOC_LIFECYCLE.md`).
