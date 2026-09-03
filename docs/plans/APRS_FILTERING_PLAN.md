# APRS filtering — a station roster, not a packet firehose

> **Status:** In progress · **Last verified:** 2026-09-03 · **Waves:** F1✅(classifier + derived columns) F2✅(roster + station detail API) F3✅(the stations screen) F4◻️(`aprs_recent` v2 + signal level). The GUI gate is **closed** — `../mocks/aprs/e-stations.html`, chosen from `d-filtering.html`'s three shapes, is the binding spec.

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

**F4 ◻️ — `aprs_recent` v2 and signal level.** The tool gains station/kind/since/until/
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

## Open

- Does the owner run a digipeater or fill-in digi? Decides whether "digipeated through
  my station" is worth a filter at all.
- Retention. ~130 frames/hour measured is ~3,200/day, ~1.2M/year. `APRS_CONTROL_PLAN.md`
  §7 holds this open; the roster makes it answerable per station.
