# Decoding an APRS packet into plain English — field-by-field, grounded in the box's own traffic

> **Status:** Living · **Last verified:** 2026-09-03

Research dossier for `../plans/APRS_FILTERING_PLAN.md` F5 (packet detail). Every layout is
checked against the box's own traffic, not only the spec — 254 real frames from 144.390
near Titusville, 2026-09-02/03.

Scope: what a "tap the card, see what it says" expansion can honestly show for every packet type
actually on 144.390 near Titusville, and exactly where it must stop.

**Sources.** Every layout below is checked against **APRS Protocol Reference 1.0.1 (aprs101.pdf)**,
which is present locally as `scratchpad/spec.txt` — chapter/page cites are to that. Device and
software identification (Mic-E suffix codes, TOCALLs) is from **`aprsorg/aprs-deviceid`**
(`tocalls.yaml`, maintained by Hessu OH7LZB for aprs.fi, CC BY-SA), fetched to
`scratchpad/tocalls.yaml`. `aprs.org` itself was returning HTTP 521 during this work, so
`aprs.org/aprs12/mic-e-types.txt` was substituted by `tocalls.yaml`, which is the same data
maintained live.

**Evidence.** 254 rows: `live_full_rows.json` (200) + `samples_rows.json` (54), 171 distinct
payloads, all 12 (kind, data_type) combinations represented. Every decode shown below was produced
by running a throwaway prototype (`scratchpad/proto.py`, `proto2.py`) over those rows, not by hand.

---

## 0. Two findings that change the shape of the implementation

### 0.1 The decoder must read `raw`, not `info` — Mic-E is destroyed by the control-char scrub

`classify.py` already knows this for the data-type identifier (`dti_from_raw`). It is true for the
*whole Mic-E info field*, and the capture proves it:

| what | bytes |
|---|---|
| `raw` (KN1B, direct frame) | `60 6c 4d 70 6c 20 **1c** 2d 2f 60 5f 25` = `` `lMpl <1c>-/`_% `` |
| `info` as stored | `` `lMpl -/`_% `` — the `0x1C` is **gone** |

`0x1C` is the SE+28 course byte (course = 28−28 = 0). Reading the scrubbed `info` shifts every byte
after position 5 left by one, so the symbol comes out as `` (table '`', code '/') `` — nonsense —
instead of the true `/-` (House QTH). Mic-E course/speed bytes legally take values 0x1C–0x7F
(APRS101 ch.10, "IMPORTANT NOTE", p.47), and `0x7F` is in the capture too (`W1LBR-9` →`2XPU5Y`).

**Rule: `explain()` takes the raw frame and re-derives the effective payload as `bytes`.**
`Heard.text` is fine for the ASCII types (position/object/weather/message/telemetry/status), and is
what the human sees, but Mic-E must not use it.

### 0.2 Mic-E also needs the *effective destination*, which `Heard` does not carry

Half the latitude lives in the AX.25 destination callsign. For a third-party frame the relevant
destination is the **inner** one:

```
}KN1B>R8ST8V,TCPIP,N4TDX*:`lMpl <1c>-/`_%
      ^^^^^^ ← the Mic-E latitude is in here, NOT in the AX.25 dest (APBPQ1)
```

`Heard` exposes `origin`, `relay`, `dti`, `kind`, `gated`, `direct`, `addressee`, `text` — no
destination. **Recommend adding `dest: str` (the effective/inner destination) and
`payload: bytes` to `Heard`**; both are already computed inside `classify()` and
`_split_third_party()`, so this is additive and costs nothing.

---

## 1. The twelve types, decoded

Common vocabulary used below:

* **DTI** — data-type identifier, the first byte of the effective info field.
* **Timestamp DHM** — `DDHHMMz` (day, hour, minute, zulu) or `DDHHMM/` (local) or `HHMMSSh`
  (APRS101 ch.6, p.22). It is the *sender's* claim about when the report was made, not when we heard
  it. In this capture `N1KSC-1` transmits `@290303z` on **3 September** — day 29 vs day 3. A card
  must show both "heard 10:17 UTC" and "sender says day 29, 03:03z" and never silently render the
  sender's stamp as a date.
* **Uncompressed position** — `ddmm.hhN` (8) + symbol-table (1) + `dddmm.hhW` (9) + symbol-code (1)
  = 19 bytes, fixed (ch.6, p.23–24).
* **Data extension** — the *optional, fixed 7-byte* field right after the symbol code: `CSE/SPD`
  (`ddd/ddd`), `PHGphgd`, `RNGrrrr`, `DFSshgd`, `Tyy/Cxx` (ch.7, p.27). Anything else at that offset
  is comment text, not an extension.

### 1.1 `Position` / `!` — position, no timestamp, no messaging (55 pkts, 6 stations)

Real packet, heard **direct** from N4TDX (AX.25 dest `APBPQ1` = BPQ32):

```
!2837.86NI08049.44W#PHG7460 BCAT1 NETROM Stack-IGate-Full Service BBS in Mims, Florida (EL98OP)
│└──┬───┘│└───┬───┘│└──┬──┘└──────────────────────────┬─────────────────────────────────────────┘
│   │    │    │    │   │                              └ comment, 68 chars, free text
│   │    │    │    │   └ data extension PHG7460
│   │    │    │    └ symbol code '#'
│   │    │    └ longitude 080° 49.44' W  = -80.824000
│   │    └ symbol TABLE 'I' — not '/' or '\', so: alternate table with overlay letter 'I'
│   └ latitude 28° 37.86' N = +28.631000
└ DTI '!'
```

| Field | Value | Plain English |
|---|---|---|
| Position | 28.631000, −80.824000 | "28°37.86′N 80°49.44′W (Mims, FL)" |
| Symbol | `I` over `\#` | `\#` = *Digi (green star)*, overlay `I` → **IGate** (internet gateway + digipeater) |
| PHG | `PHG7460` | p=7→**49 W**, h=4→**160 ft** above average terrain, g=6→**6 dB**, d=0→**omni**. Spec range formula (ch.7 p.29): `sqrt(2·haat·sqrt((P/10)·(gain/2)))` = **31.6 miles** |
| Comment | `BCAT1 NETROM Stack-IGate-Full Service BBS in Mims, Florida (EL98OP)` | verbatim, uninterpreted |

Other `!` shapes in the capture (all third-party, from the IGate):

