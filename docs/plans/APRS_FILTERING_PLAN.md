# APRS filtering — a station roster, not a packet firehose

> **Status:** Shipped, with the F5 row shape reopened for round 6 · **Last verified:** 2026-09-03 · **Waves:** F1✅(classifier + derived columns) F2✅(roster + station detail API) F3✅(the stations screen) F4✅(`aprs_recent` v2 + signal level) F5✅(what a packet SAYS — shape D: human readable first). The GUI gate is **closed** — `../mocks/aprs/e-stations.html`, chosen from `d-filtering.html`'s three shapes, is the binding spec.

`APRS_CONTROL_PLAN.md` P1 shipped a heard log and it works: the box has been
recording since it came up. This plan is about the log being *readable* — filtering by
who sent something, what kind of thing it was, and how recently, plus keeping every
frame durable enough that jerv can be pointed at the whole archive later.

## The measurement that reframed it

Written from a live capture rather than the APRS spec, and the difference was the whole
design. **90 minutes on 144.390 near Titusville: 184 frames.**

| What the table said | What was true |
|---|---|
| 5 values in `source` | **15 stations actually transmitting** |
| — | 130 of 184 frames (**71%**) came from the internet, not the air |
| — | kinds: Object 83 · Position 40 · Other 34 · Weather 20 · Message 7 |

Three quarters of the log was one IGate (N4TDX) relaying APRS-IS traffic onto RF as
third-party frames — and for every one of those, the AX.25 source names the **relay**,
not the sender. So the obvious build ("group by `source`, filter on `info[0]`") is
wrong for most of the table *while looking like it works*: it shows five stations
instead of fifteen, files 71% of the log under one machine's name, and types every
relayed position, object and message as the same meaningless `}` bucket.

That is F1's entire justification, and it is why F1 comes first: F2, F3 and F4 all read
columns that do not exist until it lands.

## The shape (GUI gate closed)

Three interactive shapes were built on 60 real packets (`../mocks/aprs/d-filtering.html`)
and the chosen one rebuilt on all 184 (`e-stations.html`). The owner's decision:

- **Stations first, most recent first.** The roster is the screen — a list of who has
  been heard, newest activity at the top, not a scrolling packet feed.
- **Recency at the root:** one day / 3 days / 1 week / older.
- **Kind chips at the root too, filtering the ROSTER** — "show me the stations that
  have sent positions", not "show me position packets".
- **Inside a station:** the same kind and recency filters, now over that station's own
  traffic.
- The owner's callsign is **app-wide Settings**, not an APRS-page field (shipped).

## Waves

**F1 ✅ — the classifier and the derived columns.** `sdr/classify.py`: pure, total,
`(source, info, path, raw) → Heard`. Unwraps third-party frames to the true sender,
types the frame by what is *inside* the wrapper, separates *gated* (third-party AND
carrying TCPIP/TCPXX) from merely wrapped, reads the message addressee as a fixed
nine-character field, and folds ~25 APRS data-type identifiers into five buckets a
person would actually filter on. Migration `0185` adds the results as six nullable
columns plus a `(origin_call, heard_at DESC)` index.

Every derived column is a **cache over `raw`**, which is stored losslessly. That is the
load-bearing property: a classifier bug costs a re-run and never a row. The self-healing
sweep (`run_aprs_backfill_loop`, its own loop because the drain stays attached for as
long as the owner is logging) claims `kind IS NULL` rows through a partial index that is
empty once the table is derived — so it is free in the steady state, it brings the
already-recorded backlog forward with no terminal step (CLAUDE.md rule 10), and it is
how a *better* classifier gets applied to history later.

**F2 ✅ — roster + station detail API.** `sdr/stations.py` + `GET /api/sdr/stations`
and `/stations/{call}`. Server-side aggregation on the Runs-log precedent: query params,
clamped limits, the grouping and the window in Postgres rather than in Python — a year of
this channel is ~1.2M rows and the roster is fifteen lines.

