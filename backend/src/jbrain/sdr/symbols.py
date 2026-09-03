"""What the two characters after a position mean — the APRS symbol.

A symbol is a TABLE character and a CODE character, and the table is where the subtlety
is. `/` is the primary table and `\\` is the alternate one, but **any other character is
an OVERLAY**: it selects the alternate table and is drawn on top of the icon. That is not
a corner case here — measured on the owner's box, four of the fifteen symbols on the air
are overlaid, and one of them is how the busiest station on the channel says it is an
IGate (`I#`). A resolver that treats the overlay as a table gets those wrong.

**The tables come from `aprs.org/symbols/symbolsX.txt` (the 2015 master index), not
APRS101 Appendix 2 (2000)**, which is stale on eighteen codes — two of them live on this
channel: `/r` was renamed Antenna → Repeater in 2007 and `/[` Jogger → Person in 2015.
Getting that wrong is not cosmetic: `/$` is a Phone, and reading it from the 2000 table as
"Bank or ATM" (which is `\\$`, on the OTHER table) filed a moving station as a bank.

See `docs/research/APRS_SYMBOLS.md` for the sourcing and the cross-check. `None` means the
code has no standard meaning — deliberately `None` rather than `""`, because "we do not
know" and "it is called nothing" are different answers and only one of them should reach a
reader.

Nothing here authenticates. A station chooses its own symbol and can claim any of them.
"""

from __future__ import annotations

PRIMARY = {
    "!": "Police / sheriff",
    '"': None,  # reserved (was rain)
    "#": "Digipeater",
    "$": "Phone",
    "%": "DX cluster",
    "&": "HF gateway",
    "'": "Small aircraft",  # SSID -7 convention (S2: -11)
    "(": "Mobile satellite station",
    ")": "Wheelchair (accessible)",  # added 29 Jan 04
    "*": "Snowmobile",
    "+": "Red Cross",
    ",": "Boy Scouts",
    "-": "House (VHF home station)",
    ".": "X",
    "/": "Red dot",
    "0": "Numbered circle 0",  # /0-/9 obsolete; use \\0 with an overlay
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
    "<": "Motorcycle",  # SSID -10
    "=": "Railroad engine",
    ">": "Car",  # SSID -9
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
    "L": "Logged-on PC user",  # added Jan 03
    "M": "MacAPRS",
    "N": "NTS station",
    "O": "Balloon",  # SSID -11
    "P": "Police car",
    "Q": None,
    "R": "Recreational vehicle",  # SSID -13
    "S": "Space shuttle",
    "T": "SSTV",
    "U": "Bus",  # SSID -2
    "V": "ATV (amateur television)",
    "W": "National Weather Service site",
    "X": "Helicopter",  # SSID -6
    "Y": "Yacht (sailboat)",  # SSID -5
    "Z": "WinAPRS",
    "[": "Person",  # S1 said "Jogger"; renamed 23 Jun 15
    "\\": "DF triangle",
    "]": "Mail / post office",  # S1 said "PBBS"; renamed 7 Dec 04
    "^": "Large aircraft",
    "_": "Weather station",
    "`": "Dish antenna",
    "a": "Ambulance",  # SSID -1
    "b": "Bicycle",  # SSID -4
    "c": "Incident command post",  # added 29 Jan 04
    "d": "Fire station",  # S1 said "Dual Garage (Fire Department)"
    "e": "Horse / equestrian",
    "f": "Fire truck",  # SSID -3
    "g": "Glider",
    "h": "Hospital",
    "i": "IOTA (islands on the air)",
    "j": "Jeep",  # SSID -12
    "k": "Truck",  # SSID -14
    "l": "Laptop",  # added Jan 03
    "m": "Mic-E repeater",
    "n": "Node",
    "o": "Emergency operations centre",
    "p": "Dog / rover",
    "q": "Grid square (above 128 m)",
    "r": "Repeater",  # S1 said "Antenna"; renamed Feb 07
    "s": "Ship (power boat)",  # SSID -8
    "t": "Truck stop",
    "u": "Truck (18-wheeler)",
    "v": "Van",  # SSID -15
    "w": "Water station",
    "x": "X-APRS (Unix)",
    "y": "Yagi at QTH",
    "z": None,  # "TBD" in S2; see §2 — legacy sets draw a shelter
    "{": None,
    "|": None,  # reserved: TNC stream switch
    "}": None,
    "~": None,  # reserved: TNC stream switch
}