* `!2835.05ND08049.00W&RNG0001/A=000010 70cm Voice (D-Star) 435.50000MHz +0.0000MHz` — overlay `D`
  on `\&` (*HF Gateway, diamond*) = **D-Star gateway**; `RNG0001` = pre-calculated range **1 mile**;
  `/A=000010` = **10 feet**.
* `!2835.81N/08050.93W-/A=00000070cm MMDVM Voice (DMR) 439.41250MHz…` — `/-` House QTH; note the
  **`/A=` trap**: the altitude is exactly six characters (`000000`) and the comment then begins
  `70cm`. A regex `/A=(\d+)` reads *70 000 000 feet*. Use `/A=(-?[0-9 ]{6})` — six chars, no more.
* `!2835.81N/08050.93W?442.850 PL107.2 RX/TX  n4tdx.org` — `/?` = *File Server*.
* `!2837.32N/08049.42W[/A=000000…` — `/[` = *Jogger* (a DMR hotspot using the jogger icon; the
  symbol means what the sender chose, and the card should say what the table says, not guess).

**Leading text before `!` (the "deferred position", ch.5 p.18, ≤40 chars):** `classify.py` handles
it; **zero occurrences in these 254 rows**. Keep the support — it costs one `find()` — but do not
build UI affordances for it.

### 1.2 `Position` / `=` — position, no timestamp, station is messaging-capable (7 pkts, 1 station)

```
=2828.47N/08048.25W$360/033/A=000049          (KC3EFJ via APBTUV = BTECH UV-PRO handheld)
│└──┬───┘│└───┬───┘│└──┬──┘└───┬───┘
│   │    │    │    │   │       └ altitude 49 ft
│   │    │    │    │   └ CSE/SPD: course 360° (due north), speed 33 knots (= 38 mph)
│   │    │    │    └ symbol '/$' = Phone
│   │    │    └ 080° 48.25' W = -80.804167
│   │    └ primary symbol table
│   └ 28° 28.47' N = +28.474500
└ DTI '=' — "I can receive messages"
```

Also present: `=2827.81N/08049.55W$000/000/A=-00085`. Two things a naive decoder gets wrong:
`000/000` means **course/speed unknown or not applicable** (ch.7 p.27) — render "not moving /
no course reported", never "heading 0°"; and `/A=-00085` is a **negative** altitude (−85 ft),
which aprs101 does not define but GPS receivers at sea level emit constantly.

Difference from `!` that a card should state: **`=` and `@` mean the station accepts APRS messages;
`!` and `/` mean it does not** (ch.6 p.23). That is genuinely useful to a ham.

### 1.3 `Position` / `@` — position with timestamp, messaging-capable (40 pkts, 1 station)

```
@290303z2835.13N/08039.04WSPLXDigi U=14.2V. KSC Amateur Radio Club
│└──┬──┘└──┬───┘│└───┬───┘│└──────────────┬───────────────────────┘
│   │      │    │    │    │               └ comment ('PLXDigi' is NOT a 7-byte extension → comment)
│   │      │    │    │    └ symbol '/S' = Space Shuttle (KSC Amateur Radio Club — apt)
│   │      │    │    └ 080° 39.04' W = -80.650667
│   │      │    └ primary table
│   │      └ 28° 35.13' N = +28.585500
│   └ day 29, 03:03 zulu  ← heard 2026-09-03; the sender's day counter is wrong
└ DTI '@'
```

`U=14.2V` in the comment is free text — but it cross-checks the telemetry decode in §1.9 exactly
(190 × 0.075 = 14.25 V), which is a good confidence signal that the whole chain is right.

### 1.4 `Position` / `` ` `` — Mic-E (17 pkts, 4 stations) — the hard one

Mic-E splits one position across the AX.25 **destination callsign** and the info field.

#### Destination callsign (APRS101 ch.10, p.43–44)

Six characters, each carrying a latitude digit *plus* one flag bit:

| Char | Lat digit | Msg bit (bytes 1–3) | N/S (byte 4) | Lon offset (byte 5) | W/E (byte 6) |
|---|---|---|---|---|---|
| `0`–`9` | 0–9 | 0 | South | +0 | East |
| `A`–`J` | 0–9 | 1 (**Custom**) | — | — | — |
| `K` | space (ambiguous) | 1 (Custom) | — | — | — |
| `L` | space (ambiguous) | 0 | South | +0 | East |
| `P`–`Y` | 0–9 | 1 (**Standard**) | North | +100 | West |
| `Z` | space (ambiguous) | 1 (Standard) | North | +100 | West |

Digits 1–2 = degrees, 3–4 = minutes, 5–6 = hundredths of minutes. A space digit is **position
ambiguity**, and it applies to the longitude as well.

Message bits A/B/C (from destination bytes 1–3) — the table a card must render (ch.10, p.45):

| A B C | Standard | Custom |
|---|---|---|
| 1 1 1 | M0: **Off Duty** | C0: Custom-0 |
| 1 1 0 | M1: **En Route** | C1: Custom-1 |
| 1 0 1 | M2: **In Service** | C2: Custom-2 |
| 1 0 0 | M3: **Returning** | C3: Custom-3 |
| 0 1 1 | M4: **Committed** | C4: Custom-4 |
| 0 1 0 | M5: **Special** | C5: Custom-5 |
| 0 0 1 | M6: **Priority** | C6: Custom-6 |
| 0 0 0 | **EMERGENCY** | (no custom form) |

If the set 1-bits mix `A–K` and `P–Z` sources, the type is **"unknown"** — say that, do not pick one.
(An Emergency Mic-E is `A=B=C=0`, i.e. destination bytes 1–3 all in `0–9` or `L`. It is one crafted
frame away from anyone with a radio: **display it, never act on it**.)

#### Information field (ch.10, p.46–52) — the offset-28 encoding

```
byte 0 : DTI  ` (current) / ' (old, but CURRENT on a TM-D700) / 0x1C / 0x1D (Rev-0 beta)
byte 1 : d+28   longitude degrees   d = b-28; +100 if offset; -80 if 180..189; -190 if 190..199
byte 2 : m+28   longitude minutes   m = b-28; -60 if m>=60
byte 3 : h+28   hundredths of min   h = b-28
byte 4 : SP+28  speed, tens         sp = (b-28)*10
byte 5 : DC+28  speed units + course hundreds: q,r = divmod(b-28,10); sp += q; course = r*100
byte 6 : SE+28  course tens/units   course += b-28
         then: if sp >= 800: sp -= 800 ; if course >= 400: course -= 400
