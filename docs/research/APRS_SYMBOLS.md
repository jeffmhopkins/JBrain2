# APRS symbols — the icon set, the tables, and how to render them

> **Status:** Living · **Last verified:** 2026-09-03

Research dossier for `../plans/APRS_FILTERING_PLAN.md` F5. The symbol tables, the overlay
rule, and why we draw our own glyphs rather than embedding anyone's. Supersedes
`APRS_PAYLOAD_DECODING.md` §2 where the two disagree — that section was written from
APRS101 Appendix 2 (2000), which is stale on 18 codes.

> **Status:** Research dossier · **Date:** 2026-09-03 · Nothing under `/home/user/JBrain2` was modified.

Follows on from `docs/research/APRS_PAYLOAD_DECODING.md` §2 (which established the table/code/overlay
model and the 15 symbols on the air) and `docs/research/APRS_PACKET_DETAIL_UI.md` §"Position"
(which already ruled: *"the symbol (table + code → a local glyph table, never a fetched icon)"*).
This dossier supplies the tables, resolves the 15, and settles the rendering question.

---

## 0. Sources — what I used and how I cross-checked

`https://www.aprs.org/symbols/symbols-new.txt` was **still down** (HTTP 521 from Cloudflare,
`Retry-After: 120`, retried; `web.archive.org` is blocked by this session's egress policy). So I
worked from three independent copies and cross-checked them against each other:

| # | Source | What it is | How obtained |
|---|---|---|---|
| S1 | **APRS 1.0.1 Appendix 2** (pp. 104–106, "THE APRS SYMBOL TABLES", 29 Aug 2000) | Normative. Both tables, codes `!`–`~`, 94 each. | Local text extraction of the spec already in this box's scratchpad (`spec.txt` lines 6427–6520); the same appendix is mirrored publicly at `https://www.radiomakers.net/sites/default/files/u12/the_aprs_symbol_tables.pdf` |
| S2 | **`symbolsX.txt`, 25 Nov 2015** — Bruninga's *master* one-line-per-symbol index | Supersedes S1 where they differ (25 years of amendments, chronology included in the file) | `https://raw.githubusercontent.com/wb2osz/direwolf/master/data/symbolsX.txt` (Direwolf ships a verbatim copy of `aprs.org/symbols/symbolsX.txt`) — 20 863 bytes |
| S3 | **`symbols-new.txt`, 17 Mar 2021** — "APRS SYMBOL OVERLAY and EXTENSION TABLES in APRS 1.2" | The overlay/extension list: which `<char><code>` combinations mean what | `https://raw.githubusercontent.com/wb2osz/direwolf/master/data/symbols-new.txt` — 18 828 bytes |
| S4 | **`hessu/aprs-symbol-index`** `symbols.csv` | aprs.fi's cleaned, application-ready description list (185 rows). Used **only as a cross-check**, not copied — see the licence note in §3. | `https://raw.githubusercontent.com/hessu/aprs-symbol-index/master/symbols.csv` |
| S5 | **`g4klx/ircDDBGateway`** `Common/APRSWriter.cpp` | The actual C++ that *emits* the `Da` / `D&` packets measured on this box | `https://raw.githubusercontent.com/g4klx/ircDDBGateway/master/Common/APRSWriter.cpp` |
| S6 | **`Xastir/Xastir`** `COPYING`, `symbols/symbols.dat` | Licence + symbol data of the classic X client | `https://raw.githubusercontent.com/Xastir/Xastir/master/…` |

**Cross-check method.** I parsed S1 and S2 mechanically into `{code: description}` dicts (94 + 94
each, no gaps) and diffed all three of S1/S2/S4 code-by-code. They agree on **170 of 188** codes.
The 18 disagreements are all *real amendments* recorded in S2's own change log (e.g. `/[` Jogger →
Human/Person, 23 Jun 15; `/r` Antenna → Repeater, Feb 07; `/]` PBBS → Mail/Post Office, 7 Dec 04),
not transcription errors — S2 is right and S1 is stale. Two S1 rows needed manual repair because the
PDF text layer mangles them: the `/9`–`\9` row is split across four lines, and APRS101 typesets the
apostrophe and backtick codes as the *smart quotes* `’` and `‘`, which collapse to the same character
on extraction (`/’` = Small Aircraft, `/‘` = Dish Antenna).

Where S1 and S2 differ, **the table below follows S2** and flags the change.

---

## 1. The tables, as Python

Descriptions are re-worded by me from S1/S2 into short UI labels (sentence case, per
`DESIGN.md` §Voice). `None` means **no standard meaning** — that is the trigger for the fallback
rule in §5, and it is deliberate that these are `None` and not `""`.

```python
# APRS symbol tables. Sources: APRS Protocol Reference 1.0.1 Appendix 2 (29 Aug 2000);
# aprs.org/symbols/symbolsX.txt (master index, 25 Nov 2015). None = no standard meaning.

APRS_PRIMARY = {
    "!": "Police / sheriff",
    '"': None,                      # reserved (was rain)
    "#": "Digipeater",
    "$": "Phone",
    "%": "DX cluster",
    "&": "HF gateway",
    "'": "Small aircraft",          # SSID -7 convention (S2: -11)
    "(": "Mobile satellite station",
    ")": "Wheelchair (accessible)", # added 29 Jan 04
    "*": "Snowmobile",
    "+": "Red Cross",
    ",": "Boy Scouts",
    "-": "House (VHF home station)",
    ".": "X",
    "/": "Red dot",
    "0": "Numbered circle 0",       # /0-/9 obsolete; use \\0 with an overlay
    "1": "Numbered circle 1",
    "2": "Numbered circle 2",
    "3": "Numbered circle 3",
    "4": "Numbered circle 4",
    "5": "Numbered circle 5",
    "6": "Numbered circle 6",
    "7": "Numbered circle 7",
    "8": "Numbered circle 8",
    "9": "Numbered circle 9",
    ":": "Fire",
    ";": "Campground / portable operation",
    "<": "Motorcycle",              # SSID -10
    "=": "Railroad engine",
    ">": "Car",                     # SSID -9
    "?": "File server",
    "@": "Hurricane predicted path",
    "A": "Aid station",
    "B": "BBS",
    "C": "Canoe",
    "D": None,
    "E": "Eyeball (live event)",
    "F": "Farm vehicle / tractor",  # added 28 Sep 05
    "G": "Grid square (6 character)",
    "H": "Hotel",
    "I": "TCP/IP network station",
    "J": None,
    "K": "School",
    "L": "Logged-on PC user",       # added Jan 03
    "M": "MacAPRS",
    "N": "NTS station",
    "O": "Balloon",                 # SSID -11
    "P": "Police car",
    "Q": None,
    "R": "Recreational vehicle",    # SSID -13
    "S": "Space shuttle",
    "T": "SSTV",
    "U": "Bus",                     # SSID -2
    "V": "ATV (amateur television)",
    "W": "National Weather Service site",
    "X": "Helicopter",              # SSID -6
    "Y": "Yacht (sailboat)",        # SSID -5
    "Z": "WinAPRS",
    "[": "Person",                  # S1 said "Jogger"; renamed 23 Jun 15
    "\\": "DF triangle",
    "]": "Mail / post office",      # S1 said "PBBS"; renamed 7 Dec 04
    "^": "Large aircraft",
    "_": "Weather station",
    "`": "Dish antenna",
    "a": "Ambulance",               # SSID -1
    "b": "Bicycle",                 # SSID -4
    "c": "Incident command post",   # added 29 Jan 04
    "d": "Fire station",            # S1 said "Dual Garage (Fire Department)"
    "e": "Horse / equestrian",
    "f": "Fire truck",              # SSID -3
    "g": "Glider",
    "h": "Hospital",
    "i": "IOTA (islands on the air)",
    "j": "Jeep",                    # SSID -12
    "k": "Truck",                   # SSID -14
    "l": "Laptop",                  # added Jan 03
    "m": "Mic-E repeater",
    "n": "Node",
    "o": "Emergency operations centre",
    "p": "Dog / rover",
    "q": "Grid square (above 128 m)",
    "r": "Repeater",                # S1 said "Antenna"; renamed Feb 07
    "s": "Ship (power boat)",       # SSID -8
    "t": "Truck stop",
    "u": "Truck (18-wheeler)",
    "v": "Van",                     # SSID -15
    "w": "Water station",
    "x": "X-APRS (Unix)",
    "y": "Yagi at QTH",
    "z": None,                      # "TBD" in S2; see §2 — legacy sets draw a shelter
    "{": None,
    "|": None,                      # reserved: TNC stream switch
    "}": None,
    "~": None,                      # reserved: TNC stream switch
}

