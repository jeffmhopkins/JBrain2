# APRS filtering — a station roster, not a packet firehose

> **Status:** In progress · **Last verified:** 2026-09-03 · **Waves:** F1✅(classifier + derived columns) F2◻️(roster + station detail API) F3◻️(the stations screen) F4◻️(`aprs_recent` v2 + signal level). The GUI gate is **closed** — `../mocks/aprs/e-stations.html`, chosen from `d-filtering.html`'s three shapes, is the binding spec.

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

**F2 ◻️ — roster + station detail API.** Server-side filtering on the Runs-log
precedent: GET query params, clamped limits, a bounded stats aggregate taking `since`.
Two endpoints — the roster (one row per `origin_call`, last heard, count, kinds present)
and one station's traffic. The kind chips filter the roster by *which stations have such
traffic*, which is a `HAVING`, not a `WHERE`.

**F3 ◻️ — the stations screen.** `e-stations.html` is binding spec.

**F4 ◻️ — `aprs_recent` v2 and signal level.** The tool gains station/kind/since/until/
summarize (tool `version` bump + digest re-pin at `tests/unit/test_agent_readtools.py`),
keeping its `<untrusted_external_data source="heard-over-the-air">` wrapper — the two
trust tiers are unchanged, and a station roster does not make a callsign an identity.
Signal level: an **earlier claim that it was unrecoverable was wrong.** We ship direwolf
with `-q hd`, and `h` is precisely "suppress the Heard line with the audio level".
Measured with `-q d`: `N0CALL-9 audio level = 50(14/14)   _||||||__`. It is a flag
choice and a parse, not an SDR limitation.

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