ALTERNATE = {
    "!": "Emergency",
    '"': None,  # reserved
    "#": "Digipeater (green star)",
    "$": "Bank or ATM",
    "%": "Power plant",
    "&": "Gateway station",  # S1 said "HF Gateway (diamond)"; generalised 29 Jan 04
    "'": "Crash / incident site",
    "(": "Cloudy",
    ")": "Firenet MEO / MODIS Earth observation",
    "*": "Snow",
    "+": "Church",
    ",": "Girl Scouts",
    "-": "House (HF)",
    ".": "Ambiguous / indeterminate position",
    "/": "Waypoint destination",  # added 5 Jan 04
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
    "=": None,  # available overlay group
    ">": "Car (top view)",
    "?": "Information kiosk",
    "@": "Hurricane / tropical storm",
    "A": "Box",
    "B": "Blowing snow",
    "C": "Coast Guard",
    "D": "Depot",  # S1 said "Drizzle"; re-purposed Aug 14
    "E": "Smoke / visibility",
    "F": "Freezing rain",
    "G": "Snow shower",
    "H": "Haze / hazard",
    "I": "Rain shower",
    "J": "Lightning",
    "K": "Kenwood HT",
    "L": "Lighthouse",
    "M": "MARS",  # added 8 Sep 04; A=Army N=Navy F=Air Force
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
    "Y": "Radio / APRS device",  # was blank in S1
    "Z": None,
    "[": "Wall cloud / person",
    "\\": "GPS / navigation device",
    "]": None,
    "^": "Aircraft (top view)",
    "_": "Weather station with digipeater",
    "`": "Rain",
    "a": "Organisation",  # drawn as a diamond; the label names the thing, not the shape
    "b": "Blowing dust / sand",
    "c": "CD triangle (RACES / CERT / SATERN)",
    "d": "DX spot",
    "e": "Sleet",
    "f": "Funnel cloud",
    "g": "Gale (two red flags)",
    "h": "Store / hamfest",  # S1 said "Ham Store"; generalised
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
    "x": "Wreck or obstruction",  # added 18 Oct 06
    "y": "Skywarn",  # added 29 Jan 04
    "z": "Shelter",  # moved here from the primary table, 6 May 04
    "{": "Fog",
    "|": None,  # reserved: TNC stream switch
    "}": None,
    "~": None,  # reserved: TNC stream switch
}