APRS_ALTERNATE = {
    "!": "Emergency",
    '"': None,                      # reserved
    "#": "Digipeater (green star)",
    "$": "Bank or ATM",
    "%": "Power plant",
    "&": "Gateway station",         # S1 said "HF Gateway (diamond)"; generalised 29 Jan 04
    "'": "Crash / incident site",
    "(": "Cloudy",
    ")": "Firenet MEO / MODIS Earth observation",
    "*": "Snow",
    "+": "Church",
    ",": "Girl Scouts",
    "-": "House (HF)",
    ".": "Ambiguous / indeterminate position",
    "/": "Waypoint destination",    # added 5 Jan 04
    "0": "Circle (IRLP / EchoLink / WIRES)",
    "1": None,
    "2": None,
    "3": None,
    "4": None,
    "5": None,
    "6": None,
    "7": None,
    "8": "Network node (802.11)",
    "9": "Gas station",
    ":": "Hail",
    ";": "Park / picnic area",
    "<": "Advisory (single red flag)",
    "=": None,                      # available overlay group
    ">": "Car (top view)",
    "?": "Information kiosk",
    "@": "Hurricane / tropical storm",
    "A": "Box",
    "B": "Blowing snow",
    "C": "Coast Guard",
    "D": "Depot",                   # S1 said "Drizzle"; re-purposed Aug 14
    "E": "Smoke / visibility",
    "F": "Freezing rain",
    "G": "Snow shower",
    "H": "Haze / hazard",
    "I": "Rain shower",
    "J": "Lightning",
    "K": "Kenwood HT",
    "L": "Lighthouse",
    "M": "MARS",                    # added 8 Sep 04; A=Army N=Navy F=Air Force
    "N": "Navigation buoy",
    "O": "Rocket / balloon",
    "P": "Parking",
    "Q": "Earthquake",
    "R": "Restaurant",
    "S": "Satellite / PACSAT",
    "T": "Thunderstorm",
    "U": "Sunny",
    "V": "VORTAC navigation aid",
    "W": "NWS site",
    "X": "Pharmacy",
    "Y": "Radio / APRS device",     # was blank in S1
    "Z": None,
    "[": "Wall cloud / person",
    "\\": "GPS / navigation device",
    "]": None,
    "^": "Aircraft (top view)",
    "_": "Weather station with digipeater",
    "`": "Rain",
    "a": "Diamond — organisation / affiliation",
    "b": "Blowing dust / sand",
    "c": "CD triangle (RACES / CERT / SATERN)",
    "d": "DX spot",
    "e": "Sleet",
    "f": "Funnel cloud",
    "g": "Gale (two red flags)",
    "h": "Store / hamfest",         # S1 said "Ham Store"; generalised
    "i": "Point of interest",
    "j": "Work zone",
    "k": "Special vehicle (SUV / ATV / 4x4)",  # was blank in S1
    "l": "Area symbol",
    "m": "Value signpost (3-digit)",
    "n": "Triangle",
    "o": "Small circle",
    "p": "Partly cloudy",
    "q": None,
    "r": "Restrooms",
    "s": "Ship / boat (top view)",
    "t": "Tornado",
    "u": "Truck",
    "v": "Van",
    "w": "Flooding / avalanche / landslide",
    "x": "Wreck or obstruction",    # added 18 Oct 06
    "y": "Skywarn",                 # added 29 Jan 04
    "z": "Shelter",                 # moved here from the primary table, 6 May 04
    "{": "Fog",
    "|": None,                      # reserved: TNC stream switch
    "}": None,
    "~": None,                      # reserved: TNC stream switch
}