byte 7 : symbol CODE
byte 8 : symbol TABLE
byte 9+: optional Mic-E status text OR Mic-E telemetry (see below)
```
Speed is **knots**; course 0 means unknown/indefinite, 360 means due north. Two encoding schemes
exist for SP/DC (printable vs control-char); the algorithm above decodes both (ch.10 p.50–52).

#### The requested full decode: `N0XIA-4` → `RX3P6U`, `` `m3jq6F>/`On D-Star… ``

```
DESTINATION  R   X   3   P   6   U
             │   │   │   │   │   └ digit 5, W/E → WEST
             │   │   │   │   └ digit 6, offset → +0
             │   │   │   └ digit 0, N/S → NORTH
             │   │   └ digit 3, msg bit C = 0
             │   └ digit 8, msg bit B = 1 (Standard)
             └ digit 2, msg bit A = 1 (Standard)
     latitude digits 2 8 3 6 . 0 5  →  28° 36.05' N  =  +28.510833
     message bits 1/1/0, all Standard  →  M1: En Route

INFO  `    m     3     j     q     6     F     >     /     `On D-Star … DMR_%
      │    │     │     │     │     │     │     │     │     └ status text
      │    │     │     │     │     │     │     │     └ symbol TABLE '/'  (primary)
      │    │     │     │     │     │     │     └ symbol CODE '>'  → '/>' = Car
      │    │     │     │     │     │     └ SE 0x46=70 → 70-28 = 42
      │    │     │     │     │     └ DC 0x36=54 → 26 → q=2 (speed units), r=6 (course hundreds)
      │    │     │     │     └ SP 0x71=113 → 85 → 850
      │    │     │     └ h 0x6A=106 → 78 hundredths
      │    │     └ m 0x33=51 → 23 minutes
      │    └ d 0x6D=109 → 81, offset +0 → 81 degrees
      └ DTI '`' = current GPS data
     longitude 081° 23.78' W = -81.396333
     speed  = 850 + 2 = 852 → ≥800 → 852-800 = 52 knots (60 mph)
     course = 6*100 + 42 = 642 → ≥400 → 642-400 = 242 degrees (WSW)
```

Status text `` `On D-Star K1XC, W4AES, W4PLB & KJ4OVA DMR_% `` — the leading `` ` `` and the trailing
two bytes are **device identification, not text**:

* Kenwood inserts a type code as the *10th byte of the info field*: `>` = TH-D7, `]` = TM-D700
  (ch.10 p.55). Modern radios add a **trailing 2-character suffix** instead
  (`tocalls.yaml → mice:` / `micelegacy:`).
* `_%` = **Yaesu FTM-400DR**. `]` + trailing `=` = **Kenwood TM-D710**. `>` + `=` = TH-D72,
  `>` + `^` = TH-D74, `>` + `&` = TH-D75, `]` alone = TM-D700, `>` alone = TH-D7A.
  Others in the registry: `_ ` VX-8, `_"` FTM-350, `_#` VX-8G, `_$` FT1D, `_(` FT2D, `_0` FT3D,
  `_1` FTM-300D, `_2` FTM-200D, `_3` FT5D, `_4` FTM-500D, `_)` FTM-100D, `(8` Anytone D878UV,
  `(5` D578UV, `|3`/`|4` Byonics TinyTrak3/4, `[1` APRSdroid, ` X` SainSonic AP510.
* So the human comment is **"On D-Star K1XC, W4AES, W4PLB & KJ4OVA DMR"**, radio **Yaesu FTM-400DR**.

Card rendering: *"N0XIA-4 — car, En Route, 28.5108 −81.3963, 60 mph heading 242° (WSW), Yaesu
FTM-400DR. 'On D-Star K1XC, W4AES, W4PLB & KJ4OVA DMR'."*

#### Mic-E altitude and telemetry in the status text

* **Altitude**: `xxx}` where `xxx` is base-91, metres relative to −10 000 m
  (`(c1−33)·91² + (c2−33)·91 + (c3−33) − 10000`). Real: `W1LBR-9` sends `]"4"}=` →
  `"`=1, `4`=19, `"`=1 → 1·8281 + 19·91 + 1 − 10000 = **11 m**. Its neighbours decode to 6–13 m —
  correct for coastal Brevard County.
* **Mic-E telemetry** (ch.10 p.54): if the byte *after* the symbol table is `` ` ``, `'` or `0x1D`,
  what follows is telemetry, not text (2 or 5 hex channels, or 5 binary). **Ambiguity warning**: the
  same `` ` `` is also the modern device-ID prefix, so a status text like `` `…_% `` (present here,
  twice) is *not* telemetry. Disambiguate by requiring the remainder to be exactly 4 or 10 hex
  digits; otherwise treat it as a device-prefixed status text. When in doubt, show the raw bytes.
* **Maidenhead locator** may also appear in the status text (`IO91SX/G`); not present here.

#### Verification that the whole Mic-E chain is right

Eight consecutive `W1LBR-9` frames (Kenwood TM-D710, symbol `/k` = Truck), oldest → newest:

| dest | lat | lon | speed | course | alt |
|---|---|---|---|---|---|
| `2XQW0R` | 28.2837 | −80.7342 | 56 kt | 157° | 6 m |
| `2XQT9W` | 28.2495 | −80.7242 | 56 kt | 165° | 13 m |
| `2XQS0V` | 28.2177 | −80.7150 | 56 kt | 165° | 10 m |
| `2XQQ0T` | 28.1840 | −80.7062 | 56 kt | 174° | 8 m |
| `2XPY0P` | 28.1500 | −80.7060 | 56 kt | 180° | 9 m |
| `2XPU8T` | 28.0973 | −80.6550 | 34 kt | 140° | 10 m |
| `2XPU5Y` | 28.0932 | −80.6542 | 23 kt | 99° | 9 m |
| `2XPU5T` | 28.0923 | −80.6403 | 10 kt | 152° | 11 m |

A truck driving south down I-95 from Titusville, decelerating into Palm Bay. Latitude falls
monotonically, course stays southbound, speed decays. Independent physical consistency across four
separately-decoded fields — the algorithm is correct.

`KN1B` decodes to 28.5810 −80.8307 (Titusville), `/-` House QTH, **0 knots, course unknown**,
"M2: In Service", Yaesu FTM-400DR. It appears twice: once heard direct, once re-injected through the
IGate as a third-party frame — and `classify.py` correctly attributes both to KN1B.

