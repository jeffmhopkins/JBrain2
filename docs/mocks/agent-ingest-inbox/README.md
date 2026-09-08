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
  you". These are **not** review cards. Nothing is approved; the answer is a reply. This tab
  exists because a question otherwise only lives inside its own conversation, so one you had
  not already opened would be unfindable;
- a **wiki** tab — findings that never start from a note and so have no thread to live in:
  - `wiki_contradiction` and `wiki_stale_claim` from the nightly linter
    (`backend/src/jbrain/wiki/lint.py:536,671`),
  - the EMR importer's failures — `emr_unrecognized_source` (no parser fingerprint matched,
    `ingest/emr/import_handler.py:282`) and `emr_intake_failed` (the archive would not open,
    `ingest/emr/intake_handler.py:186`),
  - and the **location firewall's catch** (`ingest/emr/firewall.py`) — the Layer-2 guard that
    keeps a home address out of the health domain. It holds the fact and never commits it.

**Binding on all three variants:** no push notifications and no nagging badges. A passive
count is allowed — the constraint was about push and nagging, not in-app state, and the Tasks
unviewed-recognition pattern (`docs/reference/DESIGN.md` "Tasks — the result band") is the
shipped precedent. Nothing here may read as an unread-mail counter demanding zero.

## The three variants

Each stages the **same content**: three threads waiting (9 days, 2 days, 6 hours), one
conversation still on its first pass, and the five wiki findings above.

| | File | Where it lives | How the two tabs relate | The signal |
|---|---|---|---|---|
| **A** | `a-inbox-holds.html` | The **Review launcher tile**, unchanged. | **Peer tabs** on one screen: the shipped `.review-segs` track is relabelled `notes · wiki`. | The existing **tile badge** (`8`), polled only while the launcher is open. Nothing counts on home. |
| **B** | `b-waiting-bucket.html` | **A fourth bucket on the chats picker** — `Waiting · Today · Older · Archived`. The Review tile retires. | **Nested**: the two tabs are a quiet underline toggle inside the Waiting bucket. | **None, anywhere.** The bucket carries no count pill, and the picker's follow-the-data default is carved out so Chats never lands on it. |
| **C** | `c-question-in-stream.html` | **The question renders inline under its note in the home stream**; the inbox holds only what has aged out of the two-day window. | **Peer tabs** on the Review screen, but the notes tab is a *remainder*, not the whole list. | The tile badge counts only what you cannot already see — 5 findings + 1 aged-out question = `6`. Top bar untouched. |

**A's primary form puts the answer buttons in the row.** A waiting thread is answerable with
one 44px tap from the notes tab — no navigation, nothing approved — so the nine-day-old
question, which is the entire justification for the tab, drains in one gesture. C takes the
same control one step further out, onto the stream itself.

### What choosing each one costs

- **A — "the inbox holds".** Review is still a place you have to remember to go. It also asks
  one screen and one word to carry two things that behave differently — a thread you reply to
  and a card you decide. Mitigating it: there is no bottom nav, so the launcher *is* the
  navigation spine and its badge is passed many times a day without being pushed.
- **B — "waiting lives with the conversations".** The Review tile retires, and with it the one
  place the system could say "there are things outstanding" without being asked. Wiki findings
  and a failed medical import end up homed inside a screen called *Chats*. And at the picker's
  settled ~46px density and the app's 75% text scale **a Waiting row cannot show its question
  at all** — only that one exists; a second `--fs-micro` line is 9px on the owner's phone,
  about four words. **It also depends on the sibling gate**: B has a host only if note
  conversations are listed in the chats picker (`docs/mocks/agent-ingest/`, option C).