The chips are a `HAVING bool_or(...)`, not a `WHERE`: they narrow *which stations are
listed*, and `kind_stations` counts stations, because a chip reading 27 beside a list of
three would be lying about what pressing it does. Both count sets are computed over the
window **unfiltered by the selection**, so the chip row does not rearrange itself as you
use it. Every response carries `unclassified` — rows in the window the sweep has not
reached — so a roster that is still filling in says so instead of quietly showing fewer
stations, the same rule this screen already follows for a dead receiver.

**Verified against the live box**, not only fixtures: 200 real packets pulled through the
debug API and run through the real reader in real Postgres — **6 apparent stations in
`source`, 16 in the roster**, nothing unclassified, and every chip count agreeing with the
list it produces.

*Two deliberate deviations from the mock, both because the mock contradicts itself.* Its
detail view matches on the base callsign while its roster groups on the full one, so
tapping `N1MPR-C` (5 pkt) would list `N1MPR-S`'s traffic too and disagree with the count
just tapped — the API matches the exact `origin_call`. And the mock's time tabs inside a
station show the whole band's packet counts; the API scopes them to that station.

**F3 ✅ — the stations screen.** `components/AprsStations.tsx` + `aprsStations.ts`,
replacing the flat packet feed the APRS tab shipped with. The roster is the screen;
tapping a station opens its own traffic with the same two controls narrowing it.

It follows the tab's existing poll rather than owning a second timer, so the station list
and the health line above it can never be reading the channel at two different moments.
The owner's callsign comes from app Settings — his stations pin to the top and tint,
which is what removes round 4's central trap: **his own mail arrives wrapped**, because an
IGate relays a message to RF only once the addressee has been heard nearby, so filed under
the relay it reads as somebody else's noise. Filed under the true sender it is simply him.

Leaving a station drops the chip selection deliberately: inside a station "Weather" means
*this station's weather*, and at the roster it means *stations that send weather at all*.
Carrying a selection across that change would silently rewrite what was asked for.

**F4 ✅ — `aprs_recent` v2 and signal level.** The tool gains station/kind/since/until/
summarize (tool `version` bump + digest re-pin at `tests/unit/test_agent_readtools.py`),
keeping its `<untrusted_external_data source="heard-over-the-air">` wrapper — the two
trust tiers are unchanged, and a station roster does not make a callsign an identity.
Signal level: an **earlier claim that it was unrecoverable was wrong.** We ship direwolf
with `-q hd`, and `h` is precisely "suppress the Heard line with the audio level".
Measured with `-q d`: `N0CALL-9 audio level = 50(14/14)   _||||||__`. It is a flag
choice and a parse, not an SDR limitation.

## What the independent review found

F1 was reviewed by someone who did not build it (`PROCESS.md`: the reviewer is never the
builder), and it found four real defects. Recorded because each one is a lesson about
where this kind of code goes wrong, not just a bug that got fixed.

**A single crafted byte destroyed the row it was supposed to protect.** `data_type` is
the one derived column read from `raw` rather than from the scrubbed `info` — and a frame
whose info field begins with 0x00 is a perfectly forwardable AX.25 UI frame. Postgres
rejects a NUL in a text column, `_store` swallows its errors to keep the drain alive, and
the packet vanished along with the `raw` that was the whole reason a wrong derivation was
supposed to be safe. Worse, one such row in the backfill's `executemany` aborted the
entire batch, which the sweep then re-claimed every minute **forever** — one transmission
would have permanently stopped the archive from ever being classified. Fixed by a storable-
identifier guard and a SAVEPOINT per row. Both halves have a test that fails without them.

**Relayed frames were reported `heard_direct`.** `direct` was computed from the *inner*
path — somebody else's hop, written by whoever composed the frame — so a transmitter that
wrote a clean inner path earned a "heard direct" attribution for any callsign it chose. It
now reads the outer path, which is the only evidence about how *we* received it.

**Compressed position reports were read at uncompressed offsets.** APRS101 ch.9 defines a
second, shorter layout; the symbol code sits at offset 10, not 19. Every compressed weather
station was invisible to the Weather filter — the exact failure the symbol check exists to
prevent — and an underscore in a comment could invent one that was not there.