# Alternate codes that carry a meaningful overlay family (S2 marks these "#" or "O").
# An overlay on any other alternate code is legal but has no agreed meaning.
APRS_OVERLAYABLE = set("!#%&'()-0<=>8;ADHMORWY[\\^_ackhinsuwz")
```

### 1a. Documented overlay combinations (from `symbols-new.txt`, 17 Mar 2021)

Keyed by the **transmitted two characters** (`table_or_overlay + code`), so a lookup is a single
dict hit. This is the whole documented set; anything not here is a legal-but-undocumented overlay.

```python
APRS_OVERLAY = {
    # Aircraft (\^)
    "A^": "Autonomous aircraft", "D^": "Drone", "E^": "Electric aircraft",
    "H^": "Hovercraft", "J^": "Jet", "M^": "Missile", "P^": "Propeller aircraft",
    "R^": "Remotely piloted aircraft", "S^": "Solar-powered aircraft",
    "V^": "VTOL aircraft", "X^": "Experimental aircraft",
    # Currency (\$)
    "U$": "US dollars", "L$": "British pounds", "Y$": "Japanese yen",
    # Diamond / affiliation (\a)
    "Aa": "ARES", "Da": "D-STAR", "Ga": "RSGB", "Ra": "RACES",
    "Sa": "SATERN (Salvation Army)", "Wa": "Winlink", "Ya": "Yaesu C4FM repeater",
    # Balloons (\O)
    "BO": "Blimp", "MO": "Manned balloon", "TO": "Tethered balloon",
    "CO": "Constant-pressure balloon", "RO": "Rockoon", "WO": "Round-the-world balloon",
    # Box (\A)
    "9A": "Mobile DTMF user", "7A": "HT DTMF user", "HA": "House DTMF user",
    "EA": "EchoLink DTMF report", "IA": "IRLP DTMF report", "RA": "RFID report",
    "AA": "AllStar DTMF report", "DA": "D-STAR report", "XA": "OLPC XO laptop",
    # Buildings (\h)
    "Ch": "Ham radio club", "Eh": "Electronics store", "Fh": "Hamfest",
    "Hh": "Hardware store",
    # Cars (\>)
    "3>": "Tesla Model 3", "B>": "Battery EV", "D>": "DIY vehicle", "E>": "Ethanol",
    "F>": "Fuel cell / hydrogen", "H>": "Hybrid", "L>": "Nissan Leaf",
    "P>": "Plug-in hybrid", "S>": "Solar powered", "T>": "Tesla", "V>": "Chevy Volt",
    "X>": "Tesla Model X",
    # Civil defence triangle (\c)
    "Dc": "Decontamination", "Rc": "RACES", "Sc": "SATERN mobile canteen",
    # Depots (\D)
    "AD": "Airport", "FD": "Ferry landing", "HD": "Heliport", "RD": "Rail depot",
    "BD": "Bus depot", "LD": "Light rail / subway", "SD": "Seaport",
    # Digipeaters (\#)
    "1#": "WIDE1-1 digipeater", "A#": "Alternate-input digipeater",
    "E#": "Emergency-powered digipeater", "I#": "IGate (digipeater with IGate)",
    "L#": "Path-length-trapping digipeater", "P#": "PacComm digipeater",
    "S#": "SSn-N digipeater", "X#": "Experimental digipeater",
    "V#": "Viscous digipeater", "W#": "WIDEn-N + SSn-N trapping digipeater",
    # Emergency (\!)
    "E!": "ELT or EPIRB", "V!": "Volcanic eruption / lava",
    # Visibility (\E)
    "HE": "Haze", "SE": "Smoke", "BE": "Blowing snow", "DE": "Blowing dust / sand",
    "FE": "Fog",
    # Gateways (\&)
    "I&": "IGate (generic)", "L&": "LoRa IGate", "R&": "Receive-only IGate",
    "P&": "PSKmail node", "T&": "TX IGate (1 hop)", "W&": "Wires-X",
    "2&": "TX IGate (2 hops)",
    # GPS devices (\\)
    "A\\": "Avmap G5",
    # Hazards (\H)
    "MH": "Methane hazard", "RH": "Radiation detector", "WH": "Hazardous waste",
    "XH": "Skull and crossbones",
    # Humans (\[)
    "B[": "Baby on board", "S[": "Skier", "R[": "Runner", "H[": "Hiker",
    # Houses / power (\-)
    "5-": "House, 50 Hz", "6-": "House, 60 Hz", "B-": "Off-grid / battery",
    "C-": "Combined renewables", "E-": "Emergency power", "G-": "Geothermal",
    "H-": "Hydro powered", "O-": "Operator present", "S-": "Solar powered",
    "W-": "Wind powered",
    # Incident sites (\')
    "A'": "Car crash site", "H'": "Hazardous incident", "M'": "Multi-vehicle crash",
    "P'": "Pileup", "T'": "Truck wreck",
    # Numbered circles (\0)
    "A0": "AllStar node", "E0": "EchoLink node", "I0": "IRLP repeater",
    "S0": "Staging area", "V0": "EchoLink + IRLP (VoIP)", "W0": "Yaesu WIRES",
    # Network nodes (\8)
    "88": "802.11 node", "G8": "802.11g node",
    # Portable (\;)
    "F;": "Field Day", "I;": "Islands on the Air", "S;": "Summits on the Air",
    "W;": "WOTA",
    # Power plants (\%)
    "C%": "Coal", "E%": "Emergency generation", "G%": "Gas turbine",
    "H%": "Hydroelectric", "N%": "Nuclear", "P%": "Portable generation",
    "R%": "Renewable", "S%": "Solar", "T%": "Geothermal", "W%": "Wind",
    # Rail (\=)
    "B=": "Trolley / streetcar", "C=": "Commuter rail", "D=": "Diesel",
    "E=": "Electric", "F=": "Freight", "G=": "Gondola", "H=": "High-speed rail",
    "I=": "Inclined rail", "L=": "Elevated rail", "M=": "Monorail",
    "P=": "Passenger", "S=": "Steam", "T=": "Terminal / station", "U=": "Subway",
    "X=": "Excursion",
    # Restaurants (\R)
    "7R": "7-Eleven", "KR": "KFC", "MR": "McDonald's", "TR": "Taco Bell",
    # Radios / devices (\Y)
    "AY": "Alinco", "BY": "Byonics", "IY": "Icom", "KY": "Kenwood", "YY": "Yaesu",
    # Special vehicles (\k)
    "4k": "4x4", "Ak": "ATV (all-terrain vehicle)",
    # Shelters (\z)
    "Cz": "Clinic", "Ez": "Emergency power shelter", "Gz": "Government building",
    "Mz": "Morgue", "Tz": "Triage",
    # Ships (\s)
    "6s": "Shipwreck", "Bs": "Pleasure boat", "Cs": "Cargo ship", "Ds": "Dive boat",
    "Es": "Medical transport", "Fs": "Fishing boat", "Hs": "High-speed craft",
    "Js": "Jet ski", "Ls": "Law enforcement boat", "Ms": "Military vessel",
    "Os": "Oil rig", "Ps": "Pilot boat", "Qs": "Torpedo", "Ss": "Search and rescue",
    "Ts": "Tug", "Us": "Submarine", "Ws": "Wing-in-ground effect craft",
    "Xs": "Passenger ferry", "Ys": "Sailing ship",
    # Trucks (\u)
    "Bu": "Bulldozer / backhoe", "Gu": "Gas truck", "Pu": "Snowplough",
    "Tu": "Tanker", "Cu": "Chlorine tanker", "Hu": "Hazardous cargo",
    # Water (\w)
    "Aw": "Avalanche", "Gw": "Flood gauge (green)", "Mw": "Mudslide",
    "Nw": "Flood gauge (normal)", "Rw": "Flood gauge (red)", "Sw": "Snow blockage",
    "Yw": "Flood gauge (yellow)",
}
```

### 1b. The resolver

```python
def resolve_symbol(table: str, code: str) -> tuple[str | None, str | None]:
    """Return (label, overlay_char). label is None when nothing standard applies."""
    if table == "/":
        return APRS_PRIMARY.get(code), None
    if table == "\\":
        return APRS_ALTERNATE.get(code), None
    # Anything else: alternate table, `table` drawn as an overlay on the icon (APRS101 ch.20 p.91).
    combo = APRS_OVERLAY.get(table + code)
    if combo:
        return combo, table
    base = APRS_ALTERNATE.get(code)
    if base:
        return f"{base} ({table} overlay)", table
    return None, table
