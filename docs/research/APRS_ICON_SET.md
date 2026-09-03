# APRS symbols — the complete house glyph set

> **Status:** Living · **Last verified:** 2026-09-03

Research dossier for `../plans/APRS_FILTERING_PLAN.md` F5. The complete house glyph set —
the family system that makes 166 drawings read as one set, the two overlay slots, what
cannot be told apart at 20px, and how the paths were validated. Shipped as
`frontend/src/components/aprsGlyphs.ts` (data) and `aprsIcons.tsx` (the renderer).
Companion to `APRS_SYMBOLS.md`, which carries the tables and the sourcing.

> **Status:** Research dossier · **Date:** 2026-09-03 · Nothing under `/home/user/JBrain2` was modified.

Follows on from `docs/research/APRS_SYMBOLS.md`. That dossier settled the *tables* (§1), the
*overlay rule* (§1a), *why we draw rather than embed* (§3), the *fallback* (§5), and drew the
**15 measured symbols** (§4). This one draws **the rest of both tables** in the same house style,
and states the system that makes 166 drawings read as one set.

Contact sheet (20 px on `--bg #0E0F11`, every glyph, rendered in headless Chromium):
`scratchpad/iconset.png`.

---

## 0. Coverage — the numbers

| | |
|---|---|
| Codes with a standard meaning in `APRS_PRIMARY` | **85** (94 rows − 9 `None`) |
| Codes with a standard meaning in `APRS_ALTERNATE` | **79** (94 rows − 15 `None`) |
| **Total codes that needed a glyph** | **164** |
| Plus legacy `/z` (TBD in the master list; every deployed set still draws a shelter) | 1 |
| Plus the circle-and-slash fallback | 1 |
| **Glyphs delivered** | **166** |
| Distinct drawings (8 deliberate shares — §3) | 158 |
| Documented overlay combinations covered *without extra art* | **195** (37 overlayable bases × the transmitted character) |
| Codes deliberately **not** drawn | 24 (`None` in both tables — reserved, TNC stream-switch, and the never-assigned rows) |
| Raw path data | 23 266 bytes, 140 bytes/glyph average |
| Module source | ~30 KB, **6.4 KB gzipped** — under the "all 188 ≈ 12 KB gzipped" estimate in the prior dossier |

Every glyph is `viewBox="0 0 24 24"`, `fill="none"`, `stroke="currentColor"`, `stroke-width="1.5"`,
`stroke-linecap="round"`, `stroke-linejoin="round"`, single colour, no fills except deliberate
dots (`fill="currentColor" stroke="none"`), no gradients, no shadows, no text except the overlay
character and the digits of `/0`–`/9`, which *are* the symbol.

---

## 1. The family system — the part that makes it a set

Eleven families. Each one fixes a piece of geometry that every member shares, so the eye learns the
family once and then only has to read the difference. The shared constants are the whole trick:

| Constant | Value | Used by |
|---|---|---|
| Wheel | `circle r=1.7` at `cy=17.6` (r 1.5–2.0 where the body demands it) | every road vehicle |
| Cloud | one path, bottom edge at **y = 14** | every sky symbol |
| Precipitation band | **y 14 → 22**, marks hung from the cloud's bottom edge | rain, snow, sleet, hail, shower, thunder, funnel |
| Head | `circle r=3.2` at `cy=7.2` over a shoulder arc | every person |
| Mast | `M12 8.5V20` + splayed legs `M8.5 20 12 12l3.5 8` | repeater, Mic-E repeater, tower-shaped RF |
| Radiating arcs | `M8.6 3.6a5 5 0 0 0 0 6.8` mirrored | "this thing transmits" |
| Roofline | 45° gable over a body, apex ≈ y 4–5, eaves ≈ y 9–11 | every building |
| Waterline | a shallow arc at y ≈ 15–17 with the hull sitting on it | every vessel |
| Chrome shapes | diamond `M12 2.5 21.5 12 12 21.5 2.5 12Z`, 8-point star, circle r=9, box 17×17 r2, triangle | everything that takes a centred overlay |