**An origin was never checked for being callsign-shaped.** The third-party header is plain
text inside `info`, so one transmitter could mint a hundred stations on the roster,
including `N4TDX*` sitting next to the real `N4TDX`. The check is deliberately looser than
AX.25 — `K4JTT-D`, `N1MPR-C` and `WINLINK` are all real originators in the owner's own
capture, and rejecting them to stop a hypothetical attacker would delete real stations.

The review also confirmed the things worth confirming: the command gate is untouched and
consumes nothing classifier-derived; the sweep runs under RLS; the partial index is really
used; and migration 0185 is rolling-restart safe. Its mutation matrix showed ten surviving
mutants, including one that deleted the entire raw-recovery feature — the module's headline
justification — with the suite still green. All ten are killed now, verified by applying
each mutation and watching a test fail.

One review point is a **gap this wave closes rather than a defect**: `_store` swallowing
its errors means new code against an un-migrated schema stops the log with no symptom at
all. `GET /sdr/packets` now reports `store_failures`, a third way this surface can lie
alongside `reachable` and `logging` — packets being heard and lost is now visible from the
PWA rather than only in a log file on a box with no terminal.

### The second independent review (F2 + F3)

A different reviewer, again not the builder. Everything below is fixed; the pattern worth
keeping is that **five of the seven were the same mistake — a state the owner cannot get
out of, or a screen that is wrong without saying so.**

- **A failed station load was a permanent dead end.** The only "All stations" button lived
  *inside* the detail that never arrived, so a 404 or a dropped connection left the owner
  on "Reading N4TDX…" for ever with no exit but leaving the tab. This screen had already
  learned the identical lesson one level up (its own test says so, in those words) and F3
  reintroduced it one level down. The way out now renders *before* the loading state.
- **An error after the first load was invisible.** Once the roster had arrived the error
  branch was unreachable, so a failing `/sdr/stations` froze the list on stale rows under
  a health line still reading green. Errors are a banner now, not a replacement.
- **No stale-response guard.** Tapping a slow station, going back, tapping a fast one
  painted the *first* station's packets under the second one's header — while the chips
  went on querying the second. The sequence token `RadioScreen` already used sat one file
  away and had not been applied.
- **The window-counts query was a full sequential scan, every five seconds.** The only
  statement with no `WHERE`, and `count(DISTINCT origin_call)` sorted every row in the
  table: 45 ms over 40k rows, so ~1.3 s and ~140 MB of buffers per poll at the plan's own
  ~1.2M rows/year, spilling to disk past `work_mem`. It is bounded to a week now — the
  three nested ranges all live inside one — and `old` gets *presence* (an index answers it
  by stopping at the first row) with an exact count only when that range is opened.
- **The roster truncated silently, and could truncate away the owner's own station.**
  Pinning happened client-side, over the already-capped list — and a client cannot pin
  what it was never sent. Over a long range the owner's station falls outside the cap, and
  the screen shows every station except his: exactly the one this feature exists for. The
  pin is a `split_part` sort key in SQL now, applied before the cap, and `truncated` says
  when the list was capped.
- **A carried-in chip could become invisible and unclearable.** Selecting Weather then
  opening a station that only sends positions gave an empty list, a message saying "clear
  the type filter", and no filter on screen to clear. A selected kind now renders at count
  zero.
- **`store_failures` never reached the PWA** — this plan claimed it did. The backend field
  existed and was tested; nothing consumed it. It is on `AprsLogState` now and outranks
  every healthy reading in `receiverHealth`, because "heard 12s ago" beside a drain saving
  nothing is precisely the lie the field exists to stop.