```

Note the compressed-position wrinkle already recorded in `APRS_PAYLOAD_DECODING.md` §3: in a
compressed report the overlay is transmitted as `a`–`j` meaning `0`–`9`, so map it back before the
lookup. It is dead code on this box's traffic today but costs two lines.

---

## 2. The 15 measured symbols, resolved

| On air | Table / code | Reads as on a card | Basis |
|---|---|---|---|
| `` /` `` (43) | primary `` ` `` | **Dish antenna** | S1 + S2 agree, unchanged since 2000 |
| `/_` (23) | primary `_` | **Weather station** | S1 + S2 agree |
| `/S` (18) | primary `S` | **Space shuttle** | S1 "Space Shuttle" / S2 "SHUTTLE" |
| `/z` (15) | primary `z` | **Shelter** — but label it *"Shelter (legacy `/z`; the current table leaves it undefined)"* | see below |
| `/r` (14) | primary `r` | **Repeater** | **S1 is stale.** S1 says "Antenna"; S2 says `/r LR  Repeater  (Feb 07)`. Fits KD4WLE's 442.850 object exactly |
| `/-` (12) | primary `-` | **House (VHF home station)** | S1 + S2 agree ("House QTH (VHF)") |
| `D&` (8) | alternate `&`, `D` overlay | **D-STAR gateway** | verified, see below |
| `Da` (6) | alternate `a`, `D` overlay | **D-STAR** (repeater module / hotspot) | S3: `Da = DSTAR (had been ARES Dutch)` |
| `Wa` (4) | alternate `a`, `W` overlay | **Winlink gateway** | S3: `Wa = WinLink`; S2 chronology `3 Jan 05: Added W overlay to "\a" symbol to indicate WinLINK` |
| `I#` (4) | alternate `#`, `I` overlay | **IGate** (digipeater with an internet gateway) | S3: `I# - I-gate equipped digipeater`; S2 chronology `16 Jun 06: Suggest I for 2-way IGate and R overlay for RX only` |
| `/[` (3) | primary `[` | **Person** | **S1 is stale.** S1 says "Jogger"; S2 renamed it `Human/Person` on 23 Jun 15 |
| `/?` (3) | primary `?` | **File server** | S1 "File Server" / S2 "SERVER for Files" |
| `/k` (3) | primary `k` | **Truck** | S1 + S2 agree; SSID −14 convention matches W1LBR-9… (note: −9 is the *car* SSID, so this station's SSID and symbol disagree — the symbol is what the sender chose, so report the symbol) |
| `/>` (2) | primary `>` | **Car** | S1 + S2 agree; SSID −9 matches |
| `/$` (2) | primary `$` | **Phone** | S1 + S2 agree |

### The three questions, settled

**Is `/$` really "Bank or ATM"?** **No — `/$` is Phone.** "Bank or ATM (green box)" is `\$`, the
*alternate*-table `$`. S3 states the pair explicitly:

```
ATM Machine or CURRENCY:  #$
/$ = original primary Phone
\$ = Bank or ATM (generic)
```

S1 (p. 104) has the same split: `/$ BEV 04 Phone` … `\$ OEV 04 Bank or ATM (green box)`. KC3EFJ at
33 knots is a **phone**, which is exactly right for a BTECH UV-PRO — an Android handheld running an
APRS app. There is no puzzle here; the confusion is purely primary-vs-alternate.

**Is `/z` really undefined in APRS101 and later assigned?** *Undefined: yes. Later assigned: no —
it was assigned earlier and then moved away.* The full history:

- **S1 (2000)**: the `/z` cell is blank. `\z` is blank too.
- **APRS 1.1 era**: `\z = Shelter (with overlay) (A red house with peaked roof)` is added
  (S2 chronology, `29 Jan 04`), and then — the decisive line — `06 May 04 to move Shelter(overlay)
  from PRI to ALT table`. So shelter *was* on `/z` in practice, and was deliberately moved off it.
- **S2 (2015, the master list)**: `/z LZ  TBD` and `\z SZ# OVERLAYED Shelter`.
- **S3 (2021)**: the SHELTERS family is `#z`, and it reads `/z = was available`, `\z = overlayed
  shelter`, with all five assignments (`Cz` clinic, `Ez` emergency power, `Gz` government building,
  `Mz` morgue, `Tz` triage) on the **alternate** row.
- **Deployed reality**: every shipped graphics set — WA8LMF Rev H, Xastir, UI-View, aprs.fi — still
  *draws* a shelter at `/z`, and hessu's index (S4) still labels `/z` "Shelter", because the bitmap
  was never removed.

So `docs/research/APRS_PAYLOAD_DECODING.md` §2 is right that `/z` is blank in APRS101, but its
gloss — *"`symbols-new.txt` later uses the `z` row for shelters"* — is off by one table: the shelter
row is `\z`, not `/z`. **Recommended card text: "Shelter" with the qualifier "legacy `/z` — the
current master table lists it as TBD."** That is honest in both directions: it names the icon every
other client on the air is drawing, and it does not assert a standard meaning that does not exist.
KD4WLE's `N4TDX-2` object with the comment "Mims Auxcom" (an auxiliary-communications shelter) fits.

**`\&`, `\a`, `\#` and their D / W / I overlays — verified, not confirmed.**

- **`\#` + `I` = IGate.** Two independent statements. S2's `\#` row reads `OD# OVERLAY DIGI (green
  star)` — the base symbol is a digipeater, overlayable. S3's DIGIPEATERS family lists
  `I# - I-gate equipped digipeater`, and S2's change log has `16 Jun 06: Suggest I for 2-way IGate
  and R overlay for RX only`. Note the precise meaning: **an I-gate that is also a digipeater** —
  the base glyph is the digi star, the `I` says it also gates to the internet. N4TDX being this
  box's IGate is consistent. ✔ (guess correct)
- **`\a` + `W` = Winlink.** S3: `Wa = WinLink` in the "ARRL or DIAMOND: #a" family. S2 change log:
  `3 Jan 05: Added W overlay to "\a" symbol to indicate WinLINK`. ✔ (guess correct)
- **`\&` + `D` = D-STAR gateway — but this one is *not* in the documented list.** S3's GATEWAYS
  family (`#&`) documents only `I&`, `L&`, `R&`, `P&`, `T&`, `W&` and `2&`. `D&` appears in neither
  S2 nor S3. It is a *de facto* assignment, and the proof is in the software that generates the
  exact packets on this box — `ircDDBGateway`, `Common/APRSWriter.cpp`:

  ```cpp
  // the gateway itself, as an object, symbol "Da":
  output.Printf(wxT("%s-S>APDG01,TCPIP*,qAC,%s-GS:;%-7s%-2s*%02d%02d%02dz%s%cD%s%caRNG…"), …);

  // each repeater module <call>-A/B/C, as a position, symbol "D&":
  output.Printf(wxT("%s-%s>APDG02,TCPIP*,qAC,%s-%sS:!%s%cD%s%c&RNG…"), …);
  ```

  Read the format strings: after longitude + `E/W` the first emits the literal `a` and the second
  the literal `&`, both preceded by the literal `D` after latitude + `N/S`. That is `Da` and `D&`,
  hard-coded. And it matches the measurement precisely: **`Da` is sent by the `-S` gateway station,
  `D&` by the `-A`/`-B`/`-C` module stations** — exactly the KM4OSL-S/N1MPR-S vs KM4OSL-C/N1MPR-C
  split observed. The tocalls corroborate: `APDG01`/`APDG02` are ircDDBGateway's.

  So: ✔ the guess is right in substance, with the caveat that **`D&` is convention, not
  specification.** Card text: *"D-STAR gateway"* for `D&` and *"D-STAR"* for `Da`, and the detail
  view should be willing to say `D` overlay on `\&` (gateway station) so a reader can check.

  Worth noting for the card: `\&`'s meaning was broadened on 29 Jan 04 from "HF Gateway" to
  **"is not just HF, but ANY GATEWAY with overlay character"** (S2 change log). Calling `D&`
  "HF Gateway", as APRS101 would, is now actively wrong.