- **C — "the question comes to the note".** It puts agent turns into the home stream. The
  stream is a **chat log, not a feed** — notes sort ascending (`useNotes.ts:287`) and the
  scroller pins the bottom on every append (`Stream.tsx:229-234`) — so a question renders
  *below* its note, and a question on the newest note lands **between that note and the
  composer**: an agent turn in the thumb zone directly above "What happened?". What scrolls off
  the top is older history, not the new capture; the thing crowded is capture itself. It also
  re-opens the lifecycle chip's documented "analyzed → no chip, the quiet end-state" doctrine
  (`notes/lifecycle.ts:8`). And it has a **seam**: placement keys on the *note's* age, not the
  question's, so a note near the two-day edge — and every backdated import — has left the
  window before its question exists and never appears on home at all. The stream is the fast
  path; the inbox is the guaranteed one. If home is a capture surface that must stay pristine,
  C is the wrong answer.

## Fidelity: what the mocks show, and what does not exist yet

The mocks render **only actions the producers actually file**. Everything below is a real gap
the plan must own; none of it is designed around in the HTML.

1. **The linter's two verbs are the only wiki-card actions.** `lint.py:781-792` files every
   card with `{"action":"dismiss","label":"Dismiss"}` and
   `{"action":"correct","label":"File correction note"}` — both universal `_apply_resolution`
   branches, no new resolution code — and `payload.ts:235` renders `payload.choices` verbatim.
   There is **no** "rule for this side", "close the interval", or "re-stamp as current" verb.
   If the owner wants the inbox to settle a contradiction rather than hand it to a correction
   note, **that is new resolution code and new scope**, and it needs its own decision.
2. **Dismiss does not suppress.** `_file_card` (`lint.py:770-780`) dedups on `status='open'`
   only, so the next nightly run re-files the identical pair. The mocks say exactly that. The
   shipped pattern that *does* suppress is `_file_confirm_entity_card`
   (`analysis/pipeline.py:1354-1368`), deduped on entity id **across all statuses** "so a
   dismissed proposal never nags again". Bringing the linter to that behaviour is a change the
   plan must schedule — without it, "dismiss" on a recurring finding is a treadmill.
3. **The three EMR cards advertise no proposals at all.** They file no `choices`, no `accept`
   and no `reject`, so `proposalsFor` returns `[]` and the detail renders a "choose among
   proposals" header over nothing; the only exit is the footer's *correct it*. The mocks draw
   that honestly. **They need at least a dismiss.**
4. **The firewall card does not exist.** `import_handler.py:129` discards
   `integrate_parse_result`'s return value, so the Layer-2 catches are never filed. The guard
   holds the fact and never tells the owner. *(Carried into the plan as W4 work.)*
5. **And its payload is thinner than a card wants.** `FirewallCatch`
   (`ingest/emr/importer.py:51-58`) records `entity_kind`, `predicate`, `anchor`, `subkind` —
   **no entity name, no note id, and no held value** (`statement` and `value_json` are dropped
   at the early return, `:99`). So the card cannot say *which* organization or *which* import,
   and the mocks render only the four fields that exist. Adding a name and a note id means the
   producer must start retaining them.
6. **No "file it under Location".** An earlier draft offered it; it is removed.
   `docs/plans/EMR_IMPORT_PLAN.md:471-474` already specifies the sanctioned path — should a
   facility address ever legitimately be needed it is added **deliberately** as a
   location-domain `Place` sidecar, never as a health-entity fact. A one-tap commit button on
   a health-domain card is the opposite of deliberate, and pairing "the guard stopped a leak"
   with "commit it anyway" would relocate the single point of failure the two-layer design
   removes from the parser to a thumb. The card is **informational**, and once (3) lands its
   only action is dismiss.
7. **`emr_unrecognized_source` has no filename.** Its payload is `{note_id, subkind,
   attachment_id}`, so the row cannot name the PDF. The mocks show the attachment id. A
   filename means either widening the payload or resolving it client-side.
8. **Contradiction sources have no dates.** `lint.py:622` appends `{"text": chunk_text}` only.
   The mocks quote the chunks unattributed.
