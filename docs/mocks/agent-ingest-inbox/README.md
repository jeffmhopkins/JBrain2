# The review inbox becomes two tabs — where that lives, and how loud it is

> **Status:** GUI gate **SETTLED — variant A chosen.** `a-inbox-holds.html` is the binding
> spec (`docs/reference/PROCESS.md` "GUI gate"). `b-waiting-bucket.html` and
> `c-question-in-stream.html` are **superseded**, kept as the record of the round and not
> maintained. Nothing is built yet.
> Build plan: `docs/plans/AGENT_INGEST_CONVERSATION_PLAN.md` **D4/D5**, wave **W3**.

## The ruling

> *"The conversation should be the only place where interaction for note ingestion actually
> occurs. The inbox just redirects there and is an easy point of access."*

Two consequences, and they are what separate the binding mock from the version that was
reviewed:

- **The notes tab decides nothing.** A row is a redirect — which note it came from, what is
  being asked, how long it has waited, how much the agent already committed — and tapping it
  opens the conversation. The reviewed draft put answer chips in the row; **they are removed.**
  Answering in the inbox *and* in the thread would re-create the second decision surface this
  whole design exists to delete, which outweighs the one-tap drain they bought.
- **The note screen does not change at all.** A note conversation is an ordinary agent
  conversation — Full Brain / Jervis — with the note as turn 0 and the agent's writes and
  clarification asks rendered as custom tool components inside it. The companion note-body mock
  round was scrapped for that reason. This gate therefore draws the conversation thinly and
  deliberately: where a row *lands* is the sibling gate's business
  (`docs/mocks/agent-ingest/`); all this one settles is that the row goes there.

**The wiki tab is unaffected** by the ruling. Those cards are not note ingestion, have no
thread to redirect to, and keep the two verbs their producers actually file.

### Why B and C lost

- **B — "waiting lives with the conversations".** Ruled out on its dependency: it has a host
  only if note conversations are listed in the chats picker at all, which is the sibling gate's
  call and not this one's to assume. It was also the variant that most thoroughly retired the
  Review tile, and the ruling keeps that tile as the "easy point of access".
- **C — "the question comes to the note".** Its premise is gone. C's whole idea was answering a
  question *outside* the conversation, on the home stream; the ruling makes the conversation the
  only place that happens. Its own cost stood independently: the stream is a chat log
  (`useNotes.ts:287` ascending, `Stream.tsx:229-234` pins the bottom), so a question renders
  below its note and one on the newest note lands between that note and the composer.

## What is already decided (not up for a vote here)

The review inbox is **not** deleted. Under the agent-conversation ingestion change the agent
commits its reading and shows you what it did, so the arbiter-derived ingest cards
(`low_confidence_inference`, `ambiguous_mention`, `new_predicate`) stop being filed — there is
nothing left to approve. What survives is **two tabs**:

- a **notes** tab — **questions and approvals waiting**. These are **not** review cards and
  nothing is decided in them; each row redirects into the conversation that raised it. Two
  kinds:
  - an `ask_owner` **question** from a note conversation, and
  - a staged **approval** — `prefs_write` stages a Proposal before it writes (plan **D17**), so
    a pending change to your standing instructions (`owner_prefs`) waits here too, and routes
    to its conversation exactly the same way a question does.

  The tab exists because both otherwise live only inside their own conversation, so one you had
  not already opened would be unfindable;
- a **wiki** tab — findings that never start from a note and so have no thread to live in:
  - `wiki_contradiction` and `wiki_stale_claim` from the nightly linter
    (`backend/src/jbrain/wiki/lint.py:536,671`),
  - the EMR importer's failures — `emr_unrecognized_source` (no parser fingerprint matched,
    `ingest/emr/import_handler.py:282`) and `emr_intake_failed` (the archive would not open,
    `ingest/emr/intake_handler.py:186`),
  - and the **location firewall's catch** (`ingest/emr/firewall.py`) — the Layer-2 guard that
    keeps a home address out of the health domain. It holds the fact and never commits it.

**Binding on every variant, and on the build:** no push notifications and no nagging badges. A passive
count is allowed — the constraint was about push and nagging, not in-app state, and the Tasks
unviewed-recognition pattern (`docs/reference/DESIGN.md` "Tasks — the result band") is the
shipped precedent. Nothing here may read as an unread-mail counter demanding zero.

## A — the binding mock (`a-inbox-holds.html`)

The Review tile survives on the launcher, unchanged, and keeps the badge it already renders.
The shipped `.review-segs` track is relabelled from `pending · decided` to **`notes · wiki`** —
same markup, same count pills.

- **Notes tab — a redirect.** Rows are ordered **oldest question first**, so the list drains
  from the top (9 days, 3 days, 2 days, 6 hours in the mock). Each row carries the note it came
  from, what is being asked, its age, how much the agent already committed, and a kind chip
  (`question` / `approval`). Tapping it opens the conversation. **There are no answer controls
  in the inbox.** A conversation still on its first pass is listed but *uncounted* — it is not
  waiting on you.
- **Wiki tab — decides.** The shipped list → detail with prev/next and the typed block
  registry, and the two verbs the producers actually file.
- **The signal.** The launcher tile badge only (`9` = 4 waiting + 5 findings), polled only
  while the launcher is on screen. Nothing counts on home or in the top bar.

The two tabs are deliberately **asymmetric — notes redirects, wiki decides** — and that
asymmetry *is* the ruling. Its cost: the nine-day-old question now takes two taps rather than
one, so the row has to stay informative enough to be worth the second. Build cost is small:
relabel the segmented filter, add an endpoint listing waiting questions and staged approvals,
route a row tap into the conversation, leave the block registry alone.