---

## 3. Rendering — the four options

The constraints that decide this, from `docs/reference/DESIGN.md`:

- §Iconography: *"One outline set (Lucide), 1.5px stroke, 20px in controls / 24px in tiles and nav.
  No filled icons except the status dot. **No emoji in UI chrome**."*
- §Iconography, entity icons: glyph *"rendered in a round disc tinted by the type's accent"* —
  there is already a house pattern for "small semantic glyph beside a row".
- §Color tokens: accents are *"muted, desaturated pastels — never saturated/neon"*.
- §Accessibility: *"Status conveyed by dot color is always paired with text."*
- App constraint: offline-capable PWA, no external asset loading.

### Option A — embed a third-party sprite sheet (hessu/aprs-symbols, aprs.fi's set)

**Licence — the decisive finding.** The repo has no `LICENSE` file; it has `COPYRIGHT.md`, and the
set is **not uniformly licensed**. It defines two shorthands:

> `*VEC-OH7LZB*` - Vectorized by OH7LZB, based on original APRS symbol set
>   * Source of original bitmap: http://wa8lmf.net/aprs/APRS_symbols.htm
>   * Original designer of individual symbol unknown at this time, but one of:
>     * Roger Barker, G4IDE / Steve Dimse, KH4G / Stephen Smith, WA8LMF
>   * **Licensing: Unknown**

> `*OH7LZB*` - Original vector design by Heikki Hannikainen, OH7LZB
>   * **License: CC BY-SA 2.0**

**69 of the entries are `VEC-OH7LZB`, i.e. explicitly "Licensing: Unknown."** The file also says:

> The original symbols do not come with any information on their licensing. They've been distributed
> with a lot of APRS software over time, but I don't know who designed which symbol originally.

and:

> Some symbols are vectorized versions of product or brand logos. The copyright of those is owned by
> the respective companies (Apple, Microsoft, Kenwood), and each of those may have some opinions on
> how the logos are used. **Please check for yourself if you can use them or not.**

So the widely-repeated summary "hessu/aprs-symbols is CC BY-SA" — including the one in
`docs/research/APRS_PAYLOAD_DECODING.md` §2 — **is not accurate and should be corrected**: the
CC BY-SA 2.0 grant covers only the subset Hessu considers his own original work; the largest group
is unlicensed derivative work from an unlicensed original, and a handful are third-party trademarks.

**Second correction:** the same doc says the repo carries SVG. It does not. The repo ships
`aprs-symbols.ai` (2 783 809 bytes, Adobe Illustrator) plus PNG sprite sheets. There is no `svg/`
directory (probed: 404). The README confirms: *"The symbol set is published in both vector (Adobe
Illustrator/PDF) and raster (PNG) formats."* Converting the `.ai` is a manual Illustrator/Inkscape
job, and the README warns the file uses Illustrator effects (blur, shadow) that may not survive.

| | |
|---|---|
| **Size** | 1× 24px sheets: 53 735 + 43 979 + 11 744 = **~107 KB**; @2× (48px, needed for retina): **~228 KB**. Already-compressed PNG, so no gzip win. |
| **Dark theme** | Bad. The set is full-colour raster with drop shadows and white fills tuned for a light map background. On `--bg #0E0F11` the white-cored digi star and the blue WX station glow; nothing can be recoloured, and it collides head-on with the "muted, desaturated pastels — never saturated/neon" rule. |
| **Overlays** | Supported in principle — sheet `-2` is the overlay characters — but as a *third* composited raster layer, and the README admits *"The aprs.fi symbol graphics set does not contain additional symbols for overlays yet."* |
| **Accessibility** | Sprite via `background-position` on a `<span>`; needs an explicit `aria-label` either way. No worse, no better. |
| **Verdict** | **Reject.** Unclear licence on the majority of the artwork, brand logos inside it, wrong visual register for a dark-first desaturated system, 107–228 KB of raster in an offline bundle, and vector work required to use it properly anyway. |

*(Xastir's set, `symbols/symbols.dat`, 97 601 bytes, is the same lineage: Xastir is **GPL v2**
(`COPYING`), which is copyleft, and the underlying artwork is the same unattributed WA8LMF/UI-View
material. Same objections plus a licence that propagates.)*

### Option B — draw our own inline SVG glyph set

| | |
|---|---|
| **Licence** | Ours. No attribution, no share-alike, no trademark exposure. The 100+ codes we do *not* draw cost nothing (§5 fallback). |
| **Size** | ~200–320 bytes of path data per glyph. The 15 measured symbols ≈ **4 KB**; a generous 40-glyph set covering everything plausible on this box ≈ 11 KB raw, ~3 KB gzipped; all 188 would be ~47 KB raw / ~12 KB gzipped. It is a JS/TSX module, so it tree-shakes and is in the service-worker precache automatically. |
| **Dark theme** | Perfect by construction — `stroke="currentColor"`, no fills, so the glyph is whatever `--text` / accent token the row gives it, and it flips with the theme for free. |
| **Overlays** | Native. The overlay character is an SVG `<text>` node placed inside the shape, which is exactly what APRS does. Font size, weight and position are ours to tune, so it stays legible at 20 px instead of being a 24×24 raster stamped on a 24×24 raster. |
| **Accessibility** | Best. `role="img"` + `<title>` on the SVG gives every icon a first-class text alternative that reads correctly, and the label string comes from the same dict that drives the tooltip and the detail row — one source, no drift. |
| **Cost** | The drawing. 15 glyphs for day one (§4, done below). |
| **Verdict** | **Recommended.** |

### Option C — an icon font

Build a WOFF2 (IcoMoon/Fontello) with one glyph per symbol. ~15–25 KB for 188 glyphs, and it does
inherit `color`. But: overlays require stacking two glyphs with negative margins and hoping the
metrics line up; screen readers announce PUA codepoints as garbage unless every use is
`aria-hidden` + a visually-hidden label (so you write the label anyway); a font is an opaque binary
in the repo that nobody can diff or fix without re-running a toolchain; and it adds a build step and
a FOIT/FOUT flash on a PWA cold start. **Reject** — strictly worse than B on every axis that matters
here, and no smaller once B is gzipped.