# The overlay combinations that MEAN something (`symbols-new.txt`, 17 Mar 2021), keyed by
# the two characters as transmitted so a lookup is one dict hit. An overlay outside this
# set is legal but has no agreed meaning — it is reported as "base (X overlay)" rather
# than guessed at.
OVERLAY = {
    # Aircraft (\^)
    "A^": "Autonomous aircraft",
    "D^": "Drone",
    "E^": "Electric aircraft",
    "H^": "Hovercraft",
    "J^": "Jet",
    "M^": "Missile",
    "P^": "Propeller aircraft",
    "R^": "Remotely piloted aircraft",
    "S^": "Solar-powered aircraft",
    "V^": "VTOL aircraft",
    "X^": "Experimental aircraft",
    # Currency (\$)
    "U$": "US dollars",
    "L$": "British pounds",
    "Y$": "Japanese yen",
    # Diamond / affiliation (\a)
    "Aa": "ARES",
    # NOT in any Bruninga document. Verified from ircDDBGateway's APRSWriter.cpp, whose
    # two hard-coded formats emit `Da` for a gateway's -S and `D&` for its -A/B/C
    # modules — exactly the split KM4OSL and N1MPR show on this channel.
    "Da": "D-STAR",
    "D&": "D-STAR gateway",
    "Ga": "RSGB",
    "Ra": "RACES",
    "Sa": "SATERN (Salvation Army)",
    "Wa": "Winlink gateway",
    "Ya": "Yaesu C4FM repeater",
    # Balloons (\O)
    "BO": "Blimp",
    "MO": "Manned balloon",
    "TO": "Tethered balloon",
    "CO": "Constant-pressure balloon",
    "RO": "Rockoon",
    "WO": "Round-the-world balloon",
    # Box (\A)
    "9A": "Mobile DTMF user",
    "7A": "HT DTMF user",
    "HA": "House DTMF user",
    "EA": "EchoLink DTMF report",
    "IA": "IRLP DTMF report",
    "RA": "RFID report",
    "AA": "AllStar DTMF report",
    "DA": "D-STAR report",
    "XA": "OLPC XO laptop",
    # Buildings (\h)
    "Ch": "Ham radio club",
    "Eh": "Electronics store",
    "Fh": "Hamfest",
    "Hh": "Hardware store",
    # Cars (\>)
    "3>": "Tesla Model 3",
    "B>": "Battery EV",
    "D>": "DIY vehicle",
    "E>": "Ethanol",
    "F>": "Fuel cell / hydrogen",
    "H>": "Hybrid",
    "L>": "Nissan Leaf",
    "P>": "Plug-in hybrid",
    "S>": "Solar powered",
    "T>": "Tesla",
    "V>": "Chevy Volt",
    "X>": "Tesla Model X",
    # Civil defence triangle (\c)
    "Dc": "Decontamination",
    "Rc": "RACES",
    "Sc": "SATERN mobile canteen",
    # Depots (\D)
    "AD": "Airport",
    "FD": "Ferry landing",
    "HD": "Heliport",
    "RD": "Rail depot",
    "BD": "Bus depot",
    "LD": "Light rail / subway",
    "SD": "Seaport",
    # Digipeaters (\#)
    "1#": "WIDE1-1 digipeater",
    "A#": "Alternate-input digipeater",
    "E#": "Emergency-powered digipeater",
    "I#": "IGate",  # a digipeater that also gates to the internet
    "L#": "Path-length-trapping digipeater",
    "P#": "PacComm digipeater",
    "S#": "SSn-N digipeater",
    "X#": "Experimental digipeater",
    "V#": "Viscous digipeater",
    "W#": "WIDEn-N + SSn-N trapping digipeater",
    # Emergency (\!)
    "E!": "ELT or EPIRB",
    "V!": "Volcanic eruption / lava",
    # Visibility (\E)
    "HE": "Haze",
    "SE": "Smoke",
    "BE": "Blowing snow",
    "DE": "Blowing dust / sand",
    "FE": "Fog",
    # Gateways (\&)
    "I&": "IGate (generic)",
    "L&": "LoRa IGate",
    "R&": "Receive-only IGate",
    "P&": "PSKmail node",
    "T&": "TX IGate (1 hop)",
    "W&": "Wires-X",
    "2&": "TX IGate (2 hops)",
    # GPS devices (\\)
    "A\\": "Avmap G5",
    # Hazards (\H)
    "MH": "Methane hazard",
    "RH": "Radiation detector",
    "WH": "Hazardous waste",
    "XH": "Skull and crossbones",
    # Humans (\[)
    "B[": "Baby on board",
    "S[": "Skier",
    "R[": "Runner",
    "H[": "Hiker",
    # Houses / power (\-)
    "5-": "House, 50 Hz",
    "6-": "House, 60 Hz",
    "B-": "Off-grid / battery",
    "C-": "Combined renewables",
    "E-": "Emergency power",
    "G-": "Geothermal",
    "H-": "Hydro powered",
    "O-": "Operator present",
    "S-": "Solar powered",
    "W-": "Wind powered",
    # Incident sites (\')
    "A'": "Car crash site",
    "H'": "Hazardous incident",
    "M'": "Multi-vehicle crash",
    "P'": "Pileup",
    "T'": "Truck wreck",
    # Numbered circles (\0)
    "A0": "AllStar node",
    "E0": "EchoLink node",
    "I0": "IRLP repeater",
    "S0": "Staging area",
    "V0": "EchoLink + IRLP (VoIP)",
    "W0": "Yaesu WIRES",
    # Network nodes (\8)
    "88": "802.11 node",
    "G8": "802.11g node",
    # Portable (\;)
    "F;": "Field Day",
    "I;": "Islands on the Air",
    "S;": "Summits on the Air",
    "W;": "WOTA",
    # Power plants (\%)
    "C%": "Coal",
    "E%": "Emergency generation",
    "G%": "Gas turbine",
    "H%": "Hydroelectric",
    "N%": "Nuclear",
    "P%": "Portable generation",
    "R%": "Renewable",
    "S%": "Solar",
    "T%": "Geothermal",
    "W%": "Wind",
    # Rail (\=)
    "B=": "Trolley / streetcar",
    "C=": "Commuter rail",
    "D=": "Diesel",
    "E=": "Electric",
    "F=": "Freight",
    "G=": "Gondola",
    "H=": "High-speed rail",
    "I=": "Inclined rail",
    "L=": "Elevated rail",
    "M=": "Monorail",
    "P=": "Passenger",
    "S=": "Steam",
    "T=": "Terminal / station",
    "U=": "Subway",
    "X=": "Excursion",
    # Restaurants (\R)
    "7R": "7-Eleven",
    "KR": "KFC",
    "MR": "McDonald's",
    "TR": "Taco Bell",
    # Radios / devices (\Y)
    "AY": "Alinco",
    "BY": "Byonics",
    "IY": "Icom",
    "KY": "Kenwood",
    "YY": "Yaesu",
    # Special vehicles (\k)
    "4k": "4x4",
    "Ak": "ATV (all-terrain vehicle)",
    # Shelters (\z)
    "Cz": "Clinic",
    "Ez": "Emergency power shelter",
    "Gz": "Government building",
    "Mz": "Morgue",
    "Tz": "Triage",
    # Ships (\s)
    "6s": "Shipwreck",
    "Bs": "Pleasure boat",
    "Cs": "Cargo ship",
    "Ds": "Dive boat",
    "Es": "Medical transport",
    "Fs": "Fishing boat",
    "Hs": "High-speed craft",
    "Js": "Jet ski",
    "Ls": "Law enforcement boat",
    "Ms": "Military vessel",
    "Os": "Oil rig",
    "Ps": "Pilot boat",
    "Qs": "Torpedo",
    "Ss": "Search and rescue",
    "Ts": "Tug",
    "Us": "Submarine",
    "Ws": "Wing-in-ground effect craft",
    "Xs": "Passenger ferry",
    "Ys": "Sailing ship",
    # Trucks (\u)
    "Bu": "Bulldozer / backhoe",
    "Gu": "Gas truck",
    "Pu": "Snowplough",
    "Tu": "Tanker",
    "Cu": "Chlorine tanker",
    "Hu": "Hazardous cargo",
    # Water (\w)
    "Aw": "Avalanche",
    "Gw": "Flood gauge (green)",
    "Mw": "Mudslide",
    "Nw": "Flood gauge (normal)",
    "Rw": "Flood gauge (red)",
    "Sw": "Snow blockage",
    "Yw": "Flood gauge (yellow)",
}


def resolve(table: str, code: str) -> tuple[str | None, str | None]:
    """`(label, overlay_char)` for a symbol. `label` is None when nothing standard applies.

    The overlay branch is the one that matters: a table character that is neither `/` nor
    `\\` IS the overlay, and the icon comes from the alternate table."""
    if table == "/":
        return PRIMARY.get(code), None
    if table == "\\":
        return ALTERNATE.get(code), None
    combo = OVERLAY.get(table + code)
    if combo:
        return combo, table
    base = ALTERNATE.get(code)
    if base:
        return f"{base} ({table} overlay)", table
    return None, table


def label(table: str, code: str) -> str:
    """What a card calls this symbol. Never empty, and never a guess.

    An unassigned code says so. The spec's own instruction is that an unknown symbol is
    drawn as the international circle-and-slash "meaning NOT" — the text has to be equally
    honest, because a plausible wrong name is worse than an admitted gap."""
    found, _overlay = resolve(table, code)
    if found:
        return found
    pair = f"{table}{code}".strip()
    return f"unknown symbol {pair}" if pair else "unknown symbol"