Plus a NUL in the `{call}` path returning a 500 (now the classifier's own callsign guard),
and a fourth copy of the house segmented control — on the screen whose own test forbids
inventing one, and which the clone slipped past by using a different class name. That test
now asserts the range control *reuses* `.seg-tabs`.

Thirteen mutations the reviewer named as survivors are killed, each verified by applying
the mutation and watching a specific test fail — including the one on the wave's headline
claim, where swapping the roster's `HAVING` for a `WHERE` was indistinguishable under the
old fixtures because no station in them sent a mix of kinds.

### F5 as built

Five pieces, all landed:

- **`sdr/symbols.py`** — both tables and the 195 documented overlay combinations, from the
  2015 master index. The overlay rule is the whole point: four of the fifteen symbols on
  this channel are overlaid.
- **`sdr/explain.py`** — the decoder. Pure, total, no I/O. Run over all 600 packets on the
  box: **zero unreadable**. Eight deliberate mutations killed.
- **`components/aprsGlyphs.ts` + `aprsIcons.tsx`** — 166 glyphs, every code with a standard
  meaning. Stored as typed data rather than SVG strings, so nothing needs
  `dangerouslySetInnerHTML` and a malformed shape is a type error rather than a blank icon.
- **`sdr/stations.py`** — returns the reading, the frame, and the row `id`.
- **`components/AprsStations.tsx`** — the row. Eight mutations killed, two of which needed
  tests that did not exist: keying rows on the array index (which silently moves a
  keyboard user's focus to a *different* packet when a poll prepends) and repeating the
  callsign inside a station.

Two API fields carry the distinctions the screen leans on. A packet has **`kind` and
`bucket`** — the row's title says "Telemetry", the chip filters "Other", and one field
serving both would force the row to lie or the chip row to sprawl. And it has **`relay`**,
so the row can say *how* it reached us without repeating the callsign the header carries.

### F4 as built

**The tool's real defect was the column it filtered on.** `source` is the AX.25 sender,
which on this channel is the IGate for three quarters of the traffic — so "has KD4WLE
been heard" searched the wrong column and answered wrong. v2 adds `station`, matching
`origin_call` with a COALESCE onto `source` so a row the sweep has not reached is still
findable. `source` stays, because "what has this RELAY put on the air" is a real
question, just a different one.

Also: `kind`, `since`/`until` (an ISO instant *or* a duration back from now — a model
writes "6h" reliably and computes a timestamp unreliably), and `summarize`, which
answers "who is around" with one line per station instead of making a model count
callsigns across a hundred frames. An unreadable time or an unknown kind is an ERROR
returned to the model, never a silently-ignored filter: a window that quietly did not
apply reports a whole day's traffic as the last hour's and nothing looks wrong. Lines
now go through `explain`, so a position reads "Car (28.6212, -80.8237; 317° at 2 knots)"
rather than `!2837.27N/08049.42W>317/002`. Untrusted-envelope unchanged, in both modes.

**Signal level was a flag, and the claim that it was unrecoverable was wrong.** `-q h`
means precisely "suppress the heard line with the audio level"; we shipped `hd`. Now
`-q d`. Measuring it on the real pipeline changed the design twice:

- The heard line does **not** always name the sender — a digipeated frame reports
  `Digipeater TCPIP audio level = 50`. Pairing by callsign would have attached almost
  nothing on this channel, and sometimes the relay's level to the wrong station.
- The lines are flushed per decode and arrive in the **same millisecond** as their own
  KISS frame, 1:1 and in order, and a failed decode prints no level at all. That is what
  makes pairing by ORDER sound: one slot, claimed once, expiring in 2 s.

`audio_level` is the one column that **cannot be backfilled** — the reading exists only
at decode time — so NULL means "not measured", never "weak", all the way up to the row
on screen, which shows nothing rather than a tint it did not earn.

## What is stored, honestly

The owner asked whether everything heard is being saved so jerv can be pointed at it
later. It substantially is — `raw` is kept losslessly and never truncated — with three
qualifications on the record:

1. **Mic-E identifiers 0x1C/0x1D are control bytes**, stripped from `info` by the NUL
   scrub. F1 recovers them from `raw` (`dti_from_raw`), which is why the classifier
   prefers `raw` over `info`.
2. **A sidecar reconnect leaves an unmarked gap.** The drain resumes; nothing records
   that a stretch is missing, so "no packets at 3am" and "not listening at 3am" look
   identical.
3. **`_store` swallows its own errors** so one bad row cannot end the log. Correct for
   liveness, but it means a broken INSERT stops the log *silently* — which is exactly
   why F1's live-path test writes through real Postgres.

## F5b — what the row SHOWS (GUI gate REOPENED, `../mocks/aprs/j-what-a-row-shows.html`)

Shape D shipped, and the owner's own screen broke it three ways in the first hour. Two of
them the round could not have caught, because of how its sample was built:

1. **A position row rendered as the bare word "Position".** D's rule was *the icon is not
   restated as text* — but a plain position's whole reading IS the symbol name, so the rule
   stripped it to empty, and the coordinates are a `field`, one tap down. The row lost the
   only fact it exists to carry. **Zero of D's 58 sample packets hit this**, because every
   one of them happened to carry a comment. The sample hid the failure mode, and no
   amount of looking at the mock would have shown it.
2. **`/A=-00085` and `tU2k` in the quoted monospace slot**, which by D's own rule means
   *the station's own words*. One is the APRS altitude extension, the other a
   weather-software id. The row attributed protocol bytes to a person — the exact thing
   the two-voice rule exists to prevent. `tU2k` was already in D's data, in 1 row of 58.
3. **Twenty-five identical beacons**, three lines each: N1MPR-C re-announcing one D-STAR
   object every twenty minutes.

1 and 2 are decoder work and land whichever shape wins: the altitude extension and the
weather-station type become FIELDS, and nothing reaches the quoted slot unless it is free
text a human typed. 3 is a question about the list, not the row.

Round 6 puts three shapes on identical real traffic — **E** repairs D (the reading is never
empty; it relaxes the icon rule only when the symbol name is the entire reading), **F**
promotes the reading to the title and demotes the type to a chip, **G** goes to one line and
moves provenance into the tap. Open, awaiting the owner.

**The lesson worth keeping** is about mock data, not about rows: a sample drawn from real
traffic is still a *sample*, and D's happened to exclude the empty case entirely. A mock
round should include the degenerate rows on purpose — the packet with no comment, the
frame that decodes to nothing — because those are where a layout rule turns into a blank.

## F5 — what a packet SAYS (round 5, shape D)

Asked for after the roster shipped: tapping a packet card should turn it into plain
English. Four mocks on real traffic — `../mocks/aprs/f-packet-inline.html` (expand in place),
`g-packet-flip.html` (turn the card over), `h-packet-sheet.html` (a sheet), and
`i-packet-readable.html`, which is where the round landed.

**The owner's decision after seeing the first three: human readable FIRST.** A, B and C all
kept the frame on the row and put the meaning one tap away; D inverts it. On this channel
that is plainly right — the resting list was `` `m3jq6F>/`On D-Star ``, `T#110,190,088` and
`@031030z2837.27N/08049.42W_338/000g000`, three lines of which none can be read.

**Icons.** The standard APRS symbol set is part of the ask, and measuring the channel found
a gap: four of the fifteen symbols on the air are **overlaid** — `I#` is N4TDX identifying
itself as an IGate, `Wa` is the Winlink gateway, `D&`/`Da` are the D-STAR pair — and the
first cut of the decoder treated the overlay character as a table and gave up. The rule is
that a table character which is neither `/` nor `\` selects the ALTERNATE table and is drawn
*on* the icon.

`../research/APRS_SYMBOLS.md` carries the tables and settles the rendering question. We
**draw our own glyphs** rather than embed a set: `hessu/aprs-symbols` ships no LICENSE, marks
69 entries "Licensing: Unknown", carries vendor logos its own copyright notice says to check
for yourself, and is full-colour raster with drop shadows — illegible on `--bg #0E0F11`. The
app already has the mechanism: `components/icons.tsx` is inlined Lucide-style outlines behind
a shared `<Icon>` wrapper, and these are just more of its children. Adding a symbol later is
one line in the label dict; the glyph is optional, because labels and drawings are decoupled.
An unknown symbol renders as the international circle-and-slash, which is what the spec
itself prescribes — honest, rather than blank or guessed.

Three label corrections the tables forced, all live on this channel: `/$` is **Phone** (not
Bank/ATM — that is `\$` on the alternate table), `/r` is **Repeater** (renamed from Antenna
in 2007), `/[` is **Person** (renamed from Jogger in 2015).

**A row's title is the TYPE, and the callsign only where it is not already known.** The
symbol's name is not a headline — on a list where most rows are positions, "Space shuttle"
told you nothing about whose it was. And inside a station, where the header already names the
sender, a callsign in every title is forty copies of a fact on screen crowding out the type;
in a mixed list nothing else says who sent it, so the callsign leads. One row component, two
mounts, one flag.

**The icon is not restated as text.** When the whole reading is the symbol's name the row
already says it — in the glyph, and in that glyph's accessible label. The name stays in the
detail panel, where the reader is asking what the packet contains, and the line is spent on
something else.

Every row carries a glyph so the left edge is not ragged — but the two kinds of glyph are
told apart by tint. An **APRS symbol** is the station's own choice of icon and takes the
accent tint; a **kind glyph** (telemetry, message, status, a plain beacon — packets that
carry no symbol at all) is our inference about the packet and takes the neutral one. They
should not look like the same claim.

Two consequences that shape the build:

- **Two voices, told apart by typeface.** The derived sentence is the app's, in the system
  font; the station's own comment is quoted verbatim in monospace. Typography carries the
  trust boundary so no badge has to. When the only content IS the station's text — a status,
  a beacon — the app says nothing rather than reciting a stranger's sentence in its own
  voice.
- **A wrong decode now reads as a confident wrong sentence** rather than as obviously-raw
  bytes. That is the cost of the inversion, and it is why the frame stays one tap below
  every row and why anything undecodable says so instead of guessing.

Two research dossiers feed it: `../research/APRS_PAYLOAD_DECODING.md` (field-by-field, all
twelve types actually on the air) and `../research/APRS_PACKET_DETAIL_UI.md` (the
disclosure pattern under a live 5-second poll).

**What the decode research settled, against the live box rather than the spec:**

- The decoder must read `raw`, not `info`. Mic-E course bytes are legitimately control
  characters — `0x1C` and `0x7F` both occur here — and the NUL scrub deletes them, which
  shifts every later byte and yields the wrong symbol. Verified: the same KN1B frame heard
  direct and re-injected through the IGate now decodes identically.
- `Heard` needs `dest` and the raw `payload`. Half a Mic-E latitude lives in the AX.25
  destination, and for a relayed frame it is the *inner* one.
- Telemetry is decodable **here**: N1KSC-1 publishes all four companion messages, so
  `T#110,190,088,011,068` becomes 14.25 V supply, 880 heard, 110 digipeated, 68% efficient
  — cross-checked against that station's own beacon text `U=14.2V`. K4KSC-12 publishes
  none, so its card can honestly show only raw numbers. Definitions are forgeable by
  anyone, so only self-definitions are accepted and they are labelled as the station's
  claim.
- Three traps reproduced on real data: a greedy `/A=(\d+)` reads `/A=00000070cm` as
  70 million feet (it is six characters, and a leading `-` is legal); a greedy weather
  scanner reads the trailing software code `tU2k` as a second temperature and clobbers
  `t078`; and `s` means wind speed in one position and snowfall in another.
- **A correction to F1.** `classify.py` claims "the measured capture has compressed traffic
  on it". It does not — zero of 254 frames, with all twelve type combinations present. The
  compressed-layout handling is right as defensive work; the justification was invented and
  is fixed in this wave.

**What the UI research settled:** the shipped packet rows key on the array index, so every
poll remounts the whole list — invisible today, fatal the moment a row holds state, and
already costing a screen-reader user focus every five seconds. And the detail payload is
already free: `stations.py` selects `source`, `path` and `raw` on every poll and discards
them.

## Open

- Does the owner run a digipeater or fill-in digi? Decides whether "digipeated through
  my station" is worth a filter at all.
- Retention. ~130 frames/hour measured is ~3,200/day, ~1.2M/year. `APRS_CONTROL_PLAN.md`
  §7 holds this open; the roster makes it answerable per station.