### Option D — emoji

**Rejected by the design system before any other argument**: `DESIGN.md` §Iconography says
*"No emoji in UI chrome"*, and `CLAUDE.md` operating guidance forbids emoji in output generally.
Beyond the rule: emoji render as full-colour vendor art (saturated, platform-specific, different on
Android/iOS/desktop, unaffected by `currentColor`), there is no emoji for "digipeater with an IGate
overlay" or "value signpost", the overlay character cannot be composited onto one, and the
screen-reader name is the *emoji's* name (🛰 "satellite") not the *symbol's* meaning, so the text
alternative would be wrong rather than merely absent. **Reject.**

### Recommendation

**Draw our own inline SVG set (Option B), monochrome on `currentColor`, in the existing entity-disc
pattern**, and drive both the glyph and its text alternative from the `APRS_PRIMARY` /
`APRS_ALTERNATE` / `APRS_OVERLAY` dicts in §1.

Three supporting points:

1. **The label dict and the glyph set are independent.** Every one of the ~188 codes gets a correct
   *label* from day one (§1 is complete); only the ~15–40 that actually appear need a *drawing*.
   Everything else falls back honestly (§5) while still showing the right words. That decouples
   "know what it is" from "have art for it", which is what makes the incremental cost near zero.
2. **The app already ships this exact mechanism.** `frontend/src/components/icons.tsx` (474 lines,
   62 icons) opens with *"Lucide-style outline icons, inlined to avoid an icon-font dependency.
   1.5px stroke per docs/reference/DESIGN.md 'Iconography'; size set by the caller."* — and exports
   a shared `<Icon>` wrapper that is `viewBox="0 0 24 24"`, `fill="none"`, `stroke="currentColor"`,
   `strokeWidth="1.5"`, round caps and joins. That is precisely the contract the glyphs in §4 are
   drawn to, so they are `<Icon>` children and nothing else changes: no new dependency, no new
   build step, no second icon idiom, and the APRS row is visually identical to the rest of the app —
   which is what §Iconography's "one outline set" rule exists to enforce. (Note that Lucide is *not*
   an npm dependency here; the house set is hand-inlined in Lucide's style. Lucide upstream is
   ISC-licensed if a path is ever lifted from it, but nothing in §4 needs to be.) Several of the 62
   are already close enough to reuse outright — `PersonIcon` for `/[`, `RadioIcon` for `/r`,
   `GlobeIcon` for a generic gateway.
3. **Colour carries the axis that monochrome loses.** Two of the 15 (`\&` and `\a`) are *the same
   diamond* in the standard set, distinguished in the original artwork only by colour. Rendering
   them in the entity-disc pattern — `background: color-mix(in srgb, var(--accent) 16%, transparent)`,
   glyph in `var(--accent)` — recovers that: `--steel` for infrastructure (gateway, digi, IGate),
   `--green` for stations/people/vehicles, `--amber` for weather, `--slate` for unknown. Paired,
   as `DESIGN.md` §Accessibility requires, with a text alternative on every icon.


**One integration wrinkle.** The shared `Icon` wrapper hardcodes `aria-hidden="true"`, which is
right for the decorative chrome it was built for and wrong here: an APRS symbol *is* content, and
`DESIGN.md` §Accessibility requires the text pairing. Two ways out, in order of preference:

- **Label the disc, not the glyph** — keep `Icon` untouched, and put `role="img"` +
  `aria-label={label}` on the wrapping `<span class="aprs-sym">`. Zero change to shared code.
- **Or** give `Icon` an optional `label?: string` that, when present, swaps `aria-hidden` for
  `role="img"` and a `<title>` child. Cleaner long-term, but it touches a component 62 icons depend on.

Either way the string comes from the same `APRS_PRIMARY`/`APRS_ALTERNATE`/`APRS_OVERLAY` lookup that
produces the visible label, so the accessible name and the on-screen text can never drift apart.

**Cost to add a symbol later:** one line in the label dict (`"\\y": "Skywarn"`) — and that alone is
enough to ship, because the glyph falls back. If it deserves art, one ~250-byte SVG entry in the
glyph map, or one Lucide import. No sprite sheet to regenerate, no font to rebuild, no asset
pipeline, no licence review.

---

## 4. The 15 glyphs

These are written standalone so they can be read on their own, but each one's children drop
straight into the existing `<Icon>` wrapper in `frontend/src/components/icons.tsx` — the wrapper
already supplies every attribute below except `role`/`<title>` (see the integration note in §3).

Conventions for all of them: `viewBox="0 0 24 24"`, rendered at **20 px** (24 px if the packet row
uses the tile scale), `fill="none"`, `stroke="currentColor"`, `stroke-width="1.5"`,
`stroke-linecap="round"`, `stroke-linejoin="round"`, `role="img"` with a `<title>` that is the label
from §1. Overlay characters are `<text>` nodes with `stroke="none" fill="currentColor"` (the parent
stroke must be cancelled or the letter renders outlined and muddy at this size).

```jsx
// 1. /`  Dish antenna  (43 pkt — KD4WLE)
<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor"
     stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" role="img">
  <title>Dish antenna</title>
  <path d="M4 11a7 7 0 0 0 9 9Z"/>
  <path d="M9 16l3.5-3.5"/>
  <path d="M15 12a4 4 0 0 0-4-4"/>
  <path d="M19 12a8 8 0 0 0-8-8"/>
</svg>

// 2. /_  Weather station  (23 pkt — KD4WLE, W4EDL-13)
//    A thermometer: one object, unmistakable at 20px, and distinct from the two
//    mast-shaped icons (/r, /`) that would otherwise be confusable.
<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor"
     stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" role="img">
  <title>Weather station</title>
  <path d="M14 14.8V5a2 2 0 1 0-4 0v9.8a4 4 0 1 0 4 0Z"/>
  <path d="M10 8h2M10 11h2"/>
</svg>

// 3. /S  Space shuttle  (18 pkt — N1KSC-1)
<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor"
     stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" role="img">
  <title>Space shuttle</title>
  <path d="M12 2.5c2.4 2.7 3.4 6 3.4 9.3V16h-6.8v-4.2C8.6 8.5 9.6 5.2 12 2.5Z"/>
  <path d="M8.6 13.5 5.2 17.6V20l3.4-2M15.4 13.5l3.4 4.1V20l-3.4-2"/>
  <path d="M12 16v4"/>
  <circle cx="12" cy="8" r="1.1"/>
</svg>

// 4. /z  Shelter (legacy)  (15 pkt — KD4WLE)
//    Open-sided pavilion, deliberately different from the /- house below.
<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor"
     stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" role="img">
  <title>Shelter (legacy /z)</title>
  <path d="M2.5 11.5 12 4l9.5 7.5"/>
  <path d="M5.5 11.5V20M18.5 11.5V20"/>
  <path d="M3.5 20h17"/>
