# Tapping a packet card — disclosure research

> **Status:** Living · **Last verified:** 2026-09-03

Research dossier for `../plans/APRS_FILTERING_PLAN.md` F5 (packet detail). The disclosure
pattern for tapping a packet card, judged against the shipped screen and the live 5-second
poll. Feeds the three mocks in `../mocks/aprs/`.

Research for the APRS packet-detail GUI gate. Nothing under `/home/user/JBrain2` was modified.

Grounded in: `docs/reference/DESIGN.md`, `docs/reference/PROCESS.md`, `docs/mocks/aprs/e-stations.html`
(binding spec), `docs/mocks/aprs/README.md` (rounds 1–5), `frontend/src/components/AprsStations.tsx`,
`frontend/src/aprsStations.ts`, `backend/src/jbrain/sdr/classify.py`, `backend/src/jbrain/sdr/stations.py`,
`backend/src/jbrain/sdr/aprslog.py`, migrations `0180_aprs_packets.py` / `0185_aprs_derived.py`, and
200 real frames in `live_full_rows.json`.

---

## 0. Six facts from the codebase that constrain every answer below

These are load-bearing. Any mock that ignores them is mocking something that cannot be built as drawn.

**0.1 — There is a stable packet id, and the client has never been given it.**
`app.aprs_packets` has `id uuid PRIMARY KEY DEFAULT gen_random_uuid()` (migration 0180). But
`AprsStationPacket` (`frontend/src/aprsStations.ts:70`) carries only `heard_at | kind | gated | direct | text`,
and `AprsStations.tsx:341` keys rows on `` `${p.heard_at}-${i}-${p.text.slice(0,24)}` `` — **array index in the
key**. Every disclosure pattern except a modal sheet needs to remember *which row is open* across a 5-second
poll that replaces the whole array. Index-keyed state means the open row silently becomes a different packet
when one new frame arrives. **Returning `id` is a prerequisite for shapes A and B**, and it is one line in
`stations.py`.

**0.2 — The detail payload is already free.**
`stations.py:297` selects `heard_at, source, path, info, raw, kind, gated, heard_direct`, then
`stations.py:337-345` throws `source`, `path` and `raw` away, keeping only `classify(...).text`. Everything a
detail view needs to show the raw frame, the digipeater path, the AX.25 destination and the relay is *already
read out of Postgres on every poll and discarded*. No new query, no new index.

**0.3 — Nothing parses the fields yet.**
`classify.py` derives origin / relay / dti / kind / gated / direct / addressee / text, and reads a symbol
*code* only to spot weather-by-symbol (`_symbol_code`). There is **no** weather parse, no lat/lon parse, no
course/speed/altitude, no telemetry channels, no comment split. The detail view is not a rendering job on data
we have; it is a **new pure decode module** (`sdr/decode.py`) plus a renderer. `classify.py`'s own doctrine
tells you exactly how to write it: *"deliberately pure: bytes in, a struct out, no I/O … a CACHE over `raw`,
which is stored losslessly, so a better classifier can always be backfilled — nothing here has to be right the
first time to be safe."* Derive on read, like `text` already is; do not add columns.

**0.4 — Telemetry meaning lives in *other packets*.**
19 of the 200 frames are `T#nnn,...` telemetry, and they classify as **Other**, not as a kind of their own.
Four `:` messages from `N1KSC-1` in the same capture are its definitions:

```
:N1KSC-1  :PARM.Vin,Rx1h,Dg1h,Eff1h,A5,O1,O2,O3,O4,I1,I2,I3,I4
:N1KSC-1  :UNIT.Volt,Pkt,Pkt,Pcnt,None,On,On,On,On,Hi,Hi,Hi,Hi
:N1KSC-1  :EQNS.0,0.075,0,0,10,0,0,10,0,0,1,0,0,0,0
:N1KSC-1  :BITS.11111111,Telemetry test
```

So `T#110,190,088,011,068,000,00000000` is really *Vin 14.25 V · Rx1h 880 pkt · Dg1h 110 pkt · Eff1h 68 % · A5 0*.
That is the difference between a detail view that is useful and one that shows five numbers with no names. It
is also the one place the detail needs **cross-packet state** — a per-station lookup of the newest PARM/UNIT/EQNS.
This is a genuine scope decision to put in front of the owner, not a detail: without it, telemetry detail is
"channel 1 = 190" and the answer to "what does that mean" is *the station has not said*.