### 1.5 `Weather` / `@` — complete weather report with position (53 pkts, 2 stations)

The classifier already routes these: DTI is a position identifier, but symbol code `_` makes it
weather (ch.12 p.62).

```
@031030z2837.27N/08049.42W_338/000g000t078r000p000P000h99b10141L000AmbientCWOP.com
│└──┬──┘└──┬───┘│└───┬───┘│└──┬──┘└─┬──┘└─┬──┘└─┬──┘└─┬──┘└─┬──┘└─┬──┘└──┬──┘└──┬──┘
│   │      │    │    │    │   │     │     │     │     │     │     │      │     └ 'AmbientCWOP.com' — free-text comment (see the gotcha below)
│   │      │    │    │    │   │     │     │     │     │     │     │      └ L000 luminosity 0 W/m²
│   │      │    │    │    │   │     │     │     │     │     │     └ b10141 = 1014.1 hPa
│   │      │    │    │    │   │     │     │     │     │     └ h99 = 99 % humidity  (h00 would mean 100 %)
│   │      │    │    │    │   │     │     │     │     └ P000 = 0.00 in rain since midnight
│   │      │    │    │    │   │     │     │     └ p000 = 0.00 in rain, last 24 h
│   │      │    │    │    │   │     │     └ r000 = 0.00 in rain, last hour
│   │      │    │    │    │   │     └ t078 = 78 °F
│   │      │    │    │    │   └ g000 = gust 0 mph (peak in last 5 min)
│   │      │    │    │    └ 338/000 = DIR/SPD extension: wind FROM 338° (NNW) at 0 mph
│   │      │    │    └ symbol code '_' = Weather Station (blue)
│   │      │    └ 080° 49.42' W
│   │      └ 28° 37.27' N
│   └ day 03, 10:30 zulu
└ DTI '@'
```

**Units, and the conversions a card must do** (ch.12 p.64):

| Tag | Field | Raw units | Show as |
|---|---|---|---|
| `c` / `ddd/` | wind direction | degrees, the direction wind comes **from** | "from the NNW (338°)" |
| `s` / `/sss` | sustained 1-min wind | **mph** (see gotcha) | mph, or km/h |
| `g` | gust, peak over 5 min | mph | mph |
| `t` | temperature | °F, `-01`…`-99` for below zero | °F and °C |
| `r` | rain, last hour | **hundredths of an inch** | `r063` → 0.63 in |
| `p` | rain, last 24 h | hundredths of an inch | |
| `P` | rain, since midnight | hundredths of an inch | |
| `h` | humidity | %, **`00` means 100 %** | |
| `b` | barometric pressure | **tenths of millibars = tenths of hPa**, 5 digits | `b10141` → 1014.1 hPa → 29.95 inHg |
| `L` | luminosity | W/m², values ≤ 999 | |
| `l` | luminosity | W/m², values ≥ 1000 (add 1000) | |
| `s` (after rain) | snowfall, 24 h | inches | |
| `#` | raw rain counter | counts | leave raw |

**Gotcha — mph vs knots.** aprs101 ch.7 (p.27) describes the generic 7-byte DIR/SPD extension as
"speed in knots"; ch.12 (p.64) defines the `s` field it replaces as "mph". Every real
implementation (CWOP, aprs.fi, Xastir, aprslib) reads the weather extension as **mph**. Use mph, and
say "mph" in the UI so the reader can sanity-check.

**Gotcha — the trailing software/unit code is not a weather field.** A greedy tag scanner run over
the positionless report below reads `t078` correctly, then hits the trailing `tU2k` and *overwrites
the temperature with "U2k"* (reproduced in `proto2.py`). Fix: a field value must be all digits
(or dots/spaces/leading `-`); anything else terminates the scan and is the trailing
`S`+`uuuu` software/unit code plus comment.

Real values in the capture that exercise the rain and gust paths (03 Sep 01:50z, W4EDL-13):
`_170/016g025t081r000p020P000h91b10142` → wind from 170° at **16 mph gusting 25**, 81 °F, **0.20 in
of rain in the last 24 h**, 91 % RH, 1014.2 hPa. And KD4WLE at 01:59z: `p063P063` = **0.63 in in the
last 24 h, all of it since midnight** — a card can legitimately say "it rained overnight".

`AmbientCWOP.com` and ` W4EDL WX Station` are the trailing free-text comment; APRS101's
`S`+`uuuu` software/unit convention is not being followed by these stations, so the card must show
them as comment, not invent a "software type A".

### 1.6 `Weather` / `_` — positionless weather report (1 pkt, WA4IKQ via `APTW01` = Byonics WXTrak)

```
_09030625c346s000g000t078r000p000P000h94b10135tU2k
│└──┬───┘└─┬──┘└─┬──┘└─┬──┘└─┬──┘└─┬──┘└─┬──┘└─┬──┘└─┬──┘└─┬─┘
│   │      │     │     │     │     │     │     │     │     └ trailing code: software 't' + unit 'U2k'
│   │      │     │     │     │     │     │     │     └ b10135 = 1013.5 hPa
│   │      │     │     │     │     │     │     └ h94 = 94 %
│   │      │     │     │     │     │     └ P000 since midnight
│   │      │     │     │     │     └ p000 last 24 h
│   │      │     │     │     └ r000 last hour
│   │      │     │     └ t078 = 78 °F
│   │      │     └ g000 gust 0 mph
│   │      └ s000 wind 0 mph
│   └ c346 wind from 346°
└ DTI '_' + MDHM timestamp 09-03 06:25 zulu (month/day/hour/min — the ONLY place MDHM is used)
```