</svg>

// 5. /r  Repeater  (14 pkt — KD4WLE)
<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor"
     stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" role="img">
  <title>Repeater</title>
  <circle cx="12" cy="6" r="1.4"/>
  <path d="M12 7.6V20"/>
  <path d="M8.5 20 12 11.5 15.5 20"/>
  <path d="M8.6 2.6a5 5 0 0 0 0 6.8M15.4 2.6a5 5 0 0 1 0 6.8"/>
</svg>

// 6. /-  House (VHF home station)  (12 pkt — K4JTT-D, KN1B)
<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor"
     stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" role="img">
  <title>House (VHF home station)</title>
  <path d="M3 10.5 12 3.5l9 7V20a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1Z"/>
  <path d="M9.5 21v-6h5v6"/>
</svg>

// 7. D&  D-STAR gateway  (8 pkt — KM4OSL-C, N1MPR-C)
//    Base \& = gateway diamond; D drawn inside, as APRS draws overlays.
<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor"
     stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" role="img">
  <title>D-STAR gateway</title>
  <path d="M12 2.5 21.5 12 12 21.5 2.5 12Z"/>
  <text x="12" y="15.7" text-anchor="middle" font-size="10" font-weight="600"
        font-family="ui-sans-serif, system-ui, sans-serif"
        fill="currentColor" stroke="none">D</text>
</svg>

// 8. Da  D-STAR  (6 pkt — KM4OSL-S, N1MPR-S)
//    Base \a is the SAME diamond as \& in the standard set — they differ only by
//    colour there. We differ by disc tint (--violet vs --steel) plus the label.
<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor"
     stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" role="img">
  <title>D-STAR</title>
  <path d="M12 2.5 21.5 12 12 21.5 2.5 12Z"/>
  <text x="12" y="15.7" text-anchor="middle" font-size="10" font-weight="600"
        font-family="ui-sans-serif, system-ui, sans-serif"
        fill="currentColor" stroke="none">D</text>
</svg>

// 9. Wa  Winlink gateway  (4 pkt — WINLINK)
<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor"
     stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" role="img">
  <title>Winlink gateway</title>
  <path d="M12 2.5 21.5 12 12 21.5 2.5 12Z"/>
  <text x="12" y="15.7" text-anchor="middle" font-size="9.5" font-weight="600"
        font-family="ui-sans-serif, system-ui, sans-serif"
        fill="currentColor" stroke="none">W</text>
</svg>

// 10. I#  IGate  (4 pkt — N4TDX)
//     Base \# = the digi star, drawn with a wide open centre so the overlay
//     character sits inside it the way the standard set does.
<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor"
     stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" role="img">
  <title>IGate</title>
  <path d="M12 2 16.6 7.4 22 12 16.6 16.6 12 22 7.4 16.6 2 12 7.4 7.4Z"/>
  <text x="12" y="15.4" text-anchor="middle" font-size="9" font-weight="600"
        font-family="ui-sans-serif, system-ui, sans-serif"
        fill="currentColor" stroke="none">I</text>
</svg>

// 11. /[  Person  (3 pkt — KD4WLE-D)
<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor"
     stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" role="img">
  <title>Person</title>
  <circle cx="12" cy="7.2" r="3.2"/>
  <path d="M4.8 20.5a7.2 7.2 0 0 1 14.4 0"/>
</svg>

// 12. /?  File server  (3 pkt — K4JTT)
<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor"
     stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" role="img">
  <title>File server</title>
  <rect x="3" y="4" width="18" height="7" rx="1.5"/>
  <rect x="3" y="13" width="18" height="7" rx="1.5"/>
  <path d="M6.5 7.5h.01M6.5 16.5h.01"/>
</svg>

// 13. /k  Truck  (3 pkt — W1LBR-9)
<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor"
     stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" role="img">
  <title>Truck</title>
  <path d="M2 5.5h11v10H2z"/>
  <path d="M13 9h3.8l3.2 3.3v3.2h-7z"/>
  <circle cx="6.5" cy="17.6" r="1.8"/>
  <circle cx="16.8" cy="17.6" r="1.8"/>
</svg>

// 14. />  Car  (2 pkt — N0XIA-4)
<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor"
     stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" role="img">
  <title>Car</title>
  <path d="M4 15.5h16v-2.4a1.6 1.6 0 0 0-1-1.5l-2.2-.9-1.9-2.8a2 2 0 0 0-1.7-.9h-2.4a2 2 0 0 0-1.7.9L7.2 10.7l-2.2.9A1.6 1.6 0 0 0 4 13.1Z"/>
  <circle cx="7.6" cy="17.4" r="1.8"/>
  <circle cx="16.4" cy="17.4" r="1.8"/>
</svg>

// 15. /$  Phone  (2 pkt — KC3EFJ)
<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor"
     stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" role="img">
  <title>Phone</title>
  <path d="M6.6 3.5 9.4 3a1.4 1.4 0 0 1 1.5.8l1 2.3a1.4 1.4 0 0 1-.4 1.7l-1.4 1.1a11 11 0 0 0 4.6 4.6l1.1-1.4a1.4 1.4 0 0 1 1.7-.4l2.3 1a1.4 1.4 0 0 1 .8 1.5l-.5 2.8a1.4 1.4 0 0 1-1.4 1.2A15 15 0 0 1 5.4 4.9a1.4 1.4 0 0 1 1.2-1.4Z"/>
</svg>
```

### Two notes on the overlay glyphs

**The overlay character is data, not decoration.** It comes off the wire and must be escaped like
any other packet field — a station is free to transmit any of `0-9A-Z`, and a malformed frame can
put anything there. Render it through the framework's text node (JSX `{overlay}`), never
`dangerouslySetInnerHTML`, and clamp to a single character.

**`D&` and `Da` are the same diamond, and that is correct.** In every deployed set `\&` and `\a` are
both diamonds, separated only by colour. Rather than invent a shape difference (which would make
our icons wrong relative to every other client on the air), separate them with the disc tint —
`--steel` for `\&` (infrastructure: gateway, digi, IGate) and `--violet` for `\a` (organisation /
affiliation) — and with the label. `DESIGN.md` §Accessibility requires the text pairing anyway, so
the colour is a second channel, never the only one.

---

## 5. The fallback rule for unknown symbols

**The spec answers this directly.** `symbolsX.txt`, change entry of 4 Feb 2004:

> **Unassigned symbols should display the international symbol of a circle with a slash through it.
> Meaning "not"...**

That is the honest render, and it is the one other APRS clients were told to use. So:

```jsx
// Fallback: no standard meaning for this table/code pair.
<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor"
     stroke-width="1.5" stroke-linecap="round" role="img">
  <title>Unknown symbol</title>
  <circle cx="12" cy="12" r="9"/>
  <path d="M5.6 5.6 18.4 18.4"/>