**0.5 — The 5-second poll is the whole hazard, and packets are immutable.**
`RadioScreen.tsx:38` `POLL_MS = 5000`; the tick drives `AprsStations.load()`, which replaces `detail` wholesale.
Two consequences pull in opposite directions:
- *Bad:* new frames **prepend** (`ORDER BY heard_at DESC`), so an open row slides down the screen under the
  thumb by one row-height per arrival. On the measured channel that is ~123 pkt/hr overall but bursty — the
  capture has four `KD4WLE` object beacons landing in the same second.
- *Good:* **a heard packet never changes.** Unlike the Ops per-service log tail (which expands into live,
  growing content), an expanded packet's content is a historical fact. The expanded body never needs to
  re-render at all. That is a much stronger argument for inline expansion here than the Ops precedent gives.

**0.6 — DESIGN.md has already answered the general question.**
The surface-paradigm table (`DESIGN.md`, "Surface paradigms"):

| Job | Paradigm |
|---|---|
| Row-level detail that doesn't warrant navigation | **Inline expansion within the list** |
| Contextual quick forms & actions | Bottom sheet |
| Primary tasks (capture, reading an article, chat) | Full screen with top-bar back chevron |

A mock proposing a full-screen push for a 60-byte frame is arguing against a settled rule and must say so out
loud. There is also in-house precedent for exactly this interaction: the Analysis tab's **Sources card image
rows "carry a disclosure caret and unfold in place"** (settled by review, variant B), and `OpsCard` /
`SessionsPanel` / `markdown.tsx` all already ship `aria-expanded` disclosure buttons.

---

## 1. The disclosure patterns, and how each survives a live refresh

Scored against: scroll position under a 5 s poll, back-button semantics, animation cost, screen-reader
behaviour, and one-handed reach.

### 1a. In-place accordion (row grows a panel beneath its summary)

- **Scroll under refresh:** *the weak point.* The open row is pushed down as frames prepend. Three mitigations,
  in increasing order of honesty: (i) `overflow-anchor: auto` — free, but **Safari/iOS has never shipped it**,
  so on an iPhone-installed PWA it does nothing; (ii) measure-and-restore `scrollTop` after each poll; (iii)
  **buffer arrivals while a row is open** and surface them as a `3 new packets` pill that the owner taps to let
  them in. (iii) is the only one that is *deterministic*, and it has direct house precedent — the vitals screen's
  capped lists with a "Show all N" footer row, and the Runs screen's honest count line. It also states the truth
  rather than hiding it. Note the poll itself must keep running: freeze the *rendering*, never the *fetch*, or
  the health line above ("heard 12s ago") starts lying.
- **Back semantics:** none — and that is a feature. An accordion is not a navigation level, so it does not enter
  `backLayers.ts`. Back still climbs station → roster, which is what the owner already learned. Cost: an open row
  is lost on back, and there is no way to deep-link one.
- **Animation:** height animation on a variable-height panel is the classic jank source. Do not animate height;
  the house rule is 150 ms ease-out and reduced-motion off. Simplest correct answer: no height transition at all,
  just mount the panel — a packet decode is small and instant, and `DESIGN.md`'s reduced-motion rule means you
  need the no-animation path to be correct anyway.
- **Screen reader:** the WAI disclosure pattern, and the best-understood one there is: `<button aria-expanded
  aria-controls>` on the header, focus **stays on the trigger**. The trap is 0.1 — if the poll re-keys the row,
  React unmounts the focused button and focus falls to `<body>`, dumping a screen-reader user back at the top of
  the screen every 5 seconds. Stable `key={p.id}` is an accessibility requirement here, not a perf nicety.
- **Reach:** best of all options. The row you tapped is where the answer appears; nothing moves the surface.
- **Right answer when:** detail is short, comparison between rows matters, and you want several open at once.

### 1b. Bottom sheet (shared `<Sheet>`)

- **Scroll under refresh:** *immune, and by construction.* The sheet holds a snapshot of the packet object; the
  list churns behind the scrim, the scroll position is preserved by the body-scroll lock
  (`Sheet.tsx` sets `document.body.style.overflow = "hidden"` and restores it). This is the single strongest
  property any option has, and on a 5 s-polling screen it deserves real weight.
- **Back semantics:** already solved — `Sheet.tsx` calls `useBackLayer(onClose)`, so the platform back gesture
  and swipe-down close the sheet before the screen. Free, correct, consistent.