9. **`wiki_stale_claim` has no edge to draw.** Its payload is `{entity_ids, fact_id, summary}`
   on the generic block sequence (`registry.ts:60-61`), so it renders as a summary. An earlier
   draft invented a `hiringStatus` edge panel; it is gone.
10. **The badge does not count threads.** `reviewCount` is `queue.items.length`
    (`Launcher.tsx:252-254`) — review items only. A's `8` (3 threads + 5 findings) and C's `6`
    (1 aged-out thread + 5 findings) both need the count widened to include waiting
    conversations. B needs no such change, because it shows no badge.
11. **B changes the picker's default.** Its Waiting bucket is carved out of
    `SessionsPanel`'s `tab ?? first non-empty` rule (`SessionsPanel.tsx:273-281`,
    `DESIGN.md:1285-1287`) so Chats keeps landing on Today. Without the carve-out a
    first-placed non-empty Waiting bucket would be the landing screen every time — louder than
    a launcher badge, not quieter. It is a real behaviour change to a settled default, not a
    styling choice.
12. **An empty lane shows a `0` today.** `ReviewScreen.tsx:534` renders the count pill whenever
    the count is defined; only the tile badge gates on `> 0` (`Launcher.tsx:370-371`). All
    three mocks hide the pill at zero. That is a small deliberate improvement, not existing
    behaviour.

## How they were built

Standalone, no build step, phone-viewport first, correct in both themes, on the
`docs/reference/DESIGN.md` token sheet with **no raw hex outside the token block**,
`prefers-reduced-motion` honoured.

- **Text size is the app's real default.** Each phone frame sets `--font-scale: 0.75`
  (DESIGN.md "Theming"), so every string inside a device is at the size the owner actually
  sees. Mock commentary lives *outside* the frames at 100%, and nothing inside a phone is
  anything but app copy.
- **Tap targets are ≥44px including padding** (DESIGN.md:127-128) — segments, answer chips,
  detail prev/next, back and close.
- **No accent-on-tint text.** Proposal buttons are the shipped `.rprop`: plain `--surface`,
  hairline border, label in `--text`. Where a control is emphasised it uses the
  `.review-seg.seg-active` recipe (`styles.css:6278-6282`) — tinted ground, `--text` label.
- **Everything advertised as interactive works.** In all three, both tabs switch, every wiki
  row opens a detail, and prev/next walks all five items with the ends disabled. In A and C,
  every answer chip resolves its thread and updates the count. The `correct it` composer opens
  in A.

## Two things the owner should settle while choosing

1. **Do the wiki cards get real verbs?** Today the answer to a contradiction is *dismiss* (which
   does not suppress) or *file a correction note* and wait for the next build. That is defensible
   under non-negotiable #7 — the wiki stays machine-written — but it is slow, and the mocks show
   how slow. Adding verbs is scope; so is fixing dismiss.
2. **Doc corrections ride along either way.** `docs/reference/DESIGN.md:899-953` has drifted
   from the code in two places, and whichever variant wins, both are corrected in the same PR
   (`docs/DOC_LIFECYCLE.md`):
   - it describes a **three-lane** review inbox (`pending · deferred · decided`); only two
     lanes shipped, and this change replaces them with the two tabs;
   - `:930-935` describes **defer** and **talk it over** as "two universal escape hatches
     [that] sit in the footer", but `Footer.tsx` renders only `rfoot-correct` for the pending
     lane. The mocks show no defer and no talk-it-over because the code has none — the doc is
     what is stale, not the mock.

*(An earlier variant C — a "Waiting on you" sheet raised from a steel dot in the top bar's
status cluster — was withdrawn. The owner has already ruled that cluster non-tappable and its
8px dot deleted, `DESIGN.md:221-223` and `TopBarVitals.tsx:9-13`; the dot was also bare colour
with no paired text, and a five-finding sheet breaks the Sheet contract's single-primary-action
rule at `DESIGN.md:1371-1382`. Its one good idea — answering inline — was kept, and is now A's
primary form.)*