### The superseded variants, kept as the record

| | File | What it proposed | Why it lost |
|---|---|---|---|
| **B** | `b-waiting-bucket.html` | A fourth bucket on the chats picker (`Waiting · Today · Older · Archived`), the Review tile retired, the two tabs nested inside the bucket. Quietest of the three: no badge anywhere. | It has a host only if note conversations are listed in the chats picker at all — the sibling gate's call, not this one's to assume. It also retires the tile the ruling keeps as the "easy point of access", and at ~46px and 75% scale its rows cannot show a question at all. |
| **C** | `c-question-in-stream.html` | The question renders inline under its note in the home stream, answerable there; the inbox holds only what aged out of the two-day window. | Its premise is gone — the ruling makes the conversation the only place note ingestion is decided, and C's whole idea was answering outside it. Independently: the stream is a chat log (`useNotes.ts:287` ascending, `Stream.tsx:229-234` pins the bottom), so a question renders *below* its note and one on the newest note lands between that note and the composer. It also keys placement on the *note's* age, not the question's, so every backdated import misses the window entirely. |

Both are left exactly as they were reviewed, with a superseded banner. They are not maintained.

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
    (`Launcher.tsx:252-254`) — review items only. A's `9` (4 waiting + 5 findings) needs the
    count widened to include waiting questions and staged approvals.
    **And it has two sources, neither of which exists.** The notes list is a union of open
    `ask_owner` questions on note conversations *and* staged `prefs_write` Proposals (D17) —
    both W3 work. The endpoint has to return enough for a redirect row to be worth taking: the
    originating note, the question or proposal text, its age, and the committed count.
11. **An empty lane shows a `0` today.** `ReviewScreen.tsx:534` renders the count pill whenever
    the count is defined; only the tile badge gates on `> 0` (`Launcher.tsx:370-371`). The
    mocks hide the pill at zero. That is a small deliberate improvement, not existing
    behaviour.

*(Superseded-only: B's Waiting bucket carved out `SessionsPanel`'s `tab ?? first non-empty`
default at `SessionsPanel.tsx:273-281` so Chats kept landing on Today. Moot now that A is
chosen, recorded because it was a real behaviour change to a settled default.)*

## How they were built

Standalone, no build step, phone-viewport first, correct in both themes, on the
`docs/reference/DESIGN.md` token sheet with **no raw hex outside the token block**,
`prefers-reduced-motion` honoured.

- **Text size is the app's real default.** Each phone frame sets `--font-scale: 0.75`
  (DESIGN.md "Theming"), so every string inside a device is at the size the owner actually
  sees. Mock commentary lives *outside* the frames at 100%, and nothing inside a phone is
  anything but app copy.
- **Tap targets are ≥44px including padding** (DESIGN.md:127-128) — segments, rows, detail
  prev/next, back and close.
- **No accent-on-tint text.** Proposal buttons are the shipped `.rprop`: plain `--surface`,
  hairline border, label in `--text`. Where a control is emphasised it uses the
  `.review-seg.seg-active` recipe (`styles.css:6278-6282`) — tinted ground, `--text` label.
- **Everything advertised as interactive works.** In each mock both tabs switch, every wiki row
  opens a detail, and prev/next walks all five items with the ends disabled. In A, every notes
  row opens its conversation and the `correct it` composer opens.
- **The conversation is drawn thin on purpose.** A's destination view is a sketch, not a spec:
  what that surface looks like belongs to the sibling gate (`docs/mocks/agent-ingest/`). All
  this gate settles is that a notes row goes there and decides nothing itself.

## Two things still open

1. **Do the wiki cards get real verbs?** Today the answer to a contradiction is *dismiss* (which
   does not suppress) or *file a correction note* and wait for the next build. That is defensible
   under non-negotiable #7 — the wiki stays machine-written — but it is slow, and the mocks show
   how slow. Adding verbs is scope; so is fixing dismiss.
2. **Doc corrections ride along.** `docs/reference/DESIGN.md:899-953` has drifted from the code
   in two places, and both are corrected in the PR that builds this (`docs/DOC_LIFECYCLE.md`):
   - it describes a **three-lane** review inbox (`pending · deferred · decided`); only two
     lanes shipped, and this change replaces them with the two tabs;
   - `:930-935` describes **defer** and **talk it over** as "two universal escape hatches
     [that] sit in the footer", but `Footer.tsx` renders only `rfoot-correct` for the pending
     lane. The mocks show no defer and no talk-it-over because the code has none — the doc is
     what is stale, not the mock.

## Round history

Two things were tried and dropped during this gate. Recorded so they are not re-proposed:

- **A "Waiting on you" sheet raised from a steel dot in the top bar's status cluster** (the
  first variant C). Withdrawn: the owner had already ruled that cluster non-tappable and its
  8px dot deleted (`DESIGN.md:221-223`, `TopBarVitals.tsx:9-13`); the dot was bare colour with
  no paired text; and a five-finding sheet breaks the Sheet contract's single-primary-action
  rule (`DESIGN.md:1371-1382`).
- **Answer chips on the inbox row.** They survived one review and were briefly A's headline
  feature — one tap to drain the nine-day-old question, no navigation. The owner's ruling
  removed them: it would make the inbox a second place ingestion gets decided, which is the
  thing this design exists to delete. The one-tap drain was a real gain and it was traded away
  deliberately, for a single decision surface.