Two structural differences from §1.5 the implementation must handle: the timestamp is **8 characters
MDHM**, not 7-character DHM; and wind direction/speed are the **tagged `c`/`s` fields**, not the
`ddd/sss` extension — i.e. *different field order*, exactly as ch.12 p.63–64 warns ("the remaining
parameters may be in a different order (or may not even exist)"). Parse by tag, not by position.

`U2k` = **Ultimeter 500/2000** (ch.12 p.63 unit table). The software code `t` is *not* one of the six
aprs101 defines (`d M P S W X`) — report it as "unrecognised software code 't'", do not guess.

There is **no position in this packet.** A card must say "this station's position comes from a
separate beacon" and must not borrow a position from a different frame without labelling it.

### 1.7 `Object` / `;` — object reports (192 pkts, 4 stations — the biggest bucket)

Fixed layout, ch.11 p.58:

```
;FLMesh-2 *031028z2837.88N/08049.45W`N4TDX-Parish-MVFD-PTP pointing North, Channel 60/5Mhz - …
│└───┬───┘│└──┬──┘└──┬───┘│└───┬───┘│└──────────────────────────┬──────────────────────────────┘
│    │    │   │      │    │    │    │                           └ comment
│    │    │   │      │    │    │    └ symbol code '`' → '/`' = Dish Antenna
│    │    │   │      │    │    └ 080° 49.45' W
│    │    │   │      │    └ primary symbol table
│    │    │   │      └ 28° 37.88' N
│    │    │   └ timestamp day 03, 10:28 zulu (an object ALWAYS has one)
│    │    └ '*' = LIVE   ('_' would mean KILLED — remove it from the map)
│    └ object name, EXACTLY 9 bytes, any printable ASCII, trailing spaces padding
└ DTI ';'
```

Every object in the capture and what a card should say:

| Name (9 bytes) | Symbol | Sender | Rendering |
|---|---|---|---|
| `FLMesh-0/1/2` | `` /` `` Dish Antenna | KD4WLE | mesh-network link, comment gives azimuth + channel |
| `442.850  ` | `/r` Antenna | KD4WLE | name is a **frequency**: 442.850 MHz repeater; comment gives DMR colour code and talkgroups |
| `N4TDX-2  ` | `/z` | KD4WLE | `/z` is **undefined in APRS101 Appendix 2**; `aprs.org/symbols/symbols-new.txt` later assigns the `z` row to *shelters* (`Cz` clinic, `Ez` emergency power, `Mz` morgue, `Tz` triage) — comment "Mims Auxcom" fits, but the card should say "symbol `/z` (not in the standard table)" rather than assert |
| `KM4OSL C` / `N1MPR  C` | `D` over `\a` | KM4OSL-S / N1MPR-S | D-Star repeater module C. Note the **interior spaces are part of the name** — trim trailing only |
| `N4TDX-8  ` / `N4TDX-10` | `W` over `\a` | WINLINK | Winlink gateway, and the **position-ambiguity example** below |

**Position ambiguity, live in the data** (ch.6 p.24):

```
;N4TDX-8  *031013z2838.  NW08052.  Wa144.990MHz Winlink VARA FM Wide Gateway
                        ^^         ^^
```
Two trailing spaces in the latitude hundredths → precision reduced to the **nearest minute**. The
same ambiguity applies to the longitude automatically. Correct rendering: *"28°38′N 80°52′W,
to the nearest minute (about ±1 km)"* — with a circle, not a pin. Levels: 1 space = nearest 0.1′,
2 = nearest 1′, 3 = nearest 10′, 4 = nearest degree.

**Items (`)`)** are the same idea with a variable-length 3–9 char name, no timestamp, and `!`/`_`
as the live/kill flag (ch.11 p.59). None in the capture; `classify.py` already buckets them as
Object. Implement, don't feature.

**Whose object is it?** APRS101's implementation recommendation (p.57): show the *sending* station
alongside the object. Here every object is relayed by N4TDX's IGate but authored by KD4WLE /
WINLINK / KM4OSL-S — `classify.py` already gets this right, and the card must show "object
`FLMesh-2` reported by KD4WLE (relayed by N4TDX)". Any station may take over any object name by
transmitting the same name — objects are **not** owned and are trivially spoofable.

### 1.8 `Message` / `:` — messages (15 pkts, 2 stations — all telemetry definitions)

```
:N1KSC-1  :BITS.11111111,Telemetry test
│└───┬───┘│└─────────────┬────────────┘
│    │    │              └ message text (≤67 chars), may itself contain ':'
│    │    └ mandatory separator at fixed offset 10
│    └ addressee: EXACTLY 9 bytes, space-padded → 'N1KSC-1'
└ DTI ':'
```

`classify.py` already reads the addressee at the fixed offset rather than splitting on `:` — keep
that. General message features a card should support even though the capture has none of them:
a trailing `{nnnnn` message number (an ack is expected), a text of `ack…`/`rej…` (acknowledgement),
addressee `BLNn…` = **bulletin** (broadcast, no ack), addressee = the receiver's own call = directed
message, `?APRS…` = a query (ch.14, ch.15).

All 15 messages here are **telemetry definitions** addressed by N1KSC-1 to itself — see §1.9.

### 1.9 `Other` / `T` — telemetry (47 pkts, 2 stations)

```
T#110,190,088,011,068,000,00000000
││└┬┘ └┬┘ └┬┘ └┬┘ └┬┘ └┬┘ └───┬──┘
││ │   │   │   │   │   │       └ 8 digital bits B1..B8 (MSB first)
││ │   │   │   │   │   └ A5 raw 0
││ │   │   │   │   └ A4 raw 68
││ │   │   │   └ A3 raw 11
││ │   │   └ A2 raw 88
││ │   └ A1 raw 190
││ └ sequence number 110 (3 digits, or the literal 'MIC')
│└ '#'
└ DTI 'T'
```

Raw analogue values are 8-bit unsigned, 000–255 (ch.13 p.68). **They mean nothing on their own.**

#### The companion messages — all four are in the capture, from N1KSC-1 to itself

```
:N1KSC-1  :PARM.Vin,Rx1h,Dg1h,Eff1h,A5,O1,O2,O3,O4,I1,I2,I3,I4
:N1KSC-1  :UNIT.Volt,Pkt,Pkt,Pcnt,None,On,On,On,On,Hi,Hi,Hi,Hi
:N1KSC-1  :EQNS.0,0.075,0,0,10,0,0,10,0,0,1,0,0,0,0
:N1KSC-1  :BITS.11111111,Telemetry test
```

* `PARM.` — names: A1..A5 then B1..B8. Here A1=`Vin`, A2=`Rx1h`, A3=`Dg1h`, A4=`Eff1h`, A5=`A5`,
  B1..B4 = `O1..O4`, B5..B8 = `I1..I4`. The list may stop at any field (ch.13 p.69).
* `UNIT.` — units for A1..A5, labels for B1..B8. `Volt, Pkt, Pkt, Pcnt, None` + `On×4, Hi×4`.
* `EQNS.` — three coefficients `a,b,c` per analogue channel, applied as **`a·v² + b·v + c`**
  (ch.13 p.70). Here: A1 `(0, 0.075, 0)`, A2 `(0, 10, 0)`, A3 `(0, 10, 0)`, A4 `(0, 1, 0)`,
  A5 `(0, 0, 0)`.
* `BITS.` — the *sense* of each digital channel (which state matches the `UNIT.` label) plus a
  project title. `11111111` = all eight labels are active-high; project "Telemetry test".

#### The requested worked example: what "channel 3 = 11" actually means

```
A3 raw 11  →  name 'Dg1h', unit 'Pkt', eqn a=0 b=10 c=0
           →  0·11² + 10·11 + 0 = 110
           →  "Digipeated in the last hour: 110 packets"
```
The full card for `T#110,190,088,011,068,000,00000000`:

| Channel | Raw | Decoded | Reading |
|---|---|---|---|
| A1 `Vin` | 190 | 190 × 0.075 = **14.25 Volt** | supply voltage — matches the beacon's own `U=14.2V` |
| A2 `Rx1h` | 88 | 88 × 10 = **880 Pkt** | packets received in the last hour |
| A3 `Dg1h` | 11 | 11 × 10 = **110 Pkt** | packets digipeated in the last hour |
| A4 `Eff1h` | 68 | 68 × 1 = **68 Pcnt** | digipeat efficiency, % |
| A5 `A5` | 0 | 0 × 0 = **0 None** | unused channel (station published a zero equation) |
| B1–B8 | `00000000` | all **off** (sense = 1) | `O1–O4`, `I1–I4` all inactive |

Summary line: *"PLXDigi telemetry #110 — 14.25 V supply, 880 packets heard and 110 digipeated in the
last hour (68 % efficiency), all I/O off."*

#### What the card can and cannot say without them

`K4KSC-12` (DireWolf 1.7) sends `T#353,53.2,-0.8,77,0,16,11000000` and **no** PARM/UNIT/EQNS/BITS in
the whole capture. Two consequences:

1. **Never invent meaning.** The honest card is: *"Telemetry #353 — five values: 53.2, −0.8, 77, 0,
   16; digital bits 11000000. This station has not published what its channels measure."*
2. **The format is violated in practice.** aprs101 says three-digit integers 000–255; DireWolf emits
   `53.2` and `-0.8`. The parser must accept an optionally-signed decimal of bounded length and
   fall back to the raw string when it cannot.

**Trust boundary — the important one.** The definition messages are ordinary APRS messages: *anyone
with a transmitter can send `:K4KSC-12 :EQNS.0,1000000,0`* and make the card display an invented
voltage. Mitigations: (a) only accept a definition whose **sender base-callsign equals the addressee
base-callsign** (self-definition, which is what real stations do and what happens here); (b) keep it
per-station, bounded (one set, capped lengths, TTL'd); (c) label the card *"channel names as
published by the station"* so a reader knows the source; (d) always keep the raw values one tap away.

### 1.10 `Other` / `>` — status reports (20 pkts, 2 stations)

```
>Powered by WPSD (https://wpsd.radio)
│└─────────────────┬────────────────┘
│                  └ status text (≤62 chars, or ≤55 with a timestamp)
└ DTI '>'
```
A status report is "the station's current mission or any other single-line status" (ch.16 p.80).
Optional leading 7-char DHM **zulu-only** timestamp; three decodable special forms (ch.16 p.81–82):
a Maidenhead locator immediately after `>` (`>IO91SX/-`), a trailing `^HP` beam-heading/ERP code,
or plain text. Here it is plain text from a WPSD hotspot. Card: *"Status: 'Powered by WPSD
(https://wpsd.radio)'"* — and **do not auto-linkify**: the URL is attacker-chosen text.

### 1.11 `Other` / `<` — station capabilities (3 pkts, N4TDX)

```
<IGATE,MSG_CNT=106,LOC_CNT=111
│└─┬─┘ └──────┬──────────────┘
│  │          └ TOKEN=VALUE pairs, comma-separated
│  └ TOKEN
└ DTI '<'
```
Emitted in response to an `IGATE` query (ch.15 p.77). Defined tokens: `IGATE` (this station is an
internet gateway), `MSG_CNT=n` (messages transmitted), `LOC_CNT=n` (local stations it will pass
messages to). Card: *"Capabilities: this station is an internet gateway (IGate). It has transmitted
106 messages and knows 111 local stations."* Unknown tokens: show the raw `TOKEN=VALUE` pair — the
grammar is open-ended by design.

### 1.12 `Other` / `B` — not APRS at all (7 pkts, N4TDX-15 → `BEACON`)

```
BPQ Node Stack/iGate/Chat/Full Service BBSMims, Florida
```
There is no data-type identifier here. `B` is simply the first letter of "BPQ": this is a plain
AX.25 **UI beacon** from BPQ32 node software (AX.25 destination `BEACON`, not an APRS TOCALL), whose
text happens to be two concatenated fields (node description + location). The honest card:

> **Not an APRS data packet.** A plain AX.25 beacon from N4TDX-15 (BPQ32 packet-node software).
> Text: "BPQ Node Stack/iGate/Chat/Full Service BBS" / "Mims, Florida".

Do **not** try to decode it as type `B` — no such APRS type exists. This is the general case for
everything in the `Other` bucket that is not `T`/`>`/`<`: show the text, name the type as
unrecognised, and stop.

---

## 2. Symbols — every one present, and where the table comes from

The symbol is a **table identifier + code** pair. Table `/` = Primary, `\` = Alternate, and any
other character (`0`–`9`, `A`–`Z`, or `a`–`j` in compressed reports) means *Alternate table with
that character overlaid on the icon* (ch.20 p.91).

| In data | Table/code | Meaning (APRS101 Appendix 2) | Seen from |
|---|---|---|---|
| `/$` | primary `$` | Phone | KC3EFJ (BTECH UV-PRO) |
| `/-` | primary `-` | House QTH (VHF) | K4JTT-D, KN1B |
| `/>` | primary `>` | Car (SSID −9 convention) | N0XIA-4 |
| `/?` | primary `?` | File Server | K4JTT |
| `/S` | primary `S` | Space Shuttle | N1KSC-1 (KSC ARC) |
| `/[` | primary `[` | Jogger | KD4WLE-D |
| `/_` | primary `_` | Weather Station (blue) | KD4WLE, W4EDL-13 |
| `` /` `` | primary `` ` `` | Dish Antenna | KD4WLE (FLMesh objects) |
| `/k` | primary `k` | Truck (SSID −14) | W1LBR-9 |
| `/r` | primary `r` | Antenna | KD4WLE (442.850 object) |
| `/z` | primary `z` | **blank in APRS101**; `symbols-new.txt` later uses the `z` row for shelters | KD4WLE (N4TDX-2 object) |
| `D&` | `\&` + overlay `D` | HF Gateway (diamond) → **D-Star gateway** | KM4OSL-C, N1MPR-C |
| `Da` | `\a` + overlay `D` | "A=ARRL, R=RACES etc" box → **D-Star** | KM4OSL-S, N1MPR-S |
| `I#` | `\#` + overlay `I` | Digi (green star) → **IGate** | N4TDX |
| `Wa` | `\a` + overlay `W` | box with `W` → **Winlink gateway** | WINLINK objects |

**Where the full table comes from.** The normative list is **APRS101 Appendix 2** (both tables,
codes `!`–`~`, pp.104–106; local copy `scratchpad/spec.txt` lines 6427–6568). Overlay semantics are
ch.20. The living, community-maintained versions are `aprs.org/symbols/symbols-new.txt` (Bruninga's
later assignments, which fill in blanks like `/z` and standardise overlay conventions such as
`I#` = IGate) and the **`hessu/aprs-symbols`** repository (SVG/PNG icons + descriptions, CC BY-SA),
which is what aprs.fi renders. Recommendation: **embed a static 2×94 table lifted from Appendix 2**
(it has not changed in 25 years, and an on-box table means no network dependency), plus a small
overlay-convention table for the handful that matter here (`I#`, `Wa`, `Da`, `D&`), and render an
unmapped code as "symbol `/z` — no standard meaning" rather than blank or invented.

---

## 3. Compressed positions (APRS101 ch.9) — the direct answer

**There are none.** Zero of the 254 rows examined use compressed position format; all 12
(kind, data_type) combinations are represented in that set, and every position, object and weather
report in it uses the uncompressed `ddmm.hhN` layout. (`classify.py`'s module docstring asserts "the
measured capture has compressed traffic on it" — that is **not** supported by these rows. The
compressed branch in `_symbol_code` is correct and worth keeping, but it is currently dead code on
this box's traffic.)

Implement it anyway — it is 20 lines and one unheard station away from mattering:

```
/YYYYXXXX$csT      13 bytes, replaces "ddmm.hhN/dddmm.hhW$" anywhere it appears
│└──┬─┘└─┬─┘│└┬┘│
│   │    │  │ │ └ T = compression type byte
│   │    │  │ └ cs = course/speed OR radio range OR altitude
│   │    │  └ symbol code
│   │    └ compressed longitude, base 91
│   └ compressed latitude, base 91
└ symbol table id (or overlay) — a NON-digit here is what says "this is compressed"
```
* `lat  = 90 − (base91(YYYY) / 380926)`, `lon = −180 + (base91(XXXX) / 190463)`, where
  `base91(abcd) = (a−33)·91³ + (b−33)·91² + (c−33)·91 + (d−33)`.
* **`cs` is selected by three cases**, in this order (ch.9 p.38–40):
  1. `c == ' '` (space) → **no course/speed/range at all**; `cs` and `T` are meaningless fillers.
  2. `c == '{'` → **pre-calculated radio range** = `2 × 1.08^(s−33)` miles.
  3. `T`'s NMEA-source bits (bits 4–3) `== 10` (GGA) → **altitude** =
     `1.002^((c−33)·91 + (s−33))` feet.
  4. otherwise `c` in `!`..`z` → **course** = `(c−33) × 4` degrees, **speed** =
     `1.08^(s−33) − 1` knots.
* `T` byte (value = ASCII − 33): bit 5 = GPS fix (0 old / 1 current), bits 4–3 = NMEA source
  (00 other, 01 GLL, 10 GGA, 11 RMC), bits 2–0 = compression origin (000 compressed, 001 TNC BText,
  010 software, 100 KPC3, 101 Pico, 110 other tracker, 111 digipeater conversion).
* Compressed reports **cannot carry PHG**, and a numeric overlay must be written `a`–`j` (mapping to
  `0`–`9`) because a compressed position never starts with a digit.

---

## 4. What cannot be decoded — and must not be faked

1. **Free-text comments.** `"N4TDX-Parish-MVFD-PTP pointing North, Channel 60/5Mhz - seanhaga@…"`,
   `"BCAT1 NETROM Stack-IGate…(EL98OP)"`, `"AmbientCWOP.com"`. Show verbatim, escaped, unlinkified.
   The `(EL98OP)` in N4TDX's comment *is* a Maidenhead grid, but it is in free text where the spec
   defines no meaning — pattern-matching grid squares out of comments will fire on false positives.
2. **Vendor extensions in comments.** `U=14.2V` (Microsat), `PL107.2 RX/TX`, `439.41250MHz
   +0.0000MHz`, DMR colour codes and talkgroups. There is a semi-standard frequency-object
   convention (`aprs.org/localinfo.html`) but it is not in aprs101 and is inconsistently followed.
   At most: note "the object name looks like a frequency (442.850 MHz)". Do not parse tone/offset.
3. **Anything needing state the box does not keep**, and how to be honest about each:
   * *Telemetry channel meaning* — needs a PARM/UNIT/EQNS/BITS set that may never arrive
     (`K4KSC-12` never sent one). Say "the station has not published what its channels measure".
   * *Position of a positionless weather station* (`WA4IKQ`) — needs a separate beacon.
   * *Whether an object is still live* — a `;NAME    _` kill can arrive later; a card shows one
     packet's claim at one moment.
   * *Message acks and threading*, *bulletin sequences*, *query/response pairing* — all multi-packet.
   * *Distance and bearing from the receiver* — needs the box's own position, which it may not have.
4. **`Other`/`B`-style frames** — not APRS; name the fact, print the text.
5. **Timestamps as absolute time.** A DHM stamp gives day+hour+minute, no month or year, and
   `N1KSC-1` demonstrably sends the wrong day. Render "sender's timestamp: day 29, 03:03 UTC" beside
   "heard: 10:17 UTC", and only compute an absolute datetime when the day matches within ±1.
6. **DF reports.** `DFSshgd` and the `/BRG/NRQ` bearing extension: **zero occurrences** in the
   capture (contrary to the brief's assumption). Implement `DFS`/`NRQ` for completeness — the tables
   are in ch.7 pp.29–30 — but there is no live example to validate against, so mark it untested.
7. **Any claim about who sent something.** A callsign is plain bytes with no authentication anywhere
   in AX.25 or APRS. Everything on this card is "what the frame says", never "who this is".

---

## 5. Recommended Python structure

### 5.1 Placement and composition

A new pure module `backend/src/jbrain/sdr/explain.py`, sibling to `classify.py`, same contract:
**bytes in, a frozen struct out, no I/O, no LLM, no network, total.** `classify()` says *what a
frame is*; `explain()` says *what it says*. Everything is a cache over `raw`, so a better decoder
can be backfilled — nothing has to be right the first time.

Two small additive changes to `classify.py` (§0):

```python
@dataclass(frozen=True, slots=True)
class Heard:
    ...                       # unchanged fields
    dest: str = ""            # effective destination — INNER one for a third-party frame (Mic-E lat)
    payload: bytes = b""      # effective info field as BYTES from `raw` (Mic-E needs 0x1C/0x7F)
```

### 5.2 The API

```python
@dataclass(frozen=True, slots=True)
class Field:
    label: str          # "Wind"                     — never empty
    value: str          # "from the NNW (338°) at 0 mph"
    raw: str            # "338/000"                  — the bytes this came from, always shown
    note: str = ""      # "gusting to 0 mph (5-minute peak)"
    confident: bool = True   # False ⇒ render greyed/with a caveat

@dataclass(frozen=True, slots=True)
class Explained:
    summary: str                       # ONE line for the collapsed card
    fields: tuple[Field, ...]          # ordered, ready to render top-to-bottom
    position: tuple[float, float] | None    # WGS84 degrees, None if absent/invalid
    ambiguity_min: float = 0.0         # position ambiguity as minutes of arc; 0 = exact
    symbol: str = ""                   # "I#" — table/overlay + code, as transmitted
    symbol_label: str = ""             # "IGate (digipeater with I overlay)" or "" if unmapped
    comment: str = ""                  # free text, scrubbed of control chars, NOT interpreted
    device: str = ""                   # "Yaesu FTM-400DR" / "BPQ32" from Mic-E suffix or TOCALL
    undecoded: str = ""                # bytes we could not attribute — always surfaced
    warnings: tuple[str, ...] = ()     # "info field is shorter than this format requires"

def explain(heard: Heard, *, defs: TelemetryDefs | None = None) -> Explained: ...
```

`TelemetryDefs` is a small frozen struct (`names`, `units`, `eqns`, `bit_sense`, `project`) supplied
by the caller — `explain()` never looks anything up itself, keeping it pure and testable. A separate,
equally pure `parse_telemetry_defs(heard) -> TelemetryDefs | None` recognises PARM./UNIT./EQNS./BITS
messages so the *caller* can decide whether to store them (with the self-definition rule from §1.9).

**Why "a list of labelled fields plus a one-line summary" is the right shape:** the phone card shows
`summary` collapsed, `fields` expanded; every field carries its own `raw`, so the reader can always
see the bytes behind the English; unmapped things land in `undecoded`/`warnings` instead of being
dropped or invented; and the whole struct is JSON-serialisable for the PWA with no bespoke encoder.

### 5.3 Totality and hostility

Non-negotiables for this module, all of them driven by the fact that **every byte came off the air
from anyone with a transmitter**:

1. **Never raises.** One `try/except Exception` around each per-type decoder; on failure return an
   `Explained` with `summary="Could not decode"`, `undecoded=<the raw text>`, and a warning. A
   module-level `explain()` wrapper catches anything the dispatcher itself throws. Unit-test this by
   fuzzing: every byte string of length 0–200, including all-NUL, all-0xFF, and truncations of each
   real packet at every offset, must return an `Explained`.
2. **Slice, never index.** `s[18:19]` returns `""` on a short frame; `s[18]` raises. Every fixed
   offset in §1 is a slice, and every field checks its own width before converting.
3. **Bounded everything.** Cap the input at a few hundred bytes (a UI frame cannot exceed 256 in
   practice); cap the weather tag loop at ~24 iterations; cap object/item names at 9; cap comment
   length before rendering; cap telemetry channels at 5+8. No unbounded `while`.
4. **Anchored, bounded regexes only** (`/A=(-?[0-9 ]{6})`, `PHG([0-9A-Za-z]{4})`) — no `.*`, no
   nested quantifiers, no catastrophic backtracking on attacker-supplied text.
5. **Range-check every physical value.** `|lat| ≤ 90`, `|lon| ≤ 180`, wind dir 0–360, humidity
   0–100, temperature within, say, −100..150 °F; out-of-range ⇒ show raw with
   `confident=False` rather than a fake number. `0.0, 0.0` with symbol `\.` is the spec's **null
   position** (ch.6 p.25) — render "no position reported", not the Gulf of Guinea.
6. **Never authenticate, never gate.** Same rule `classify.py` already states: the output narrows
   and labels. Nothing — no alert, no automation, no notification, no workflow trigger — may fire on
   a decoded field, and an Emergency Mic-E is displayed, not acted upon. Telemetry definitions are
   attacker-supplied and must be labelled as the station's own claim.
7. **Escape on the way out.** Object names and comments are arbitrary printable bytes chosen by the
   sender: strip control characters, do not auto-linkify URLs, do not render as markup, and never
   let an object name be mistaken for a callsign in the roster (`classify.looks_like_station()`
   already sets the precedent).

### 5.4 Suggested build order (by real-world value per line of code)

1. Uncompressed position + symbol table + `/A=` + PHG/RNG/CSE-SPD + ambiguity — covers
   `!`, `=`, `@`, `;` = **294 of the 457 packets**.
2. Weather (both `@`-with-`_`-symbol and positionless `_`) — **54 packets**, and the highest
   "plain English" payoff per packet.
3. Telemetry `T#` + the four definition messages — **62 packets**, and the only type where the
   decode is genuinely impossible without cross-packet state, so worth getting the honesty right.
4. Mic-E — **17 packets**, but the only type where a naive implementation produces *confidently
   wrong* output, and the one that needs the raw-bytes plumbing.
5. Status `>`, capabilities `<`, non-APRS `B` — **30 packets**, nearly free.
6. Compressed positions, items `)`, DF/NRQ, raw NMEA `$`, Peet Bros `#`/`*` — **0 packets today**;
   implement for correctness, do not tune UI for them.