| # | Family | n | What every member shares | What varies |
|---|---|---|---|---|
| 1 | **Road vehicles** | 21 | the chassis line and the two wheels | body silhouette only — box (`/k`), one-box with a slanted nose (`/v`), open roll-cage (`/j`), long window band (`/U`), big rear wheel (`/F`), cab+trailer (`/u`) |
| 2 | **Aircraft, space & satellites** | 11 | plan-view fuselage on the vertical axis, wings at mid-height, tailplane at the bottom | wing sweep and span: straight+prop bar (`/'`), swept (`/^`), very long and thin (`/g`); rockets and balloons break to a vertical envelope |
| 3 | **Watercraft & coast** | 9 | the waterline arc | above it: crescent (`/C`), sail triangles (`/Y`), superstructure+funnel (`/s`), plan-view hull (`\s`), buoy/lighthouse masts |
| 4 | **RF infrastructure & networks** | 25 | a mast with radiating arcs, **or** an enclosing chrome shape | mast-family: `/r` dot, `/m` mic capsule, `/y` yagi elements, `` /` `` dish. chrome-family: star = digipeater, diamond = gateway/affiliation, circle = VoIP node, box = DTMF |
| 5 | **Buildings, sites & services** | 21 | the 45° roofline over a body | what is inside or on top: cross (`/h`), steeple (`\+`), flag (`/K`), flame (`/d`), mast (`/o`), open sides (`/z`,`\z`), platform (`\D`), awning (`\h`) |
| 6 | **Emergency & public safety** | 10 | a cross, a shield, a flame, or a warning triangle | shield+star = police, plain cross = Red Cross, cross in a rounded square = aid station, triangle+bang = emergency, burst = incident |
| 7 | **People, animals & organisations** | 8 | head circle over a shoulder arc | the animals swap the head for a profile (knight `/e`, floppy-eared head `/p`); the scouting marks are botanical (fleur-de-lis, trefoil) |
| 8 | **Computers & devices** | 8 | a screen or a body rectangle with exactly one identifying mark | bare screen (`/M`), screen + window (`/Z`), screen + prompt (`/x`), screen + person (`/L`), screen + picture (`/T`), hinge (`/l`) |
| 9 | **Weather** | 28 | the one cloud, bottom edge y=14, precipitation hung below it | drop = rain, slanted drop = shower, asterisk = snow, circle = hail, bolt = thunder, open V = funnel; sky-state symbols (sun, fog, haze, wind) drop the cloud |
| 10 | **Markers, geometry & grid** | 24 | the bare chrome shapes at full 24×24 | the position markers (`X`, dot, crosshair, dashed circle, pin) and the grids |
| 11 | **Fallback** | 1 | — | the international circle-and-slash the spec itself prescribes |

**Cross-family reuse is deliberate and load-bearing.** `\_` (weather station with digipeater) is the
`/_` thermometer with a small star badge, so it reads as *both* families at once. `/(`
(mobile satellite station) is the `` /` `` dish put on wheels. `\y` (Skywarn) is the `/E` eye over
the `\f` funnel. Each of those is a sentence made of two words the reader already knows.

---

## 2. Overlays

The alternate table takes an overlay character drawn **on** the icon. There are exactly two slots,
and the base glyph declares which one it uses.

**`centre` (13 bases)** — enclosing shapes with interior room: `\!` `\#` `\$` `\&` `\-` `\0` `\A`
`\D` `\M` `\a` `\m` `\n` `\z`. The character sits inside, which is what APRS itself does. Each base
carries its own baseline and font size because the interiors differ (a diamond's centre is not a
house's centre) — see the slot map in §6.

**`badge` (24 bases)** — everything else: `\'` `\(` `\)` `\%` `\8` `\<` `\>` `\E` `\H` `\O` `\R`
`\W` `\Y` `\[` `\\` `\^` `\_` `\c` `\h` `\i` `\k` `\s` `\u` `\w` `\;`. A monochrome outline cannot
knock a letter out of a shape, so instead the base is scaled to **0.78 about the top-left**
(with `stroke-width="1.923"` on the group, which renders back to exactly 1.5) and the character
takes the freed bottom-right corner at `font-size 8.5`, `text-anchor="end"`. One rule, applied
uniformly, so nothing has to be redrawn per-overlay.

Three details that matter:

1. **`\!` replaces rather than adds.** The base draws a warning triangle with an exclamation mark;
   when an overlay is present the exclamation is dropped and the character takes its place. Anything
   else is two marks fighting for the same 6 px.
2. **The overlay character is data off the wire.** Render it through the framework's text node
   (JSX `{overlay}`), never `dangerouslySetInnerHTML`, and clamp to one character. In a compressed
   report the overlay arrives as `a`–`j` meaning `0`–`9` — map it back before lookup.
3. **Two bases take an overlay that `APRS_OVERLAYABLE` omits.** The set in
   `APRS_SYMBOLS.md` §1 is `set("!#%&'()-0<=>8;ADHMORWY[\\^_ackhinsuwz")`, which does not contain
   `$` or `m` — yet `APRS_OVERLAY` documents `U$`/`L$`/`Y$` (currency) and `\m` is *defined* as
   "value signpost (3-digit)", i.e. the value is the payload. **Both were given centre slots here,
   and the `APRS_OVERLAYABLE` set should gain `$` and `m`.** `\=` is the reverse case: it is in the
   overlayable set and has 15 documented rail overlays, but `APRS_ALTERNATE["="]` is `None`, so
   there is no base to draw. That row deserves a label ("Rail vehicle") before it deserves art.

---

## 3. What cannot be distinguished at 20 px — and what I did instead

### 3a. Vendor logos — not reproduced, on purpose

Seven codes are, in the deployed artwork, somebody's trademark: `/M` (Apple), `/Z` (Microsoft),
`\K` (Kenwood), `\Y` with `AY`/`IY`/`KY`/`YY` (Alinco, Byonics, Icom, Yaesu), and `\R` with
`7R`/`KR`/`MR`/`TR` (7-Eleven, KFC, McDonald's, Taco Bell). `APRS_SYMBOLS.md` §3 already established
that this is precisely why we do not embed hessu's set. Drawing them ourselves would recreate the
same exposure, so:

- `/M` MacAPRS → a bare monitor. `/Z` WinAPRS → a monitor with a **generic** window (title bar +
  body, not four panes). `/x` X-APRS → a terminal with a chevron prompt. Three platforms, three
  non-infringing marks, all in one family.
- `\K` → a generic handheld with an antenna. `\Y` → a generic radio; the manufacturer is the
  **overlay character**, which is exactly how it comes off the wire.
- `\R` → knife and fork; the chain is the overlay character. A 20 px outline of the Golden Arches
  would be both illegal and illegible.

### 3b. Eight glyphs are shared by two codes — all deliberate

| Codes | Shared drawing | Why it is right |
|---|---|---|
| `/#` `\#` | 8-point star | Same object. The alternate one is "green" and overlayable; colour is the disc tint, not the outline. |
| `\&` `\a` | diamond | **They are the same diamond in every deployed set**, separated only by colour. Prior dossier §4 already ruled this: `--steel` disc for `\&`, `--violet` for `\a`, plus the label. |
| `/[` `\[` | person | Same object; `\[` adds an overlay slot for Baby/Skier/Runner/Hiker. |
| `/^` `\^` | airliner | Side vs top view is a distinction without a difference in a plan-view outline set. |
| `/W` `\W` | radome on a tower | Both are literally "NWS site". |
| `/k` `\u` | box truck | Both are literally "truck"; `\u` adds an overlay slot (bulldozer, tanker, snowplough…). |
| `/v` `\v` | van | Both are literally "van". |
| `/z` `\z` | open pavilion | Same shelter; `\z` adds the overlay slot (clinic, morgue, triage…). |

### 3c. Near-collisions that survive — the label carries them

These are the pairs I could not separate at 20 px without making one of them *wrong*:

- **`/<` motorcycle vs `/b` bicycle.** Two wheels and a frame. Separated by body mass (the
  motorcycle has a filled-looking tank/seat wedge, the bicycle a thin triangular frame) — visible
  at 32 px, marginal at 20.
- **`/'` small aircraft vs `/g` glider vs `/^` large aircraft.** Wing sweep and span do the work.
  `/'` has a propeller bar at the nose, `/g` has long thin wings and no prop, `/^` is swept. At
  20 px `/'` and `/g` are close.
- **`` \` `` rain vs `\I` rain shower.** Vertical drops vs slanted drops. This is the same
  distinction the real symbol set makes, and it is the same size of distinction.
- **`\.` ambiguous position vs `\o` small circle.** Dashed ring + centre dot vs plain small circle.
- **`\c` CD triangle vs `\n` triangle.** `\c` is the triangle inscribed in a circle (the civil
  defence emblem); `\n` is the bare triangle.
- **`/n` node vs `\C` Coast Guard vs `\0` circle.** Three concentric-circle glyphs. `/n` has a
  filled centre, `\C` has four spokes (a life ring), `\0` is a bare ring that always carries an
  overlay character.

In every one of those the pairing is **glyph + label**, never glyph alone, which
`DESIGN.md` §Accessibility requires anyway. The label string is free (both tables are complete),
so the drawing never has to carry meaning on its own.

### 3d. Two places where the *symbol's* meaning is stretched, not the drawing

- **`\H` "Haze / hazard".** Drawn as a hazed sun, because that is what the base symbol means and
  what every deployed set draws. But its documented overlays are `MH` methane, `RH` radiation,
  `WH` hazardous waste, `XH` skull-and-crossbones — a radiation detector rendering as a hazy sun
  with an R in the corner is weak. The honest fix is upstream (`\H` is two symbols wearing one
  code), not in the artwork; the label reads "Radiation detector" and that is what the reader sees.
- **`\E` "Smoke / visibility".** Drawn as rising plumes. Its overlays cover haze, smoke, blowing
  snow, blowing dust and fog — four of which have their own dedicated codes elsewhere in the table.
  Same situation, same answer.

### 3e. Where a `<text>` node is used on purpose

`/0`–`/9` (numbered circles) draw the digit with `<text stroke="none" fill="currentColor">`, because
the digit *is* the symbol — same mechanism as the overlay, same escape rules. Everything else in the
set is pure geometry. `\P` Parking and `\?` Information kiosk look like letterforms but are **drawn
as stroked paths** (a stem and a bowl; a stem and a dot), following the precedent already in the
repo — `SigmaIcon` in `frontend/src/components/icons.tsx` draws a Σ as a path.

---

## 4. Validation — how I know these render

Four independent passes; a blank icon is worse than a wrong one, so none of this is asserted.

1. **Strict path parser (Python).** Every `d` attribute is tokenised command-by-command:
   unknown commands, wrong argument counts, arc **flags** that are not `0`/`1` (the classic
   `a3 3 0 0 1 .3-6.98` trap where the flags run into the next number without a separator),
   negative arc radii, data before any command, arguments after `Z`, implicit-lineto-after-moveto,
   and paths that do not start with `M`. **166/166 clean.**
2. **XML well-formedness + a fill policy check.** Each glyph's children are parsed as an XML
   fragment; only `path`/`circle`/`rect`/`ellipse`/`text` are allowed; `circle` must have
   `cx`/`cy`/`r`; any `fill` other than `none` must be `currentColor` **with** `stroke="none"`;
   any `<text>` must be `fill="currentColor" stroke="none"`. **0 violations.**
3. **Headless Chromium (`/opt/pw-browsers/chromium`, Playwright).** All 166 glyphs plus 37 overlay
   composites were rendered, then for every child element: `getBBox()` must be non-empty,
   `getTotalLength() > 0` for paths, and the union bounding box must lie inside the 24×24 viewBox
   (−0.8 … 24.8 tolerance for round caps). **0 problems.** This is the check that catches a path
   Chrome truncates at a syntax error but the parser accepted.
4. **Round-trip of the deliverable itself.** The `APRS_GLYPHS` and `APRS_OVERLAY_SLOT` blocks were
   extracted **from this markdown file as written**, `require`d into Node, and compared
   key-by-key and byte-for-byte against the validated source. **166/166 glyphs and 37/37 slots
   identical.** This caught a real bug: the key `"/'"` contains a single quote, which broke the
   single-quoted emission. Validating the *artefact*, not just the *source*, is why it was found.

**Visual review, twice.** The contact sheet was rendered at 20 px on `#0E0F11` and inspected at
20 px and at 44 px, and two revision passes followed:

- *Pass 1 redrew:* `/*` snowmobile (was an unreadable blob), `/;` campground (read as a warning
  triangle, colliding with `\!`), `/=` railroad engine, `/o` EOC (read as a Wi-Fi router, colliding
  with `\8`), `\D` depot (indistinguishable from a house — gained a platform), `\h` store (gained an
  awning), `\f` funnel cloud (was a closed teardrop, now an open funnel), `\E` smoke (was two bare
  arcs, now plumes rising from a ground line), `\>` car top view (was a featureless rounded box, now
  has windows and mirrors), `\\` GPS (the nav arrow read as a letter A), `/t` truck stop, `/W` NWS
  site (was a jellyfish, now a radome on a tower), `/,` Boy Scouts, `/e` horse, `/<` motorcycle.
- *Pass 2 redrew:* `/X` helicopter (the ellipse body read as a keyhole), `/p` dog (pointy ears read
  as a cat — now floppy), `/h` hospital (was the same cross-in-a-rounded-square as `/A` aid station
  — now a building block with a cross and an entrance).

---

## 5. The glyph dict

Paste-ready TypeScript. Keys are the **two transmitted characters** (`"/>"`, `"\\a"`), values are
the SVG children as a string — the same shape as `APRS_SYMBOLS.md` §4, so they drop straight into
the existing `<Icon>` wrapper, which already supplies every attribute.

```ts
// APRS symbol glyphs — house set, Lucide-style outline, 24×24 viewBox.
// Drawn for this repo; no third-party artwork, no trademarks (see §3a).
// Pair every glyph with its label from APRS_PRIMARY / APRS_ALTERNATE / APRS_OVERLAY.
export const APRS_GLYPHS: Record<string, string> = {

  // ─── PRIMARY TABLE  /  — 85 codes with a standard meaning, plus legacy /z ──────
  '/!':
    '<path d="M12 2.6 5 5.2v6.6c0 4.1 2.9 6.9 7 8.6 4.1-1.7 7-4.5 7-8.6V5.2Z"/><path d="m12 8.4 1.4 2.8 3.1.5-2.2 2.2.5 3.1-2.8-1.5-2.8 1.5.5-3.1-2.2-2.2 3.1-.5Z"/>',  // Police / sheriff
  '/#':
    '<path d="M12 2 16.6 7.4 22 12 16.6 16.6 12 22 7.4 16.6 2 12 7.4 7.4Z"/>',  // Digipeater
  '/$':
    '<path d="M6.6 3.5 9.4 3a1.4 1.4 0 0 1 1.5.8l1 2.3a1.4 1.4 0 0 1-.4 1.7l-1.4 1.1a11 11 0 0 0 4.6 4.6l1.1-1.4a1.4 1.4 0 0 1 1.7-.4l2.3 1a1.4 1.4 0 0 1 .8 1.5l-.5 2.8a1.4 1.4 0 0 1-1.4 1.2A15 15 0 0 1 5.4 4.9a1.4 1.4 0 0 1 1.2-1.4Z"/>',  // Phone
  '/%':
    '<circle cx="12" cy="12" r="2.5"/><circle cx="4.4" cy="6.2" r="1.6"/><circle cx="19.6" cy="6.2" r="1.6"/><circle cx="4.4" cy="17.8" r="1.6"/><circle cx="19.6" cy="17.8" r="1.6"/><path d="m5.8 7.2 4.3 3.3M18.2 7.2l-4.3 3.3M5.8 16.8l4.3-3.3M18.2 16.8l-4.3-3.3"/>',  // DX cluster
  '/&':
    '<path d="M12 2.5 21.5 12 12 21.5 2.5 12Z"/><path d="M7.9 12.5c1.05-1.8 2.1-1.8 3.15 0s2.1 1.8 3.15 0"/>',  // HF gateway
  '/\'':
    '<path d="M12 2.8c.8 0 1.3 1.2 1.3 3v3.4h7.4v2.2h-7.4v4.3l2.2 1.7v1.5L12 18.2l-3.5.7v-1.5l2.2-1.7v-4.3H3.3V9.2h7.4V5.8c0-1.8.5-3 1.3-3Z"/><path d="M9.5 3.4h5"/>',  // Small aircraft
  '/(':
    '<path d="M4.5 8.5a6.5 6.5 0 0 0 8.2 8.2Z"/><path d="M8 13 11.2 9.8"/><path d="M14 9.6a4.2 4.2 0 0 0-4.2-4.2"/><path d="M17.6 9.2A7.8 7.8 0 0 0 9.8 1.4"/><circle cx="6.5" cy="20" r="1.5"/><circle cx="15.5" cy="20" r="1.5"/><path d="M6.5 18.5h9"/>',  // Mobile satellite station
  '/)':
    '<circle cx="10.5" cy="15.8" r="5"/><circle cx="15" cy="4.6" r="1.7"/><path d="M13.6 8.4h-3.4a1.7 1.7 0 0 0-1.7 1.9l.6 4.3h4.6l2.6 4.6h2.8"/>',  // Wheelchair (accessible)
  '/*':
    '<rect x="3.5" y="13.8" width="10.5" height="4.4" rx="2.2"/><path d="M5.8 13.8 8.6 9h4.6l2.6 4.8"/><path d="M13.2 9 15.8 6.2h2.4"/><path d="m16.4 13.6 1 5"/><path d="M14 18.6h5.4c1 0 1.6-.6 1.8-1.6"/>',  // Snowmobile
  '/+':
    '<path d="M9.2 4h5.6v5.2H20v5.6h-5.2V20H9.2v-5.2H4V9.2h5.2Z"/>',  // Red Cross
  '/,':
    '<path d="M12 3c1.6 2.4 2.2 4.6 2.2 6.4S13 12.4 12 13.4c-1-1-2.2-2.2-2.2-4S10.4 5.4 12 3Z"/><path d="M9.8 9.4c-2-1.4-4.2-.6-4.6 1.4-.5 2.4 2 4.2 6.8 4.4"/><path d="M14.2 9.4c2-1.4 4.2-.6 4.6 1.4.5 2.4-2 4.2-6.8 4.4"/><path d="M8.6 16.6h6.8"/><path d="M12 15.2v5.4"/>',  // Boy Scouts
  '/-':
    '<path d="M3 10.5 12 3.5l9 7V20a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1Z"/><path d="M9.5 21v-6h5v6"/>',  // House (VHF home station)
  '/.':
    '<path d="M5 5 19 19M19 5 5 19"/>',  // X
  '//':
    '<circle cx="12" cy="12" r="7"/><circle cx="12" cy="12" r="2.4" fill="currentColor" stroke="none"/>',  // Red dot
  '/0':
    '<circle cx="12" cy="12" r="9"/><text x="12" y="15.7" text-anchor="middle" font-size="10.5" font-weight="600" font-family="ui-sans-serif, system-ui, sans-serif" fill="currentColor" stroke="none">0</text>',  // Numbered circle 0
  '/1':
    '<circle cx="12" cy="12" r="9"/><text x="12" y="15.7" text-anchor="middle" font-size="10.5" font-weight="600" font-family="ui-sans-serif, system-ui, sans-serif" fill="currentColor" stroke="none">1</text>',  // Numbered circle 1
  '/2':
    '<circle cx="12" cy="12" r="9"/><text x="12" y="15.7" text-anchor="middle" font-size="10.5" font-weight="600" font-family="ui-sans-serif, system-ui, sans-serif" fill="currentColor" stroke="none">2</text>',  // Numbered circle 2
  '/3':
    '<circle cx="12" cy="12" r="9"/><text x="12" y="15.7" text-anchor="middle" font-size="10.5" font-weight="600" font-family="ui-sans-serif, system-ui, sans-serif" fill="currentColor" stroke="none">3</text>',  // Numbered circle 3
  '/4':
    '<circle cx="12" cy="12" r="9"/><text x="12" y="15.7" text-anchor="middle" font-size="10.5" font-weight="600" font-family="ui-sans-serif, system-ui, sans-serif" fill="currentColor" stroke="none">4</text>',  // Numbered circle 4
  '/5':
    '<circle cx="12" cy="12" r="9"/><text x="12" y="15.7" text-anchor="middle" font-size="10.5" font-weight="600" font-family="ui-sans-serif, system-ui, sans-serif" fill="currentColor" stroke="none">5</text>',  // Numbered circle 5
  '/6':
    '<circle cx="12" cy="12" r="9"/><text x="12" y="15.7" text-anchor="middle" font-size="10.5" font-weight="600" font-family="ui-sans-serif, system-ui, sans-serif" fill="currentColor" stroke="none">6</text>',  // Numbered circle 6
  '/7':
    '<circle cx="12" cy="12" r="9"/><text x="12" y="15.7" text-anchor="middle" font-size="10.5" font-weight="600" font-family="ui-sans-serif, system-ui, sans-serif" fill="currentColor" stroke="none">7</text>',  // Numbered circle 7
  '/8':
    '<circle cx="12" cy="12" r="9"/><text x="12" y="15.7" text-anchor="middle" font-size="10.5" font-weight="600" font-family="ui-sans-serif, system-ui, sans-serif" fill="currentColor" stroke="none">8</text>',  // Numbered circle 8
  '/9':
    '<circle cx="12" cy="12" r="9"/><text x="12" y="15.7" text-anchor="middle" font-size="10.5" font-weight="600" font-family="ui-sans-serif, system-ui, sans-serif" fill="currentColor" stroke="none">9</text>',  // Numbered circle 9
  '/:':
    '<path d="M12 20.8a5.8 5.8 0 0 0 5.8-5.8c0-3.9-2.9-5.3-2.9-8.7-2.9 1.5-4.4 3.9-4.4 6.3 0 1-1 1.5-1.4.8a2.9 2.9 0 0 1-.6-2.2A6.7 6.7 0 0 0 6.2 15a5.8 5.8 0 0 0 5.8 5.8Z"/>',  // Fire
  '/;':
    '<path d="M12 5 3.5 20h17Z"/><path d="m12 11.4 4.8 8.6"/><path d="M2 20h20"/><path d="M12 5V2.8"/>',  // Campground / portable operation
  '/<':
    '<circle cx="5.5" cy="16.6" r="3.1"/><circle cx="18.5" cy="16.6" r="3.1"/><path d="M5.5 16.6h3l1.2-4.4h4.6l2.2 4.4"/><path d="M9.7 12.2 8 9.4H6"/><path d="M14.6 12.2h-4.4"/><path d="m16.4 12.2 2-3h1.6"/>',  // Motorcycle
  '/=':
    '<path d="M3.5 16.5V7.5h6v9"/><path d="M9.5 16.5v-6h10.5v6"/><path d="M3.5 16.5h16.5"/><path d="M17 10.5V8h2.2v2.5"/><path d="M5 9.6h3v2.6H5Z"/><circle cx="6" cy="18.4" r="1.5"/><circle cx="12.5" cy="18.4" r="1.5"/><circle cx="17.5" cy="18.4" r="1.5"/>',  // Railroad engine
  '/>':
    '<path d="M4 15.5h16v-2.4a1.6 1.6 0 0 0-1-1.5l-2.2-.9-1.9-2.8a2 2 0 0 0-1.7-.9h-2.4a2 2 0 0 0-1.7.9L7.2 10.7l-2.2.9A1.6 1.6 0 0 0 4 13.1Z"/><circle cx="7.6" cy="17.4" r="1.8"/><circle cx="16.4" cy="17.4" r="1.8"/>',  // Car
  '/?':
    '<rect x="3" y="4" width="18" height="7" rx="1.5"/><rect x="3" y="13" width="18" height="7" rx="1.5"/><path d="M6.5 7.5h.01M6.5 16.5h.01"/>',  // File server
  '/@':
    '<circle cx="15" cy="8.5" r="1.4"/><path d="M15 7.1c0-2.7 1.3-4.9 4.4-4.9-2 1.3-2.9 2.8-3 4.9"/><path d="M15 9.9c0 2.7-1.3 4.9-4.4 4.9 2-1.3 2.9-2.8 3-4.9"/><path d="M2.5 21c1.4-3.4 3.6-6.2 6.6-8.2" stroke-dasharray="2.6 2.4"/>',  // Hurricane predicted path
  '/A':
    '<rect x="3.5" y="3.5" width="17" height="17" rx="3"/><path d="M12 8v8M8 12h8"/>',  // Aid station
  '/B':
    '<rect x="3" y="3.5" width="18" height="14" rx="1.5"/><path d="M12 17.5v3.5"/><path d="M6.5 7.5h5M6.5 11h8M6.5 14.5h4"/>',  // BBS
  '/C':
    '<path d="M2.5 12.5c2.5 5 16.5 5 19 0"/><path d="M2.5 12.5c2.5-1.7 16.5-1.7 19 0"/><path d="m9.5 5.5 4 7"/><path d="M8.6 3.3 6.4 6.6l3.4 1.2Z"/>',  // Canoe
  '/E':
    '<path d="M2.5 12s3.6-6 9.5-6 9.5 6 9.5 6-3.6 6-9.5 6-9.5-6-9.5-6Z"/><circle cx="12" cy="12" r="2.6"/>',  // Eyeball (live event)
  '/F':
    '<circle cx="16.8" cy="16" r="4.6"/><circle cx="5.5" cy="18" r="2.6"/><path d="M2.8 14.5V9.5h5l1.6 4"/><path d="M7.8 9.5V6.4h4.4l1.1 3.1"/><path d="M9.4 13.5h6.2"/>',  // Farm vehicle / tractor
  '/G':
    '<rect x="3.5" y="3.5" width="17" height="17" rx="1"/><path d="M9.2 3.5v17M14.8 3.5v17M3.5 9.2h17M3.5 14.8h17"/>',  // Grid square (6 character)
  '/H':
    '<path d="M3 19v-9"/><path d="M3 14h18v5"/><path d="M21 19v-5a3 3 0 0 0-3-3h-7v3"/><circle cx="7" cy="11.4" r="2"/>',  // Hotel
  '/I':
    '<rect x="8.5" y="3" width="7" height="5" rx="1"/><rect x="2" y="16" width="7" height="5" rx="1"/><rect x="15" y="16" width="7" height="5" rx="1"/><path d="M12 8v4M5.5 16v-4h13v4"/>',  // TCP/IP network station
  '/K':
    '<path d="M4 20.5v-8.8l8-5 8 5v8.8Z"/><path d="M12 6.7V2.6l3.6 1.3L12 5.2"/><path d="M9.5 20.5v-5h5v5"/>',  // School
  '/L':
    '<rect x="2.5" y="4" width="19" height="12.5" rx="2"/><path d="M8.5 20.8h7M12 16.5v4.3"/><circle cx="12" cy="8.6" r="1.8"/><path d="M8.8 13.4a3.6 3.6 0 0 1 6.4 0"/>',  // Logged-on PC user
  '/M':
    '<rect x="2.5" y="4" width="19" height="12.5" rx="2"/><path d="M8.5 20.8h7M12 16.5v4.3"/>',  // MacAPRS
  '/N':
    '<rect x="2" y="7" width="13.5" height="9.5" rx="1.5"/><path d="m2 8.4 6.75 4.2L15.5 8.4"/><path d="M17.8 8.2 21.3 11.8 17.8 15.4"/>',  // NTS station
  '/O':
    '<path d="M12 15.4c3.6 0 6.5-3.2 6.5-7A6.5 6.5 0 0 0 5.5 8.4c0 3.8 2.9 7 6.5 7Z"/><path d="M10.6 15.2 12 17.7l1.4-2.5"/><rect x="10.1" y="17.7" width="3.8" height="3.2" rx="0.6"/>',  // Balloon
  '/P':
    '<path d="M4 15.5h16v-2.4a1.6 1.6 0 0 0-1-1.5l-2.2-.9-1.9-2.8a2 2 0 0 0-1.7-.9h-2.4a2 2 0 0 0-1.7.9L7.2 10.7l-2.2.9A1.6 1.6 0 0 0 4 13.1Z"/><circle cx="7.6" cy="17.4" r="1.8"/><circle cx="16.4" cy="17.4" r="1.8"/><rect x="9.4" y="5.4" width="5.2" height="2" rx="0.7"/>',  // Police car
  '/R':
    '<path d="M2 5.5h13.5v11H2Z"/><path d="M15.5 8.5h3.2l2.8 3.6v4.4h-6Z"/><path d="M4.2 8h6.4v3.2H4.2Z"/><circle cx="6.8" cy="18.2" r="1.6"/><circle cx="17.6" cy="18.2" r="1.6"/>',  // Recreational vehicle
  '/S':
    '<path d="M12 2.5c2.4 2.7 3.4 6 3.4 9.3V16H8.6v-4.2C8.6 8.5 9.6 5.2 12 2.5Z"/><path d="M8.6 13.5 5.2 17.6V20l3.4-2M15.4 13.5l3.4 4.1V20l-3.4-2"/><path d="M12 16v4"/><circle cx="12" cy="8" r="1.1"/>',  // Space shuttle
  '/T':
    '<rect x="2.5" y="4" width="19" height="13" rx="2"/><path d="m5.6 14 3.4-4 2.5 2.6 3-3.5 4 4.9"/><circle cx="8" cy="7.6" r="1.2"/><path d="M9 20.8h6"/>',  // SSTV
  '/U':
    '<rect x="3.5" y="4" width="17" height="12" rx="2"/><path d="M3.5 8.5h17"/><path d="M12 8.5V16"/><circle cx="7.5" cy="18.2" r="1.6"/><circle cx="16.5" cy="18.2" r="1.6"/>',  // Bus
  '/V':
    '<rect x="2" y="8" width="12.5" height="8.5" rx="1.5"/><path d="m14.5 12.5 5.5-3.5v8.5l-5.5-3.5Z"/><path d="M6 8V4.4"/><path d="m4 3 2 1.4 2-1.4"/>',  // ATV (amateur television)
  '/W':
    '<path d="M7 10.5a5 5 0 0 1 10 0Z"/><path d="M9 10.5 8 20.5M15 10.5l1 10"/><path d="M8.5 15h7"/><path d="M6.6 20.5h10.8"/>',  // National Weather Service site
  '/X':
    '<path d="M3.5 4.6h17"/><path d="M10.5 4.6v3"/><path d="M4.6 12.8a5.2 5.2 0 0 1 5.2-5.2h1.4c2.6 0 4.4 1.8 5.2 4.2l4.6.8v1.8h-5.2a5.2 5.2 0 0 1-5.2 3.6H9.8a5.2 5.2 0 0 1-5.2-5.2Z"/><path d="M5.4 19.4h9"/><path d="M7.2 17.4v2M12.2 17.4v2"/>',  // Helicopter
  '/Y':
    '<path d="M2.5 17c2.6 3.2 16.4 3.2 19 0"/><path d="M12.6 3.5v12"/><path d="M11 15.5H4.6L11 6Z"/><path d="M14.2 15.5h5L14.2 8.4Z"/>',  // Yacht (sailboat)
  '/Z':
    '<rect x="2.5" y="4" width="19" height="12.5" rx="2"/><path d="M8.5 20.8h7M12 16.5v4.3"/><path d="M6.6 7.4h10.8v6.2H6.6Z"/><path d="M6.6 9.5h10.8"/>',  // WinAPRS
  '/[':
    '<circle cx="12" cy="7.2" r="3.2"/><path d="M4.8 20.5a7.2 7.2 0 0 1 14.4 0"/>',  // Person
  '/\\':
    '<path d="M12 3 21 20H3Z"/><path d="M12 3v17"/>',  // DF triangle
  '/]':
    '<rect x="2.5" y="5.5" width="19" height="13" rx="2"/><path d="m3 7 9 6.5L21 7"/>',  // Mail / post office
  '/^':
    '<path d="M12 2.4c1.1 0 1.9 1.7 1.9 4.1v2.2l7.6 5.1v2.3l-7.6-2.6v3.6l2.3 2v1.6L12 19.6l-4.2 1.1v-1.6l2.3-2v-3.6L2.5 16.1v-2.3l7.6-5.1V6.5c0-2.4.8-4.1 1.9-4.1Z"/>',  // Large aircraft
  '/_':
    '<path d="M14 14.8V5a2 2 0 1 0-4 0v9.8a4 4 0 1 0 4 0Z"/><path d="M10 8h2M10 11h2"/>',  // Weather station
  '/`':
    '<path d="M4 11a7 7 0 0 0 9 9Z"/><path d="M9 16l3.5-3.5"/><path d="M15 12a4 4 0 0 0-4-4"/><path d="M19 12a8 8 0 0 0-8-8"/>',  // Dish antenna
  '/a':
    '<rect x="2" y="6" width="12" height="10" rx="1"/><path d="M14 9.5h3.4l3.1 3.4V16H14Z"/><circle cx="6" cy="17.6" r="1.7"/><circle cx="18" cy="17.6" r="1.7"/><path d="M8 8.6v4.8M5.6 11h4.8"/>',  // Ambulance
  '/b':
    '<circle cx="5.5" cy="17" r="3.6"/><circle cx="18.5" cy="17" r="3.6"/><path d="M5.5 17h4l3-6.5 3 6.5h3"/><path d="M9.4 10.5h4.6"/><path d="m12.5 10.5 2.5-4h2"/>',  // Bicycle
  '/c':
    '<path d="M6 3v18"/><path d="M6 4.4h11.5L14.6 8l2.9 3.6H6Z"/>',  // Incident command post
  '/d':
    '<path d="M3 20.5V11l9-6.5 9 6.5v9.5Z"/><path d="M8 20.5V15h8v5.5"/><path d="M12 6.8c1.4 1.5 2 2.4 2 3.4a2 2 0 0 1-4 0c0-1 .6-1.9 2-3.4Z"/>',  // Fire station
  '/e':
    '<path d="M8 20.5c0-4 1-6.4 3.4-8.2l-1-2.2-2.6 1.4 1.2-3.6L12.4 5.4V2.9l3 2.7c2.6 1.2 3.6 3.8 3.6 6.9 0 3.2-1.2 5.6-1.2 8Z"/><circle cx="14.6" cy="7.6" r="0.7" fill="currentColor" stroke="none"/>',  // Horse / equestrian
  '/f':
    '<rect x="2" y="7" width="11.5" height="9" rx="1"/><path d="M13.5 9.5h3.6l3.4 3.5V16h-7Z"/><circle cx="5.8" cy="18" r="1.7"/><circle cx="16.6" cy="18" r="1.7"/><path d="M2.8 5.6 12.6 3"/><path d="M4.4 5.4 3.9 3.4M7 4.7 6.5 2.7M9.6 4 9.1 2"/>',  // Fire truck
  '/g':
    '<path d="M12 3.6c.7 0 1.1 1 1.1 2.6v4.3l8.4 1.6v1.6l-8.4-.9v4.3l1.9 1.7v1.2L12 19.4l-3 .6v-1.2l1.9-1.7v-4.3l-8.4.9v-1.6l8.4-1.6V6.2c0-1.6.4-2.6 1.1-2.6Z"/>',  // Glider
  '/h':
    '<rect x="4" y="6.5" width="16" height="14" rx="1.5"/><path d="M12 9.6v5M9.5 12.1h5"/><path d="M8.6 20.5V17h6.8v3.5"/>',  // Hospital
  '/i':
    '<path d="M3 19c2 1.8 4.6 1.8 6.6 0s4.6-1.8 6.6 0 3.3 1.4 4.8 0"/><path d="M6.5 16c1.3-2.6 3.2-4 5.5-4s4.2 1.4 5.5 4Z"/><path d="M12 12V7"/><path d="M12 7c-1.4-1.5-3.4-1.5-4.4 0M12 7c1.4-1.5 3.4-1.5 4.4 0"/>',  // IOTA (islands on the air)
  '/j':
    '<path d="M2.5 15.5v-3.2h19v3.2Z"/><path d="M5 12.3 7.5 7h8l2.5 5.3"/><path d="M11.5 7v5.3"/><circle cx="6.8" cy="17.6" r="2"/><circle cx="17.2" cy="17.6" r="2"/>',  // Jeep
  '/k':
    '<path d="M2 5.5h11v10H2z"/><path d="M13 9h3.8l3.2 3.3v3.2h-7z"/><circle cx="6.5" cy="17.6" r="1.8"/><circle cx="16.8" cy="17.6" r="1.8"/>',  // Truck
  '/l':
    '<rect x="4" y="5" width="16" height="10.5" rx="1.5"/><path d="M2 18.5h20a2 2 0 0 0-2-3H4a2 2 0 0 0-2 3Z"/>',  // Laptop
  '/m':
    '<path d="M12 8.5V20"/><path d="M8.5 20 12 12l3.5 8"/><path d="M8.6 3.6a5 5 0 0 0 0 6.8M15.4 3.6a5 5 0 0 1 0 6.8"/><rect x="10.6" y="2.6" width="2.8" height="5.2" rx="1.4"/>',  // Mic-E repeater
  '/n':
    '<circle cx="12" cy="12" r="8.5"/><circle cx="12" cy="12" r="4.5"/><circle cx="12" cy="12" r="1.3" fill="currentColor" stroke="none"/>',  // Node
  '/o':
    '<path d="M4 20.5v-9l8-4.5 8 4.5v9Z"/><path d="M12 7V3.2"/><path d="M9.8 3.8a3.6 3.6 0 0 1 4.4 0"/><path d="M9.6 20.5V16h4.8v4.5"/>',  // Emergency operations centre
  '/p':
    '<path d="M6.5 9.6c0-3 2.5-5.2 5.5-5.2s5.5 2.2 5.5 5.2v2.8a5.5 5.5 0 0 1-11 0Z"/><path d="M6.6 9.8C4.6 8.8 4 6.2 5 4.6c1.5.4 2.6 1.5 3.2 2.8"/><path d="M17.4 9.8c2-1 2.6-3.6 1.6-5.2-1.5.4-2.6 1.5-3.2 2.8"/><circle cx="9.8" cy="11.4" r="0.8" fill="currentColor" stroke="none"/><circle cx="14.2" cy="11.4" r="0.8" fill="currentColor" stroke="none"/><path d="M12 13.8v1.4"/><path d="M9.9 15a2.6 2.6 0 0 0 4.2 0"/>',  // Dog / rover
  '/q':
    '<rect x="3.5" y="3.5" width="17" height="17" rx="1"/><path d="M12 3.5v17M3.5 12h17"/><path d="m13.6 18.4 2.6-3.6 2.6 3.6Z"/>',  // Grid square (above 128 m)
  '/r':
    '<circle cx="12" cy="6" r="1.4"/><path d="M12 7.6V20"/><path d="M8.5 20 12 11.5 15.5 20"/><path d="M8.6 3.6a5 5 0 0 0 0 6.8M15.4 3.6a5 5 0 0 1 0 6.8"/>',  // Repeater
  '/s':
    '<path d="M2.5 15.5h19l-2.5 4.5H5Z"/><path d="M5.5 15.5V11h9l2 4.5"/><path d="M8.5 11V7.4h3.4V11"/>',  // Ship (power boat)
  '/t':
    '<path d="M2 6h20v2.2H2Z"/><path d="M4 8.2V20.5M20 8.2V20.5"/><rect x="7.5" y="12" width="5" height="8.5" rx="1"/><path d="M9 14.2h2"/><path d="M12.5 14h1.6a1.2 1.2 0 0 1 1.2 1.2v3a1.1 1.1 0 0 0 2.2 0v-2"/>',  // Truck stop
  '/u':
    '<path d="M2 8.5h4.2l1.8 3v4.5H2Z"/><path d="M8.5 6.5h13v9.5h-13Z"/><circle cx="4.6" cy="17.6" r="1.6"/><circle cx="13.6" cy="17.6" r="1.6"/><circle cx="18" cy="17.6" r="1.6"/>',  // Truck (18-wheeler)
  '/v':
    '<path d="M2 15.5V9.6l3-4h9.4l4.6 4v5.9Z"/><circle cx="6.6" cy="17.4" r="1.8"/><circle cx="16.4" cy="17.4" r="1.8"/><path d="M6 9.6h5"/>',  // Van
  '/w':
    '<path d="M12 3.5c2.9 3.7 4.6 6.2 4.6 8.3a4.6 4.6 0 0 1-9.2 0c0-2.1 1.7-4.6 4.6-8.3Z"/><path d="M5 20.5h14"/>',  // Water station
  '/x':
    '<rect x="2.5" y="4" width="19" height="16" rx="2"/><path d="m7 10 2.5 2.5L7 15"/><path d="M12.6 15.4h4.6"/>',  // X-APRS (Unix)
  '/y':
    '<path d="M4 8h16"/><path d="M6 4.6v6.8M9.6 5.2v5.6M13.2 5.6v4.8M16.8 6.2v3.6"/><path d="M12 8v12.6"/><path d="M9 20.6h6"/>',  // Yagi at QTH
  '/z':
    '<path d="M2.5 11.5 12 4l9.5 7.5"/><path d="M5.5 11.5V20M18.5 11.5V20"/><path d="M3.5 20h17"/>',  // Shelter (legacy /z)

  // ─── ALTERNATE TABLE  \  — 79 codes with a standard meaning ────────────────────
  '\\!':
    '<path d="M12 3.5 21.5 20H2.5Z"/><path d="M12 9.8v4.6"/><circle cx="12" cy="17.4" r="0.85" fill="currentColor" stroke="none"/>',  // Emergency   [overlay: centre]
  '\\#':
    '<path d="M12 2 16.6 7.4 22 12 16.6 16.6 12 22 7.4 16.6 2 12 7.4 7.4Z"/>',  // Digipeater (green star)   [overlay: centre]
  '\\$':
    '<rect x="2.5" y="6" width="19" height="12" rx="2"/><path d="M5.4 9h1.8M16.8 15h1.8"/>',  // Bank or ATM   [overlay: centre]
  '\\%':
    '<path d="M2.5 20V11l4.6-2.6V11l4.6-2.6V20Z"/><path d="M12 20V4.6h2.9V20"/><path d="M5.2 14.4h1.4M9.8 14.4h1.4"/>',  // Power plant   [overlay: badge]
  '\\&':
    '<path d="M12 2.5 21.5 12 12 21.5 2.5 12Z"/>',  // Gateway station   [overlay: centre]
  '\\\'':
    '<path d="m12 3 2.2 4.6 5-.6-2.6 4.3 2.6 4.3-5-.6L12 20l-2.2-4.6-5 .6 2.6-4.3L4.8 7l5 .6Z"/>',  // Crash / incident site   [overlay: badge]
  '\\(':
    '<path d="M7 14a3.5 3.5 0 0 1 .3-6.98 4.7 4.7 0 0 1 8.9-.5A3.9 3.9 0 0 1 17 14Z"/>',  // Cloudy   [overlay: badge]
  '\\)':
    '<path d="M2.5 20.5a10 10 0 0 1 19 0"/><rect x="9.2" y="7.5" width="5.6" height="5" rx="1"/><path d="M9.2 10H4.6M14.8 10h4.6"/><path d="M4.6 7.6v4.8M19.4 7.6v4.8"/><path d="M12 7.5V4.6"/>',  // Firenet MEO / MODIS Earth observation   [overlay: badge]
  '\\*':
    '<path d="M12 3.5v17.0M4.605 7.75l14.79 8.5M19.395 7.75l-14.79 8.5"/><path d="m9.4 5.6 2.6 2.4 2.6-2.4M9.4 18.4l2.6-2.4 2.6 2.4"/>',  // Snow
  '\\+':
    '<path d="M5 20.5V11l7-4.6 7 4.6v9.5Z"/><path d="M12 6.4V2.8M10.3 4.4h3.4"/><path d="M10 20.5v-5h4v5"/>',  // Church
  '\\,':
    '<circle cx="12" cy="6.4" r="3"/><circle cx="7.2" cy="12" r="3"/><circle cx="16.8" cy="12" r="3"/><path d="M12 15v5.6"/>',  // Girl Scouts
  '\\-':
    '<path d="M3.5 11 12 4.5l8.5 6.5V20.5h-17Z"/><path d="M17.6 7.6V3"/><path d="M15.6 4a3 3 0 0 1 4 0"/>',  // House (HF)   [overlay: centre]
  '\\.':
    '<circle cx="12" cy="12" r="8.5" stroke-dasharray="2.5 3"/><circle cx="12" cy="12" r="1.4" fill="currentColor" stroke="none"/>',  // Ambiguous / indeterminate position
  '\\/':
    '<circle cx="12" cy="12" r="7"/><path d="M12 2v3.4M12 18.6V22M2 12h3.4M18.6 12H22"/><circle cx="12" cy="12" r="1.3" fill="currentColor" stroke="none"/>',  // Waypoint destination
  '\\0':
    '<circle cx="12" cy="12" r="9"/>',  // Circle (IRLP / EchoLink / WIRES)   [overlay: centre]
  '\\8':
    '<rect x="3.5" y="13" width="17" height="7.5" rx="1.5"/><path d="M7.6 4.6a6.5 6.5 0 0 1 8.8 0M10.1 7.8a3 3 0 0 1 3.8 0"/><path d="M12 10.4v.01"/>',  // Network node (802.11)   [overlay: badge]
  '\\9':
    '<rect x="4.5" y="4.5" width="9.5" height="16" rx="1.5"/><path d="M7 7.6h4.5v3.4H7Z"/><path d="M14 8h2.4a1.4 1.4 0 0 1 1.4 1.4v6.2a1.4 1.4 0 0 0 2.8 0v-4.4l-2-2"/>',  // Gas station
  '\\:':
    '<path d="M7 14a3.5 3.5 0 0 1 .3-6.98 4.7 4.7 0 0 1 8.9-.5A3.9 3.9 0 0 1 17 14Z"/><circle cx="9" cy="18.6" r="1.15"/><circle cx="14.6" cy="19.3" r="1.15"/><circle cx="12" cy="16.4" r="1.15"/>',  // Hail
  '\\;':
    '<path d="M3 8h18"/><path d="m6.6 8-2.6 12M17.4 8l2.6 12"/><path d="M5.4 13.5h13.2"/>',  // Park / picnic area   [overlay: badge]
  '\\<':
    '<path d="M6 3v18"/><path d="M6 4.6h11L6 11Z"/>',  // Advisory (single red flag)   [overlay: badge]
  '\\>':
    '<path d="M8 3.5h8a1.8 1.8 0 0 1 1.75 1.4l1.25 5.6v8a1.5 1.5 0 0 1-1.5 1.5h-11A1.5 1.5 0 0 1 5 18.5v-8l1.25-5.6A1.8 1.8 0 0 1 8 3.5Z"/><path d="M6.6 9.6c3.6-1 7.2-1 10.8 0"/><path d="M6.8 15.4c3.4.9 6.8.9 10.4 0"/><path d="M4.6 11.4 3 12M19.4 11.4 21 12"/>',  // Car (top view)   [overlay: badge]
  '\\?':
    '<path d="M4 8.5 12 4l8 4.5"/><path d="M6 8.5V20.5h12V8.5"/><path d="M12 12.4v5"/><circle cx="12" cy="10.4" r="0.85" fill="currentColor" stroke="none"/>',  // Information kiosk
  '\\@':
    '<circle cx="12" cy="12" r="1.8"/><path d="M12 10.2c0-3.6 1.8-6.6 6-6.6-2.8 1.7-4 3.8-4.1 6.6"/><path d="M12 13.8c0 3.6-1.8 6.6-6 6.6 2.8-1.7 4-3.8 4.1-6.6"/>',  // Hurricane / tropical storm
  '\\A':
    '<rect x="3.5" y="3.5" width="17" height="17" rx="2"/>',  // Box   [overlay: centre]
  '\\B':
    '<path d="M3 6.5h9.6a2.4 2.4 0 1 0-2.4-2.4"/><path d="M3 11.5h13"/><path d="M7 15.200000000000001v4.8M4.912 16.400000000000002l4.176 2.4M9.088000000000001 16.400000000000002l-4.176 2.4"/><path d="M15.5 15.200000000000001v4.8M13.411999999999999 16.400000000000002l4.176 2.4M17.588 16.400000000000002l-4.176 2.4"/>',  // Blowing snow
  '\\C':
    '<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="4"/><path d="M12 3v5M12 16v5M3 12h5M16 12h5"/>',  // Coast Guard
  '\\D':
    '<path d="M3 9.8 12 5l9 4.8"/><path d="M5 9.8V17h14V9.8"/><path d="M3 17h18"/><path d="M4.5 20.5h15"/>',  // Depot   [overlay: centre]
  '\\E':
    '<path d="M3.5 21h17"/><path d="M8 21c0-3.6 3-4.2 3-7.4S8 9.6 8 6.4"/><path d="M14.5 21c0-3.6 3-4.2 3-7.4s-3-4-3-7.2"/>',  // Smoke / visibility
  '\\F':
    '<path d="M7 14a3.5 3.5 0 0 1 .3-6.98 4.7 4.7 0 0 1 8.9-.5A3.9 3.9 0 0 1 17 14Z"/><path d="M8.5 16.6l0.0 2.4M12 16.6l0.0 2.4M15.5 16.6l0.0 2.4"/><path d="M6.5 21.2h11"/>',  // Freezing rain
  '\\G':
    '<path d="M7 14a3.5 3.5 0 0 1 .3-6.98 4.7 4.7 0 0 1 8.9-.5A3.9 3.9 0 0 1 17 14Z"/><path d="M9 16.6v4.4M7.086 17.7l3.8280000000000003 2.2M10.914 17.7l-3.8280000000000003 2.2"/><path d="M15.6 16.6l-1 3.4"/>',  // Snow shower
  '\\H':
    '<circle cx="12" cy="8.5" r="3.8"/><path d="M12 2.2v1.6M4.8 8.5H3.2M20.8 8.5h-1.6M6.9 3.4 8 4.5M17.1 3.4 16 4.5"/><path d="M3.5 16.5h17M6 20h12"/>',  // Haze / hazard   [overlay: badge]
  '\\I':
    '<path d="M7 14a3.5 3.5 0 0 1 .3-6.98 4.7 4.7 0 0 1 8.9-.5A3.9 3.9 0 0 1 17 14Z"/><path d="M9.6 16.6l-1.4 3.2M13.1 16.6l-1.4 3.2M16.6 16.6l-1.4 3.2"/>',  // Rain shower
  '\\J':
    '<path d="M13.6 2.5 5.6 13.6h5l-2 7.9 8-11h-5Z"/>',  // Lightning
  '\\K':
    '<rect x="7" y="5.5" width="10" height="15.5" rx="2"/><path d="M15 5.5V2"/><path d="M9.5 9h5"/><path d="M9.5 13h5M9.5 16.6h5"/>',  // Kenwood HT
  '\\L':
    '<path d="M9 20.5 10 9h4l1 11.5Z"/><path d="M9.6 13h4.8"/><path d="M10 9V6.4h4V9"/><path d="M12 6.4V4"/><path d="M6.6 6 4.2 4.6M17.4 6l2.4-1.4M6.6 9.2H4M17.4 9.2H20"/><path d="M7 20.5h10"/>',  // Lighthouse
  '\\M':
    '<path d="M12 2.8 4.5 5.8v6.4c0 4 3 6.8 7.5 8.5 4.5-1.7 7.5-4.5 7.5-8.5V5.8Z"/>',  // MARS   [overlay: centre]
  '\\N':
    '<path d="M12 20.5V13"/><path d="M9 13h6l-1-4h-4Z"/><path d="M12 9V6.4"/><circle cx="12" cy="4.6" r="1.5"/><path d="M4 18c2.7-2 5.3 2 8 0s5.3-2 8 0"/>',  // Navigation buoy
  '\\O':
    '<path d="M12 15c3.4 0 6-3 6-6.6A6 6 0 0 0 6 8.4c0 3.6 2.6 6.6 6 6.6Z"/><path d="M10.7 14.8 12 17.3l1.3-2.5"/><path d="M10.4 17.3h3.2l-.5 3.4h-2.2Z"/>',  // Rocket / balloon   [overlay: badge]
  '\\P':
    '<rect x="3.5" y="3.5" width="17" height="17" rx="3"/><path d="M10 17V7.4h3.2a2.9 2.9 0 0 1 0 5.8H10"/>',  // Parking
  '\\Q':
    '<path d="M2.5 14h4l3-5.5 3 9.5 3-7.5 2 3.5h4"/><path d="M4 19.5h16"/>',  // Earthquake
  '\\R':
    '<path d="M6 3v6a2.5 2.5 0 0 0 5 0V3"/><path d="M8.5 11.5v9.5"/><path d="M17.5 3c-1.6 1.6-2.2 3.6-2.2 5.6s.8 3.1 2.2 3.1v9.3"/>',  // Restaurant   [overlay: badge]
  '\\S':
    '<rect x="9.5" y="9.5" width="5" height="5" rx="1"/><path d="M9.5 12H4.5M14.5 12h5"/><path d="M4.5 9v6M19.5 9v6"/><path d="M12 9.5V6.2M10.4 5.2a2.2 2.2 0 0 1 3.2 0"/>',  // Satellite / PACSAT
  '\\T':
    '<path d="M7 14a3.5 3.5 0 0 1 .3-6.98 4.7 4.7 0 0 1 8.9-.5A3.9 3.9 0 0 1 17 14Z"/><path d="M13.4 15.8 10 20.2h2.7l-1 2"/>',  // Thunderstorm
  '\\U':
    '<circle cx="12" cy="12" r="4.5"/><path d="M12 2.6v2.4M12 19v2.4M2.6 12H5M19 12h2.4M5.3 5.3 7 7M17 17l1.7 1.7M18.7 5.3 17 7M7 17l-1.7 1.7"/>',  // Sunny
  '\\V':
    '<path d="m12 3 7.8 4.5v9L12 21l-7.8-4.5v-9Z"/><circle cx="12" cy="12" r="2"/><path d="M12 3v3.4M12 17.6V21"/>',  // VORTAC navigation aid
  '\\W':
    '<path d="M7 10.5a5 5 0 0 1 10 0Z"/><path d="M9 10.5 8 20.5M15 10.5l1 10"/><path d="M8.5 15h7"/><path d="M6.6 20.5h10.8"/>',  // NWS site   [overlay: badge]
  '\\X':
    '<path d="M5 10.5h14v1.4a7 7 0 0 1-14 0Z"/><path d="M12 18.9v2.1M8 21h8"/><path d="m10.2 10.5 5.8-6 2.4 2.4-4.4 3.6"/>',  // Pharmacy
  '\\Y':
    '<rect x="2.5" y="8" width="19" height="11" rx="2"/><path d="M6 8 17 3.6"/><circle cx="8" cy="13.5" r="2.5"/><path d="M13 12h6M13 15.4h6"/>',  // Radio / APRS device   [overlay: badge]
  '\\[':
    '<circle cx="12" cy="7.2" r="3.2"/><path d="M4.8 20.5a7.2 7.2 0 0 1 14.4 0"/>',  // Wall cloud / person   [overlay: badge]
  '\\\\':
    '<rect x="3.5" y="3.5" width="17" height="17" rx="3"/><path d="m7.6 16.8 4.4-9.6 4.4 9.6-4.4-3Z"/>',  // GPS / navigation device   [overlay: badge]
  '\\^':
    '<path d="M12 2.4c1.1 0 1.9 1.7 1.9 4.1v2.2l7.6 5.1v2.3l-7.6-2.6v3.6l2.3 2v1.6L12 19.6l-4.2 1.1v-1.6l2.3-2v-3.6L2.5 16.1v-2.3l7.6-5.1V6.5c0-2.4.8-4.1 1.9-4.1Z"/>',  // Aircraft (top view)   [overlay: badge]
  '\\_':
    '<path d="M13 13.6V5a2 2 0 1 0-4 0v8.6a3.6 3.6 0 1 0 4 0Z"/><path d="M9 8h2M9 10.6h2"/><path d="M18 3.4v5.2M15.4 6h5.2M16.2 4.2l3.6 3.6M19.8 4.2l-3.6 3.6"/>',  // Weather station with digipeater   [overlay: badge]
  '\\`':
    '<path d="M7 14a3.5 3.5 0 0 1 .3-6.98 4.7 4.7 0 0 1 8.9-.5A3.9 3.9 0 0 1 17 14Z"/><path d="M8.5 16.6l0.0 3.2M12 16.6l0.0 3.2M15.5 16.6l0.0 3.2"/>',  // Rain
  '\\a':
    '<path d="M12 2.5 21.5 12 12 21.5 2.5 12Z"/>',  // Diamond — organisation / affiliation   [overlay: centre]
  '\\b':
    '<path d="M3 6.5h9.6a2.4 2.4 0 1 0-2.4-2.4"/><path d="M3 11.5h11a2.4 2.4 0 1 1-2.4 2.4"/><circle cx="6" cy="18.4" r="0.9" fill="currentColor" stroke="none"/><circle cx="11" cy="19.4" r="0.9" fill="currentColor" stroke="none"/><circle cx="16" cy="18.2" r="0.9" fill="currentColor" stroke="none"/>',  // Blowing dust / sand
  '\\c':
    '<circle cx="12" cy="12" r="9.2"/><path d="m12 6.4 5 8.8H7Z"/>',  // CD triangle (RACES / CERT / SATERN)   [overlay: badge]
  '\\d':
    '<path d="m12 3.4 2 4.6 5 .5-3.8 3.3 1.1 4.9L12 14.1l-4.3 2.6 1.1-4.9L5 8.5l5-.5Z"/><path d="M4.5 20.4a10 10 0 0 1 15 0"/>',  // DX spot
  '\\e':
    '<path d="M7 14a3.5 3.5 0 0 1 .3-6.98 4.7 4.7 0 0 1 8.9-.5A3.9 3.9 0 0 1 17 14Z"/><path d="m9 16.6-.9 3.2M16 16.6l-.9 3.2"/><circle cx="12.4" cy="19.2" r="1.05"/>',  // Sleet
  '\\f':
    '<path d="M7 14a3.5 3.5 0 0 1 .3-6.98 4.7 4.7 0 0 1 8.9-.5A3.9 3.9 0 0 1 17 14Z"/><path d="M8.4 15.4c1.2 3.4 2.4 5 3.4 5.8M15.6 15.4c-.6 2.4-1.6 4-2.8 5.2"/>',  // Funnel cloud
  '\\g':
    '<path d="M6 3v18"/><path d="M6 4.4h10L6 9.8Z"/><path d="M6 11.6h10L6 17Z"/>',  // Gale (two red flags)
  '\\h':
    '<path d="M3.5 9.5 5.5 5h13l2 4.5Z"/><path d="M5.2 9.5V20.5h13.6V9.5"/><path d="M9.6 20.5V15h4.8v5.5"/>',  // Store / hamfest   [overlay: badge]
  '\\i':
    '<path d="M12 21.4c0 0 7-6.1 7-11a7 7 0 1 0-14 0c0 4.9 7 11 7 11Z"/><circle cx="12" cy="10.4" r="2.4"/>',  // Point of interest   [overlay: badge]
  '\\j':
    '<path d="M12 4.5 18.6 20.5H5.4Z"/><path d="M9.4 13h5.2M8.2 16.4h7.6"/>',  // Work zone
  '\\k':
    '<path d="M2.5 15.5v-3.4l2-.6 2.5-3.5h9l2.5 3.5 2 .6v3.4Z"/><circle cx="6.8" cy="17.4" r="1.7"/><circle cx="17.2" cy="17.4" r="1.7"/><path d="M11.6 8v4.1"/>',  // Special vehicle (SUV / ATV / 4x4)   [overlay: badge]
  '\\l':
    '<rect x="3.5" y="6" width="17" height="12" rx="1" stroke-dasharray="3 2.5"/>',  // Area symbol
  '\\m':
    '<path d="M12 21v-6.6"/><rect x="3" y="4" width="18" height="10.4" rx="1.5"/>',  // Value signpost (3-digit)   [overlay: centre]
  '\\n':
    '<path d="M12 4 21 19.5H3Z"/>',  // Triangle   [overlay: centre]
  '\\o':
    '<circle cx="12" cy="12" r="4.5"/>',  // Small circle
  '\\p':
    '<circle cx="8" cy="7.6" r="3.2"/><path d="M8 1.8v1.4M2.2 7.6h1.4M3.9 3.5 4.9 4.5M12.1 3.5 11.1 4.5M8 12v1.4"/><path d="M10 19.5a3 3 0 0 1 .3-5.98 4 4 0 0 1 7.6-.4 3.3 3.3 0 0 1 .6 6.38Z"/>',  // Partly cloudy
  '\\r':
    '<circle cx="6.8" cy="4.8" r="1.8"/><path d="M6.8 8v6.4M3.8 10.6h6M4.8 21l2-6.6 2 6.6"/><circle cx="17.2" cy="4.8" r="1.8"/><path d="M17.2 8 14.2 15h6Z"/><path d="M15.8 15v6M18.6 15v6"/>',  // Restrooms
  '\\s':
    '<path d="M12 2.5c3 3.5 4.5 8 4.5 12.5v4a1.5 1.5 0 0 1-1.5 1.5H9a1.5 1.5 0 0 1-1.5-1.5v-4C7.5 10.5 9 6 12 2.5Z"/><path d="M9 12h6"/>',  // Ship / boat (top view)   [overlay: badge]
  '\\t':
    '<path d="M4 4.5h16"/><path d="M6 8h13M8.5 11.5h9M11 15h5.5M12.6 18.4h2.4"/><path d="M14.6 18.4c-1 1.6-2.6 2.4-4.6 2.6"/>',  // Tornado
  '\\u':
    '<path d="M2 5.5h11v10H2z"/><path d="M13 9h3.8l3.2 3.3v3.2h-7z"/><circle cx="6.5" cy="17.6" r="1.8"/><circle cx="16.8" cy="17.6" r="1.8"/>',  // Truck   [overlay: badge]
  '\\v':
    '<path d="M2 15.5V9.6l3-4h9.4l4.6 4v5.9Z"/><circle cx="6.6" cy="17.4" r="1.8"/><circle cx="16.4" cy="17.4" r="1.8"/><path d="M6 9.6h5"/>',  // Van
  '\\w':
    '<path d="M2.5 16c2-2 4-2 6 0s4 2 6 0 4-2 6 0"/><path d="M2.5 20.5c2-2 4-2 6 0s4 2 6 0 4-2 6 0"/><path d="M12 12.4V3.6M8.4 7.2 12 3.6l3.6 3.6"/>',  // Flooding / avalanche / landslide   [overlay: badge]
  '\\x':
    '<path d="M6.5 4.5 17.5 15.5M17.5 4.5 6.5 15.5"/><path d="M2.5 19.6c2-2 4-2 6 0s4 2 6 0 4-2 6 0"/>',  // Wreck or obstruction
  '\\y':
    '<path d="M3 8s3.6-4.6 9-4.6S21 8 21 8s-3.6 4.6-9 4.6S3 8 3 8Z"/><circle cx="12" cy="8" r="2"/><path d="M8.6 15.2c.6 3.2 1.8 5 3.4 5.9M15.4 15.2c-.6 3.2-1.8 5-3.4 5.9"/>',  // Skywarn
  '\\z':
    '<path d="M2.5 11.5 12 4l9.5 7.5"/><path d="M5.5 11.5V20M18.5 11.5V20"/><path d="M3.5 20h17"/>',  // Shelter   [overlay: centre]
  '\\{':
    '<path d="M3 7.6c2-1.6 4-1.6 6 0s4 1.6 6 0 4-1.6 6 0"/><path d="M3 12.6c2-1.6 4-1.6 6 0s4 1.6 6 0 4-1.6 6 0"/><path d="M3 17.6c2-1.6 4-1.6 6 0s4 1.6 6 0 4-1.6 6 0"/>',  // Fog

  // ─── Fallback ───────────────────────────────────────────────────────────────────
  '??':
    '<circle cx="12" cy="12" r="9"/><path d="M5.6 5.6 18.4 18.4"/>',  // Unknown symbol (fallback)
};
```

---

## 6. Overlay slots, the renderer, and the fallback

### 6a. The slot map

```ts
// Which slot an alternate-table base uses for its overlay character.
// ["centre", baselineY, fontSize] — the character sits inside the shape.
// ["badge"]                       — the base is scaled 0.78 about the top-left and the
//                                   character takes the freed bottom-right corner.
export type OverlaySlot = ["centre", number, number] | ["badge"];
export const APRS_OVERLAY_SLOT: Record<string, OverlaySlot> = {
  '!': ["centre", 17.4, 8.5],     // \!  Emergency
  '#': ["centre", 15.4, 9.0],     // \#  Digipeater (green star)
  '$': ["centre", 15.6, 10.0],    // \$  Bank or ATM
  '%': ["badge"],                 // \%  Power plant
  '&': ["centre", 15.7, 10.0],    // \&  Gateway station
  '\'': ["badge"],                // \'  Crash / incident site
  '(': ["badge"],                 // \(  Cloudy
  ')': ["badge"],                 // \)  Firenet MEO / MODIS Earth observation
  '-': ["centre", 17.8, 8.5],     // \-  House (HF)
  '0': ["centre", 15.7, 10.5],    // \0  Circle (IRLP / EchoLink / WIRES)
  '8': ["badge"],                 // \8  Network node (802.11)
  ';': ["badge"],                 // \;  Park / picnic area
  '<': ["badge"],                 // \<  Advisory (single red flag)
  '>': ["badge"],                 // \>  Car (top view)
  'A': ["centre", 15.7, 10.5],    // \A  Box
  'D': ["centre", 15.4, 8.0],     // \D  Depot
  'H': ["badge"],                 // \H  Haze / hazard
  'M': ["centre", 15.6, 9.5],     // \M  MARS
  'O': ["badge"],                 // \O  Rocket / balloon
  'R': ["badge"],                 // \R  Restaurant
  'W': ["badge"],                 // \W  NWS site
  'Y': ["badge"],                 // \Y  Radio / APRS device
  '[': ["badge"],                 // \[  Wall cloud / person
  '\\': ["badge"],                // \\  GPS / navigation device
  '^': ["badge"],                 // \^  Aircraft (top view)
  '_': ["badge"],                 // \_  Weather station with digipeater
  'a': ["centre", 15.7, 10.0],    // \a  Diamond — organisation / affiliation
  'c': ["badge"],                 // \c  CD triangle (RACES / CERT / SATERN)
  'h': ["badge"],                 // \h  Store / hamfest
  'i': ["badge"],                 // \i  Point of interest
  'k': ["badge"],                 // \k  Special vehicle (SUV / ATV / 4x4)
  'm': ["centre", 11.8, 8.0],     // \m  Value signpost (3-digit)
  'n': ["centre", 17.6, 8.5],     // \n  Triangle
  's': ["badge"],                 // \s  Ship / boat (top view)
  'u': ["badge"],                 // \u  Truck
  'w': ["badge"],                 // \w  Flooding / avalanche / landslide
  'z': ["centre", 18.4, 8.0],     // \z  Shelter
};
```

### 6b. The renderer

```tsx
const BADGE_SCALE = 0.78;          // frees the bottom-right corner
const BADGE_STROKE = 1.5 / BADGE_SCALE;   // 1.923 — renders back to exactly 1.5

// `table` is "/" or "\\" or an overlay character; `code` is the symbol character.
export function AprsSymbol({ table, code, label, size = 20 }: {
  table: string; code: string; label: string; size?: number;
}) {
  const isOverlay = table !== "/" && table !== "\\";
  // compressed reports send the overlay as a-j meaning 0-9 (APRS101 ch.9)
  const ov = isOverlay
    ? (table >= "a" && table <= "j" ? String(table.charCodeAt(0) - 97) : table).slice(0, 1)
    : null;
  const key = (isOverlay ? "\\" : table) + code;
  const children = APRS_GLYPHS[key] ?? APRS_GLYPHS["??"];
  const slot = ov ? APRS_OVERLAY_SLOT[code] : undefined;

  // \! draws its own exclamation mark; the overlay replaces it rather than stacking on it.
  const base = (ov && key === "\\!") ? '<path d="M12 3.5 21.5 20H2.5Z"/>' : children;

  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor"
         strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" role="img">
      <title>{label}</title>
      {slot?.[0] === "badge"
        ? <g transform={`scale(${BADGE_SCALE})`} strokeWidth={BADGE_STROKE}
             dangerouslySetInnerHTML={{ __html: base }} />
        : <g dangerouslySetInnerHTML={{ __html: base }} />}
      {ov && slot && (slot[0] === "centre"
        ? <text x="12" y={slot[1]} textAnchor="middle" fontSize={slot[2]} fontWeight="600"
                fontFamily="ui-sans-serif, system-ui, sans-serif"
                fill="currentColor" stroke="none">{ov}</text>
        : <text x="21.4" y="22" textAnchor="end" fontSize="8.5" fontWeight="600"
                fontFamily="ui-sans-serif, system-ui, sans-serif"
                fill="currentColor" stroke="none">{ov}</text>)}
    </svg>
  );
}
```

The `dangerouslySetInnerHTML` is on the **glyph** — a compile-time constant from this file, never
packet data. The overlay character, which *is* packet data, goes through the JSX text node
`{ov}` and is clamped to one character. That split is the whole security story.

If the shared `Icon` wrapper is preferred over a bespoke `<svg>`, the only change it needs is the
optional `label?: string` already proposed in `APRS_SYMBOLS.md` §3 — swapping `aria-hidden` for
`role="img"` + `<title>` when a label is given. Otherwise put `role="img"` and `aria-label` on the
wrapping `<span class="aprs-sym">` disc and leave shared code alone.

### 6c. The fallback rule (unchanged from `APRS_SYMBOLS.md` §5)

`APRS_GLYPHS["??"]` is the international circle-and-slash, which is what `symbolsX.txt` itself
prescribes for an unassigned code (4 Feb 2004: *"Unassigned symbols should display the international
symbol of a circle with a slash through it"*). Rendered in the `--slate` disc.

The four cases still hold, and the second is now much rarer than it was:

| Case | Glyph | Label |
|---|---|---|
| Known code, glyph drawn (**all 164**) | the glyph | the label |
| Known code, no glyph yet | circle-slash | the label, verbatim |
| Undefined code (`None` in the table) | circle-slash | ``symbol `/D` — no standard meaning`` |
| Undefined code with an overlay | circle-slash + the character in the badge slot | ``symbol `Q]` — overlay `Q` on an undefined alternate symbol`` |

With this set, **row 2 is empty**: every code that has a meaning has a drawing. Row 2 stays in the
code because it is what makes adding a symbol later cost one line in a label dict, not a PR.

### 6d. Disc tints

Per `DESIGN.md` §Entity-type accents and `APRS_SYMBOLS.md` §3, the glyph goes in a round disc
tinted by family — this is the second channel that recovers the colour the original set used:

| Tint | Families |
|---|---|
| `--steel` | RF infrastructure & networks, computers & devices |
| `--green` | road vehicles, aircraft, watercraft, people |
| `--amber` | weather |
| `--red` (or the emergency accent) | emergency & public safety |
| `--violet` | organisations / affiliation (`\a` and its overlays) |
| `--slate` | markers, geometry, grid, and the fallback |

Always paired with text, never colour alone.

---

## 7. Files produced

| File | What |
|---|---|
| `scratchpad/research_iconset.md` | this dossier — the family system, the limits, the dict |
| `scratchpad/iconset.png` | the contact sheet: all 166 glyphs at 20 px on `#0E0F11`, the 15 measured ones at 32 px, plus every overlay slot with a sample character |
| `scratchpad/iconwork/` | the working set — glyph sources, the strict path validator (`build.py`), the sheet generator, the Playwright render/QA scripts, and `out.json` (glyphs + slots + labels as data) |

---

## Sources

- `docs/research/APRS_SYMBOLS.md` §§1, 1a, 3, 4, 5 — the tables, the overlay list, the
  draw-don't-embed decision, the 15 measured glyphs, the fallback rule.
- `docs/reference/DESIGN.md` §§Iconography, Color tokens, Accessibility — one outline set,
  1.5 px, 20 px in controls, no emoji, muted accents, status always paired with text.
- `frontend/src/components/icons.tsx` — the `<Icon>` wrapper these glyphs are children of.
- `symbolsX.txt` (25 Nov 2015) and `symbols-new.txt` (17 Mar 2021), already in the scratchpad from
  the prior dossier, used to re-check the overlay families while drawing.