- **Animation:** shared shell, already tuned, already reduced-motion aware.
- **Screen reader:** focus trap, `panelRef.current?.focus()` on open, Escape dismiss. NN/g's caution applies:
  ship a visible close control, not only the grab handle. `Sheet` gives a handle and a title but callers add
  their own actions; check that a packet sheet has a tappable exit (DESIGN: *"Every overlay surface must have a
  visible, tappable exit; a gesture is never the only way out"*).
- **Reach:** good — content rises into the bottom half.
- **Costs:** it is a **modal over a list you were reading**; you cannot compare two packets; it burns a
  back-stack level so back no longer means "leave this station" while it is open; and DESIGN's paradigm table
  files sheets under *quick forms and actions*, not *row-level detail*. Also "one modal at a time, never nested"
  means the raw-frame view has to be inside the sheet, not a sheet on a sheet.
- **Right answer when:** the detail is long, has its own actions, or the list underneath is volatile. The last
  clause is exactly our case, which is why this is a serious contender rather than a straw man.

### 1c. Full-screen push (a fourth level: Radio → APRS → station → packet)

- **Scroll:** immune (the list is unmounted), but returning re-mounts the list at the top unless scroll is
  restored explicitly — worse than the sheet, which never left.
- **Back:** correct and familiar (`subscreen` layer + chevron + swipe-down + `useBackGesture`), but it is a
  **fourth level of a tree** whose root is already three deep. DESIGN: *"card screen → launcher → home"*, and
  APRS is already Radio → APRS tab → station.
- **Screen reader / reach:** fine.
- **Cost:** wildly disproportionate. A packet is 30–160 bytes; the largest real frame in the capture is 163
  characters. A whole screen for that, plus a navigation level, plus a re-entry animation, to read one weather
  report — and you have to come back and find your place to read the next one, which on a beaconing channel is
  the *normal* activity. This is what DESIGN's paradigm table exists to prevent.
- **Right answer when:** the detail has its own sub-navigation (tabs, a map, a chart). It does not.

### 1d. Card flip (front/back on one footprint)

- **Scroll:** the footprint *changes* anyway — a status packet's back is one line, a weather packet's is twelve
  fields — so the flip does not actually hold layout still. It only *looks* like it should.
- **Back / SR:** a 3-D flip is either two elements swapped (fine, announce via `aria-expanded`) or a real
  `transform: rotateY` (a compositor cost, meaningless under reduced motion, and mid-flip the back face is
  readable by AT while invisible unless you manage `hidden`/`inert` carefully).
- **Cost:** the front face disappears. On this screen the front face is the *evidence* — the raw comment text
  the owner tapped because it caught their eye — and hiding it to explain it is backwards. Also DESIGN forbids
  the visual register a flip usually asks for ("no gradients, no glass, no shadows heavier than a hairline").
- **Right answer when:** front and back are the same information in two representations of equal size (a card
  and its notes). Not here.
- **However** — and this matters — the *non-3-D* version of this (the summary line is **replaced** by the plain
  reading, same card, no growth below) is a genuinely different and defensible idea, and it is the closest read
  of the owner's literal words. That is Shape B in §6.

### 1e. Side-by-side / master-detail on wide screens

- Not a phone answer, but the responsive escape hatch for the tablet/desktop case: at ≥ 900 px the station
  detail becomes list-left / packet-right, and the tapped row is selected rather than expanded. Immune to the
  poll (the detail pane holds a snapshot keyed by id), and it composes with **any** of A/B/C as their wide-screen
  form. Worth one line in whichever mock wins; not worth a mock of its own — this app is phone-first and the
  owner uses it one-handed.

### 1f. The option that removes the interaction: a list-level Plain/Raw switch

Rather than per-row disclosure, a screen-level segmented control rewrites *every* row into its human reading.
Perfectly immune to the poll (no per-row state at all), zero a11y surface, and it is a control the house already
has (`.seg-tabs`). It fails the owner's actual request — he asked for touch on a card — and it destroys the
density that makes a 200-row feed scannable (a weather packet's plain form is four lines). Recorded as
considered-and-rejected; its good idea (that *most* rows could read better even collapsed) survives as the
"rewrite the summary line" refinement in §6.

---

## 2. Heterogeneous types: one shell, a closed registry of readings

**Recommendation: one shell, type-specific content — not bespoke layouts, and not a single generic field list.**
Both extremes are wrong for reasons visible in the real data.

*Why not bespoke per type:* APRS defines ~25 data-type identifiers, and `classify.py` already argues the case
for collapsing them ("a phone filter with fifteen equal chips is worse than one with five that matter"). Twelve
bespoke layouts is twelve things to keep in step, and the 200-frame capture already contains a type the five
buckets do not name (telemetry, filed as Other, 19 frames — ~10 % of the channel).

*Why not one generic field list:* a weather packet rendered as `t: 078 · r: 000 · p: 000 · P: 000 · h: 94 ·
b: 10135` is the raw frame with extra steps. The whole ask is "human readable".

### The shell (identical for every packet, always in this order)

1. **Attribution line** — who, when, how it reached us. The app's sentence, the packet's callsign.
2. **The reading** — the type-specific block (below).
3. **Raw frame** — collapsed, last (§3).

The shell is what makes the surface learnable and what carries the untrusted-content rule uniformly (§4). This
mirrors a doctrine the codebase already has: DESIGN's **agent tool views** are *"a closed registry of
first-party components … an unknown name renders nothing"*, with the data payload naming a registered renderer
and the component owning the token mapping. Same shape, same safety argument, and the fallback rule is the same:
**an unrecognised type degrades to the shell**, which is still a complete, honest surface.

### Three tiers of reading

**Tier 1 — earns a bespoke block** (fields have units and relationships):

- **Weather** (24/200). Headline stats first, in the register `stat_block` uses: **78 °F · 0 mph · 30.05 inHg ·
  94 %**. Then the rest as a compact 2-column field grid. A **small wind arrow** rotated by the reported bearing
  is legitimate under DESIGN — a glance aid always paired with its text value ("338° NNW"), exactly the rule
  meters live under ("the bar is a glance aid, never the only encoding"). What is *not* legitimate is a
  temperature colour ramp: colour is information (state/domain), never decoration, and a temperature hue would
  be colour carrying a value nothing else carries.
  Two parse traps in the real capture: (i) `_09030625c346s000g000t078r000p000P000h94b10135tU2k` — the trailing
  `tU2k` is the software/unit identifier, **not** a second temperature; a naive `t` scan reports 2 °F. (ii) `s`
  means *sustained wind speed* immediately after `c`, and *snowfall* elsewhere — positional, not by letter.
  Both are `sdr/decode.py` concerns, but they are why the mock must show a *decoded* value next to a raw frame
  the owner can check.
- **Position** (63/200). Coordinates in both DMS-ish APRS form and decimal, the **symbol** (table + code →
  a local glyph table, never a fetched icon), course/speed/altitude when present (`=2828.47N/08048.25W$360/033/A=000049`
  is a moving station at 33 kt, 49 ft), and the comment as the packet's own sentence. `A=00000` is a *reported*
  altitude of zero, which is different from absent — render it, don't hide it.
- **Telemetry** (19/200, currently "Other"). Five analogue channels + 8 bits. With the station's PARM/UNIT/EQNS
  (0.4): named, scaled, united. Without: `ch1 190 · ch2 088 …` and a plain line — *"K4KSC-12 has not published
  what these channels mean."* That sentence is the feature. Silence here reads as a broken decoder.

**Tier 2 — almost no layout, because there is nothing to lay out:**

- **Status / beacon** (`>Powered by WPSD (https://wpsd.radio)`, `BPQ Node Stack/iGate/Chat/Full Service BBS…`):
  the sentence, quoted, and nothing else. A field list with one row labelled "Status text" is chrome around a
  sentence. This is the case that proves the shell has to be able to get out of the way.
- **Message**: addressee (the one structured field, already parsed in `classify.py::_addressee`), then the text.
  A message *to the owner* deserves emphasis — the round-5 README's whole IGate trap is about the owner's mail
  arriving wrapped as third-party.
- **Object / Item** (79/200 — the *largest* bucket): the object's **name** (`FLMesh-2`, `442.850`), live/killed
  flag, its position, and its comment. Objects are the one type where the interesting thing is that the *station
  is describing something that is not itself*, and the shell's attribution line has to say so or the reading is
  wrong: *KD4WLE reports an object called `442.850`*.

**Tier 3 — the shell alone:** unknown / unparsed. Attribution + the info field verbatim in monospace + raw. The
copy must say *we do not know what this is*, not render an empty grid.

### Two rules that fall out of the real data

- **Zeroes are content, blanks are not.** `r000p000P000` means *it has not rained*, and that is worth one line
  ("no rain in 24 h") rather than three zero rows. Absent fields are simply not drawn — never a `—` placeholder
  in a field grid, which reads as a decode failure.
- **Repeats are the dominant reading.** `KD4WLE`'s `442.850` object is byte-identical every five minutes, and 79
  of 200 frames are objects. The detail block should be able to say *"identical to the previous 11 from this
  station"*, which is more informative than any single field on it. (Cheap: compare `info` to the previous row
  for that origin, client-side, over the array already in hand.)

---

## 3. The raw frame

**Where:** inside the detail, **last**, **collapsed behind its own toggle**, monospace.

The house has settled this exact question twice: the vitals detail puts raw output last *"so it can run as long
as it likes"*, and the verbatim prompt is *"collapsed by default (the largest thing on the surface)"*. Follow it.

**What it contains** — and this is the part that earns its place rather than being a curiosity:

- The reconstructed TNC-2 line: `SRC>DEST,path:info`. `destination` and `path` are columns nobody has ever
  shown; the destination is how a ham identifies the *software* (`APMI03`, `APBPQ1`, `APCHP0`, `APDG02`, `APIN22`
  in this capture), and the path with its `*` markers is the evidence behind the `direct`/`rf` badge.
- For a wrapped frame, **both layers**: the wrapper as received and the inner frame. This is the only place the
  gating story becomes checkable — the row shows only the inner `text` by design (`stations.py:341-345`,
  "showing the stored frame would print the transport on every line"), so the raw disclosure is where "gated via
  N4TDX" stops being an assertion and becomes something the owner can verify.
- **Hex**, on the same toggle via a small Text/Hex switch. `raw` is stored as hex already
  (`82a09a926066609c…`) and is the *only* lossless copy: `_clean()` in `aprslog.py:293` strips control characters
  from `info` on the way in, and `dti_from_raw` exists precisely because two legitimate Mic-E identifiers are
  control bytes that the scrub deletes. aprs.fi does the same thing for the same reason (a Normal/Hex mode
  selector, "revealing difference in whitespace"), and its TNC view added tap-to-decode — the same direction of
  travel, from the other end.
- A **Copy** button (house precedent: Ops per-service "Copy logs", reads "Copied" for 2 s). This is the action a
  ham actually wants — paste the frame into a forum post or a bug report.

**What it must not be:** a peer of the reading. Not a second tab, not side-by-side, not on the collapsed row.
One nested toggle, maximum — deeper nesting inside a disclosure inside a list row is the "modal over a modal"
smell one layer down.

---

## 4. Untrusted content in a detail view

The invariant is already written into the code: `AprsStations.tsx:16-18` — *"EVERY STRING RENDERED HERE CAME OFF
THE AIR from anyone with a transmitter, callsigns included"* — and `classify.py` — *"a callsign is plain bytes
that forge trivially… it never authenticates."* A detail view is where this gets harder, because a detail view's
job is to *interpret*, and interpretation is the app speaking.

**The one rule that makes it readable rather than nagging: typography carries the trust boundary.**
The app's words in the system font; every byte that came off the air in `--font-mono`. The roster already does
this by accident (`.aprs-st-call` is mono, section headers are not); make it explicit and total. So a field row
is *label (system font, `--text-2`)* : *value (mono, `--text`)*, and a comment is mono at body size. The owner
learns one rule and never has to be told again — which is the alternative to badging every string, and badging
every string is how a surface becomes unreadable.

**The attribution line frames receipt, not identity.** The row's 3-letter badge (`direct`/`gated`/`rf`) is right
for a row and too terse for a detail. Promote it to a sentence that names the relay:

> Heard 4 min ago · `KD4WLE` · relayed onto RF by `N4TDX` from the internet

...never *"from KD4WLE"* as bare fact. `direct` deserves the strongest wording available, because it is the one
provenance claim the box can actually make from its own receiver: *nothing repeated this — we heard the
transmitter*. `classify.py` is careful that a wrapped frame is never `direct`; the copy should be equally
careful.

**Derived values are the app's reading of the packet's bytes, and must be traceable.** Say *"reports 78 °F"*,
not *"the temperature is 78 °F"*. That is also the argument for the raw frame living one tap away rather than on
another screen: an interpretation the owner cannot check is the app's voice wearing the packet's clothes.

**Hard prohibitions:**

- **Never linkify.** The real capture contains `https://wpsd.radio`, `https://n4tdx.org`, `AmbientCWOP.com` and
  `seanhaga@kd4wle.net` — all from anonymous transmitters. An `<a>` here is a phishing surface the app minted
  on a stranger's behalf, and it is the shape of DESIGN's I-9 exfiltration rule. Text is selectable; that is
  enough.
- **No packet-driven resource fetch** — no map tile centred on a packet coordinate that hits a third party, no
  symbol icon fetched by code. Symbol table + code → a fixed local glyph map; unknown code renders the two raw
  characters, not a guessed icon.
- **Nothing here becomes a prompt.** The plan's two trust tiers, and round 1's *"a packet becoming an LLM prompt
  is prompt injection with an antenna"*. The trap a detail view invites is an "ask the agent about this packet"
  button. If that is ever wanted it is a separate, escalation-worthy decision — it does not slip in as a
  convenience on a detail card.
- **Isolate bidi.** `_clean()` (`aprslog.py:293`) keeps everything `>= " "`, so U+202E and friends survive if the
  sidecar decodes UTF-8. In a mono column of callsigns and app labels, one override character reorders the line.
  Cheap fix, and it belongs in the mock's CSS so it is not forgotten: `unicode-bidi: isolate` (or `plaintext`)
  on every off-air string. Worth verifying the sidecar's decode before deciding whether to also strip them
  server-side.
- **Bound the length.** The longest real frame is 163 chars, but nothing stops a transmitter sending a
  kilobyte. The detail may show it whole (it is the point), but it must not be able to push the raw toggle off
  the screen forever — clamp with a "show all" the way the OCR inset does (*"clamped ~6 lines, 'show all N
  lines' grows in place"*).

---

## 5. Accessibility

- **Roles.** Header = `<button type="button" aria-expanded aria-controls="pkt-<id>">`; panel = a plain `<div
  id="pkt-<id>">`. Do **not** give 200 rows `role="region"` (200 landmarks). Do **not** reach for
  `role="tab"`/`tablist` — `WindowTabs` (`AprsStations.tsx:369-371`) already documents why: *"a nested tablist
  makes a screen reader announce a filter as a tab. `aria-pressed` says what it actually is."*
- **Focus stays on the trigger** on expand and collapse (WAI disclosure pattern). The failure mode unique to
  this screen: if the 5 s poll re-keys rows (fact 0.1), the focused button unmounts and focus falls to `<body>`
  — a screen-reader user is thrown to the top of the screen every five seconds. **Stable `key={p.id}` is an
  accessibility fix here, not a performance one.**
- **The list must not be a live region.** DESIGN settled the identical question for the vitals chart: it stopped
  being an `<output>` when it became tappable, because *"a live region that is also a control would announce the
  whole reading on every 1 Hz tick"*. A 5 s-polled packet list announcing itself is unusable. Instead: one
  `aria-live="polite"` status line carrying only the buffered-arrivals count (`3 new packets`) — which the
  accordion needs anyway (§1a), and which is silent when nothing arrives.
- **Tap targets.** DESIGN: ≥ 44×44 px, compact rows may reduce to 36 px height *"but never shrink tap areas below
  44px including padding"*. `.aprs-row` is `padding: 11px 12px` around 13 px/1.45 text ≈ 40 px for a one-line
  packet — **under the floor today**, and it becomes a control under every shape here. The nested raw toggle and
  Copy need the same. Existing `.aprs-chip` (5 px padding, 11.5 px text ≈ 27 px) is worth re-measuring while the
  screen is open anyway.
- **Text size.** Every token is `calc(px × var(--font-scale))` and the scale runs 65–100 %. A 12-field weather
  grid must be `repeat(auto-fit, minmax(…))`, never a fixed 4-up — at 100 % scale a fixed grid overflows and the
  page scrolls horizontally, which the mock discipline explicitly drives for.
- **Motion.** No height animation (§1a). Whatever transition ships must be dead under
  `prefers-reduced-motion: reduce`, which every mock in `docs/mocks/aprs/` already handles with a blanket rule.
- **Contrast.** Values at `--text`, labels at `--text-2`; never a value at `--text-3` (muted is for placeholders
  and disabled). The provenance badge keeps its tint+text pairing — status colour is never the only encoding.
- **Exit.** For the sheet shape: a visible close control, not only the grab handle (DESIGN's overlay rule, and
  NN/g's first bottom-sheet guideline).

---

## 6. Three shapes worth mocking

Genuinely different mechanisms — not three skins. All three assume the §0 prerequisites (`id` on the payload,
`sdr/decode.py`, the raw/path/destination fields that are already being fetched and discarded).

### A — **Decode in place** (inline accordion)

- **Interaction.** Tap a packet row → it grows a decoded panel beneath its existing summary line; the summary
  stays put as the anchor. Chevron rotates. **Single-open** (opening one closes the last) — with 200 rows,
  multi-open turns the list into an unnavigable ribbon, and single-open makes "scroll the newly opened panel
  into view" well defined. Tap the header again to close. Raw frame is a second, nested toggle at the panel's
  foot.
- **Layout.** Row unchanged → hairline rule → attribution sentence → the type's reading → `raw frame ›`.
- **Live refresh.** While a row is open, arrivals are buffered behind a `N new packets` pill; the poll keeps
  running (the health line stays honest), only the rendering is held. Tapping the pill, or closing the row,
  lets them in.
- **Good at.** The house answer to this exact job (DESIGN's paradigm table). No navigation level, no modal, no
  new shell. The tapped thing stays where the thumb is. Comparing a packet with its neighbours is trivial.
  Composes with the wide-screen master/detail (§1e) with no rework.
- **Gives up.** The list reflows under the reader — the one thing it has to defend against, and the pill is a
  visible admission of the problem. Long decodes (a full weather grid) push the *next* rows far away. No
  deep-link. Cannot see two packets at once.

### B — **Turn the card over** (in-place replacement, the owner's literal words)

- **Interaction.** Tap → the row's own content is **replaced** by the plain reading on the same card; the raw
  comment line it was showing is superseded, not pushed down. Tap again → back to the frame line. A `raw ›`
  toggle inside restores the frame text in place. No new surface at all: *"weather when clicked should change
  to show all the weather detail."*
- **Layout.** One card, two faces. Face 1 = badge + text + kind + age (today's row). Face 2 = attribution
  sentence + reading + raw toggle. Same padding, same border, same position in the list.
- **Live refresh.** Same prepend hazard as A, and slightly worse reflow (a status packet's back face is *shorter*
  than its front, a weather packet's much taller, so the list breathes in both directions). Same pill mitigation.
- **Good at.** It is exactly what was asked for, and there is a real idea underneath the literalism: on this
  channel the *collapsed* row is already unreadable — `` `m3jq6F>/`On D-Star … `` or
  `T#110,190,088,011,068,000,00000000` tells the owner nothing. B's shape generalises to *the card's default
  face could be the plain reading*, with tap revealing the frame. That inverts the whole surface, and it is a
  genuine question the gate should put to the owner rather than answer for him.
- **Gives up.** The evidence disappears on tap — you tapped *that string* and now it is gone, which hurts most
  for the types where the string was already readable (a status line, a message). Harder to check a decode
  against its source without a third state. And it is a novel disclosure idiom in an app whose disclosures are
  all carets-and-panels, so it costs a paradigm.

### C — **Packet sheet** (the shared `<Sheet>`)

- **Interaction.** Tap → the shared bottom sheet rises with the whole packet: attribution, reading, raw, Copy.
  Swipe-down / back / scrim / close button dismiss. The list is untouched behind the scrim.
- **Layout.** Sheet title = the callsign + kind; body = the same shell as A and B; a single primary action
  (Copy raw), per the sheet rules.
- **Live refresh.** **Immune, by construction** — the sheet holds a snapshot, the body-scroll lock holds the
  position, and the list can churn as hard as it likes. No pill, no buffering, no scroll-anchor hack, no
  `overflow-anchor` Safari gap. This is the only shape where the 5 s poll costs nothing at all.
- **Good at.** Everything is already built and already correct: focus trap, scroll lock, back-layer
  registration, Escape/swipe-down, safe-area padding, reduced motion. Unlimited room, so telemetry with
  definitions or a 1 kB comment fits without deforming the list. Room for actions later (mute this station, copy,
  show on a map) that an accordion has nowhere to put.
- **Gives up.** It is a modal on a *reading* surface — DESIGN files sheets under "contextual quick forms and
  actions", and NN/g warns against sheets replacing page-to-page flows. One at a time; no comparison. It burns a
  back-stack level, so while it is open, back no longer means "leave this station". And a ham skimming twenty
  frames does twenty open/close cycles where A does twenty taps.

### Ranking, and what I would build

**A > C > B.**

**Build A**, with one refinement borrowed from B and one from C:

- from **B**: when a row expands, its *summary line* is rewritten to the one-line plain reading
  (`78 °F · calm · 30.05 inHg` instead of `@031030z2837.27N/08049.42W_338/000g000t078r000…`), with the raw
  string still reachable in the panel. That answers "the card changes" honestly without losing the evidence.
- from **C**: the arrivals pill, which is the only deterministic fix for the poll and is worth having on screen
  even when nothing is open.

The case for A over C: DESIGN's paradigm table already decides it, the house already ships this exact idiom
(the Sources card's image rows "unfold in place"), it costs no navigation level, and the strongest argument
against inline expansion — *"the content behind the disclosure is live and keeps changing"* — **does not apply
here**, because a heard packet is immutable (0.5). C's immunity to the poll is real, but it is immunity bought
by covering the list; A buys most of it with a pill and keeps the list.

The case for C, if the owner disagrees: he reads this screen one-handed while the channel is busy, and A's
answer to a busy channel is to stop the list from updating. That is a real cost, honestly stated, and it is
exactly the kind of trade the GUI gate exists to put in front of him rather than settle by argument.

B ranks last on merit but must be mocked, because it is the literal reading of the request and because its
underlying question — *should the collapsed row be plain rather than raw?* — is the most interesting one in this
round, and it is invisible unless someone draws it.

### What the three mocks must show (per DESIGN's mock discipline: realistic, varied, driven)

Real frames only, from `live_full_rows.json`, and all of these states:

- **weather** with the full field set; **status** (one sentence, the shell getting out of the way);
  **telemetry** in *both* forms — named via PARM/UNIT, and unnamed with the "the station has not said" line;
  **object** with a name and a long wrapping comment; **Mic-E** (`` `m3jq6F>/` ``) as the unreadable-collapsed
  case; and an **Other/unparsed** frame (`<IGATE,MSG_CNT=106,LOC_CNT=85`) degrading to the shell.
- a **gated** frame with both layers of raw visible; a **direct** frame; an **rf** frame.
- the row open while **new packets arrive** — this is the discriminator between the three and must be
  demonstrated by the control, not asserted in a hint (the lesson round 3's rebuild paid for).
- a repeated object (*"identical to the previous 11"*), and a comment containing a URL rendered as **plain
  text**.
- 44 px targets checked; no horizontal scroll at `--font-scale: 1`; driven in Chromium before it is offered.

---

## Sources

Prior art and standards consulted:

- [aprs.fi raw packets guide](https://aprs.fi/doc/guide/aprsfi-raw-packets.html) — Normal/Hex mode selector,
  invalid packets shown in red with a plain-English error; tap a raw packet in the TNC view to decode it and show
  the decoded fields (403 to direct fetch; read via search summary).
- [aprs.fi user guide](https://aprs.fi/doc/guide/guide.html) and [changelog](https://aprs.fi/page/changelog)
- [YAAC — View Raw Network Packets](https://www.ka2ddo.org/ka2ddo/YAACdocs/viewrawpackets.html) — raw AX.25
  frames in a separate tabular window, distinct from the station views
- [APRSdroid](https://aprsdroid.org/) and its [FAQ wiki](https://github.com/ge0rg/aprsdroid/wiki/Frequently-Asked-Questions)
  — full raw packet content (header + data) exposed per packet type
- [NN/g — Bottom Sheets: Definition and UX Guidelines](https://www.nngroup.com/articles/bottom-sheet/) — support
  back dismissal, include a visible close button, never stack, avoid replacing page-to-page flows
- [Material Design 3 — Bottom sheets](https://m3.material.io/components/bottom-sheets/guidelines)
- [MDN — Overview of scroll anchoring](https://developer.mozilla.org/en-US/docs/Web/CSS/Guides/Scroll_anchoring/Overview)
  and [caniuse: overflow-anchor](https://caniuse.com/css-overflow-anchor) — not supported in Safari/iOS
- [MDN — aria-expanded](https://developer.mozilla.org/en-US/docs/Web/Accessibility/ARIA/Reference/Attributes/aria-expanded)
  and [W3C ARIA Practices](https://wai-aria-practices.netlify.app/aria-practices/) — disclosure pattern, focus
  stays on the trigger
- [MUI X master-detail row panels](https://mui.com/x/react-data-grid/master-detail/) — the master/detail idiom
  for wide screens
- [Apache Juneau dashboard auto-refresh pause](http://www.mail-archive.com/commits@juneau.apache.org/msg06268.html)
  — precedent for suspending a polled table while an expanded detail row is open
- [APRS weather specification comments](https://www.aprs.org/aprs11/spec-wx.txt) and
  [Using APRS in Weather and SKYWARN Applications](https://www.aprs.org/APRS-docs/WX.TXT) — the `c/s/g/t/r/p/P/h/b/L`
  field set and units