</svg>
```

Rendered in the **`--slate` `#9AA0A8`** disc (the `Thing` tint from §Entity-type accents — the
"we don't know what kind of thing this is" colour the design system already has).

The rule has four cases, in this order:

| Case | Glyph | Label text |
|---|---|---|
| **Known code, glyph drawn** | the glyph | the label (`"Repeater"`) |
| **Known code, no glyph yet** | circle-slash, `--slate` disc | the label, verbatim (`"Restrooms"`) — we know *what* it is, we just haven't drawn it |
| **Unknown code** (`APRS_PRIMARY[c] is None`) | circle-slash, `--slate` disc | ``symbol `/D` — no standard meaning`` |
| **Unknown code with an overlay** | circle-slash with the overlay character beside it | ``symbol `Q]` — overlay `Q` on an undefined alternate symbol`` |

Three rules that make it honest rather than blank or wrong:

1. **Always show the two raw characters** next to or beneath the fallback icon, in the mono/tabular
   face. `/D` is a fact; "unknown" alone throws away the only information the packet carried, and it
   is what lets the owner look the code up when a new one appears on the air.
2. **Never guess from neighbours or from the station's callsign/SSID.** `APRS_PAYLOAD_DECODING.md`
   already makes this call for the general case — *"the symbol means what the sender chose, and the
   card should say what the table says, not guess"*. `/k` on W1LBR-**9** is the live proof: SSID −9
   is the *car* convention, the symbol says truck. Report the symbol.
3. **Distinguish "undefined" from "we didn't draw it".** The second case above is the common one and
   is not an error — a station with `\r` Restrooms is fully understood, it just has no art. Wording
   it the same as a genuinely undefined code would misreport 100+ perfectly standard symbols as
   broken. This is also the mechanism that makes the incremental cost of a new glyph zero.

A fifth, adjacent case worth wiring at the same time: `\.` is **"Ambiguous / indeterminate
position"** — the spec's own null-position symbol, and `APRS_PAYLOAD_DECODING.md` §"0.0, 0.0 with
symbol `\.`" already flags it. That one is *known*, and its label should say so plainly rather than
falling through to the unknown glyph.

---

## 6. Corrections this research makes to existing repo docs

Four, all in `docs/research/APRS_PAYLOAD_DECODING.md` §2 (none of them changes a decoding
decision; they change what a card should *say*):

1. **`/r` is "Repeater", not "Antenna"** (renamed Feb 07 in the master list). The §2 table and the
   `docs/research` prose both use the APRS101 wording.
2. **`/[` is "Person", not "Jogger"** (renamed 23 Jun 15).
3. **`/z`: the shelter row is `\z`, not `/z`.** §2 says *"`symbols-new.txt` later uses the `z` row
   for shelters"*; the shelter family (`Cz`/`Ez`/`Gz`/`Mz`/`Tz`) is all on the **alternate** table,
   and shelter was explicitly *moved off* the primary table on 6 May 2004. `/z` itself is `TBD`.
4. **`hessu/aprs-symbols` is not "CC BY-SA", and has no SVG.** §2 describes it as *"SVG/PNG icons +
   descriptions, CC BY-SA"*. It ships `.ai` + PNG (no SVG), and `COPYRIGHT.md` marks 69 entries
   "Licensing: Unknown", CC BY-SA 2.0 on Hessu's own originals only, plus third-party brand logos.
   The **`hessu/aprs-symbol-index`** repo (a different repo — descriptions, no artwork) *is*
   cleanly **CC BY-SA 4.0**: *"licensed under the CC BY-SA 4.0 license, so you're free to use it in
   any of your applications. For free. Just mention the source somewhere in the small print."*
   Because share-alike on a description list is an avoidable complication, the §1 tables above are
   worded from APRS101 + `symbolsX.txt` (the protocol documents this app already implements) and use
   the aprs.fi index only as a cross-check.

§2's core recommendation — *"embed a static 2×94 table lifted from Appendix 2 … plus a small overlay
table … and render an unmapped code as 'symbol `/z` — no standard meaning' rather than blank or
invented"* — is confirmed and is what §1 and §5 above implement, with the amendment that the table
should be lifted from **`symbolsX.txt` (2015)** rather than Appendix 2 (2000), because Appendix 2 is
stale on 18 of 188 codes including two of the 15 on the air.

---

## Sources

- APRS Protocol Reference 1.0.1, Appendix 2 "The APRS Symbol Tables", 29 Aug 2000 — pp. 104–106.
  Public mirror: <https://www.radiomakers.net/sites/default/files/u12/the_aprs_symbol_tables.pdf>
- `symbolsX.txt` (WB4APR master symbol index, 25 Nov 2015) — <https://www.aprs.org/symbols/symbolsX.txt>
  (origin **down**, HTTP 521); verbatim copy used:
  <https://raw.githubusercontent.com/wb2osz/direwolf/master/data/symbolsX.txt>
- `symbols-new.txt` ("APRS SYMBOL OVERLAY and EXTENSION TABLES in APRS 1.2", 17 Mar 2021) —
  <https://www.aprs.org/symbols/symbols-new.txt> (origin **down**, HTTP 521); copy used:
  <https://raw.githubusercontent.com/wb2osz/direwolf/master/data/symbols-new.txt>
- `hessu/aprs-symbols` — README and COPYRIGHT.md:
  <https://github.com/hessu/aprs-symbols> ·
  <https://raw.githubusercontent.com/hessu/aprs-symbols/master/COPYRIGHT.md>
- `hessu/aprs-symbol-index` — README (CC BY-SA 4.0) and `symbols.csv`:
  <https://github.com/hessu/aprs-symbol-index>
- `g4klx/ircDDBGateway` — `Common/APRSWriter.cpp` (emits `Da` and `D&`):
  <https://raw.githubusercontent.com/g4klx/ircDDBGateway/master/Common/APRSWriter.cpp>
- `Xastir/Xastir` — `COPYING` (GPL v2), `symbols/symbols.dat`: <https://github.com/Xastir/Xastir>
- WA8LMF "Updated APRS Symbol Set (Rev H)" — <http://wa8lmf.net/aprs/APRS_symbols.htm>
  (the origin of most deployed bitmaps; no licence statement)
- Lucide — the *style* reference named by `DESIGN.md` §Iconography (not an npm dependency here;
  `frontend/src/components/icons.tsx` inlines the house set). ISC licence, if a path is ever lifted:
  <https://github.com/lucide-icons/lucide/blob/main/LICENSE>
- Creative Commons BY-SA 4.0: <https://creativecommons.org/licenses/by-sa/4.0/> · BY-SA 2.0:
  <https://creativecommons.org/licenses/by-sa/2.0/>
- Repo: `docs/research/APRS_PAYLOAD_DECODING.md` §2, `docs/research/APRS_PACKET_DETAIL_UI.md`,
  `docs/reference/DESIGN.md` §§Iconography / Color tokens / Accessibility.
