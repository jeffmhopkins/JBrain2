# Amoeba Brain — 15-Node Feed-Forward Controller + Phone Visualization Brief

A design brief for upgrading the microscope mockup's tiny 5-node net into a real
~15-node feed-forward controller that genuinely drives advanced amoeboid
locomotion, and for rendering it legibly on a 390px phone. Matches the style of
`frontend/mockups/landing/microscope/01-darkfield-nocturne.html` (vanilla canvas
2D, no libraries, calm + mobile-first). The existing file is the reference, not
the target — this brief describes the replacement net it slots into.

Design constraint that overrides everything: **calm and legible, never busy.**
The user has rejected cluttered designs three times. A 15-node fully-connected
net is a hairball at 390px, so the whole brief is organized around *not drawing
that*. Sparse, layered, hand-placed.

---

## 0. What the old net was, and why it has to grow

The current `NET` (lines 159–162 of the reference file) is:

```
foodProx, hunger  →  h  →  reach, engulf      (5 nodes, 5 edges)
```

That can only express "is there food + am I hungry → move + eat." It cannot
represent *direction* (which way is the food?), *competing pseudopods*, *energy
budget*, *vacuole cycle*, or *edge avoidance* — all of which the upgraded
locomotion model needs. We need directional sensing on the input side and
directional actuation on the output side, with a hidden layer that fuses the
directional field into a heading. That is the minimum that makes the net an
honest controller rather than decoration.

---

## 1. ARCHITECTURE

### Topology: 8 → 4 → 3 = 15 nodes, two functional bands of hidden

```
INPUTS (8)          HIDDEN (4)        OUTPUTS (3 groups, 3 nodes)
chemo N             dirX  ───────────► driveX
chemo E             dirY  ───────────► driveY
chemo S             arousal ────────► commit (winner pseudopod)
chemo W             visceral ───────► expel  (contractile vacuole)
hunger                                 engulf
energy
vacuoleFill
edgeProx
```

Wait — that lists 5 outputs. Final count below. The headline "15+" is met by
**8 inputs + 4 hidden + 5 outputs = 17 nodes**. I recommend 17, not a bare 15,
because the two extra output nodes (a fifth output and keeping hidden at 4) are
what let the net *control* every advanced feature without overloading single
neurons. 17 still lays out cleanly in three columns on a phone (see §4).

#### Why these layer sizes (legibility justification)

- **8 inputs** — the smallest directional set that reads as "a ring of sensors
  around the membrane" while still fitting one screen-height column. 8 = the 4
  cardinal + 4 scalar (hunger, energy, vacuoleFill, edgeProx). 8 dots in a
  column at ~40px pitch is ~320px tall — fits a tall phone with margins. (Using
  6 directional sensors as the prompt allows is also fine; 4 cardinal keeps the
  input column shorter and the directional→directional wiring trivially
  readable. See the directional-resolution note in §1.4.)
- **4 hidden** — *named, not anonymous.* Two "vector" units (dirX, dirY) that
  resolve the chemoreceptor ring into a heading, plus two "scalar" units
  (arousal = how urgently to act, visceral = internal-state pressure). Four is
  the fewest that keeps each hidden node *meaning one thing*, which is what makes
  the graph legible without text. A blob of 6–8 anonymous hidden nodes is the
  hairball we are avoiding.
- **5 outputs** — driveX, driveY, commit, engulf, expel. One per controllable
  actuator. Five dots in the right column at ~50px pitch ≈ 250px — comfortably
  shorter than the input column, which gives the panel a pleasant trapezoid
  silhouette rather than a rectangle.

### 1.1 INPUT neurons (all normalized 0..1)

| # | id | reads (from sim state) | normalization |
|---|------|------------------------|----------------|
| 0 | `chemoN` | food-gradient strength sampled in the North membrane arc | `gradient(arc)` clamped 0..1 |
| 1 | `chemoE` | … East arc | same |
| 2 | `chemoS` | … South arc | same |
| 3 | `chemoW` | … West arc | same |
| 4 | `hunger` | `AMO.hunger` (rises over time, drops on feed) | already 0..1 |
| 5 | `energy` | metabolic reserve; falls with movement, rises on digest | `AMO.energy` 0..1 |
| 6 | `vacFill` | contractile-vacuole fill 0..1, sawtooth that triggers expel near 1 | `AMO.vacFill` |
| 7 | `edgeProx` | proximity to edge of microscope field | `1 - clamp(distToEdge/margin)` |

**Chemoreceptor sampling** — replace the single `nearAng`/`foodProx` scalar with
a per-arc gradient. For each of the 4 arcs (centered at angles 0, 90, 180, 270),
sum food influence weighted by an angular cosine lobe and by `1/dist`:

```js
function chemo(arcAng){
  let s=0;
  for(const fd of food){ if(fd.eat>0) continue;
    const a=Math.atan2(fd.y-AMO.y, fd.x-AMO.x);
    let d=Math.abs(a-arcAng); d=Math.min(d,Math.PI*2-d);
    const lobe=Math.max(0,Math.cos(d));          // 0..1, faces the arc
    const dist=Math.hypot(fd.x-AMO.x,fd.y-AMO.y);
    s += lobe*lobe * (1-Math.min(1,dist/SENSE));
  }
  return Math.min(1,s);
}
```

This is the load-bearing change: the brain now *sees a direction*, not just a
scalar, so a directional output can be wired straight back to a membrane arc.

### 1.2 HIDDEN neurons

| # | id | role | fed by |
|---|------|------|--------|
| 8 | `dirX` | resolves chemo ring → x-component of "go" vector | chemoE (+), chemoW (−) |
| 9 | `dirY` | resolves chemo ring → y-component | chemoS (+), chemoN (−) |
| 10 | `arousal` | overall urgency to act | hunger (+), max(chemo) (+), energy (−) |
| 11 | `visceral` | internal housekeeping pressure | vacFill (+), edgeProx (+) |

dirX/dirY are deliberately *signed* internally (use `tanh`, remap to 0..1 only
for display) so they can drive both directions. arousal gates how hard
driveX/driveY push; visceral gates expel and edge-retreat.

### 1.3 OUTPUT neurons

| # | id | drives in sim | wiring |
|---|------|---------------|--------|
| 12 | `driveX` | x-component of locomotion target + which arcs extend | from dirX·arousal |
| 13 | `driveY` | y-component of locomotion target + which arcs extend | from dirY·arousal |
| 14 | `commit` | selects the *winning* pseudopod (others retract) | from arousal·max(chemo) |
| 15 | `engulf` | phagocytosis trigger when membrane overlaps food | from commit·near-food + chemo |
| 16 | `expel` | contractile-vacuole expel pulse | from visceral when vacFill high |

### 1.4 Directional-sensor → directional-output mapping (the key wiring)

The 4 chemo inputs map to the 2 drive outputs through dirX/dirY:

```
chemoE ─(+)─►┐                 ┌─► driveX ──► extend EAST arc / push +x
chemoW ─(−)─►┤ dirX ──arousal──┤
             │                 └─► (negative driveX extends WEST arc)
chemoN ─(−)─►┐
chemoS ─(+)─►┤ dirY ──arousal──┐
             │                 └─► driveY ──► extend SOUTH arc / push +y
```

Each membrane control point `i` gets extension proportional to how well its
angle aligns with the `(driveX, driveY)` vector (cosine lobe), exactly like the
old `nearAng` lobe but sourced from the net's output instead of raw geometry.
That is what makes it "genuinely controlled."

If you want finer directionality, use **6 chemo inputs** (every 60°) and keep
dirX/dirY — the resolution improves with no change to the output side. I
recommend 4 for the first pass: shorter input column, cleaner edges.

---

## 2. DRIVING IT

### 2.1 Weights: hand-tuned, not learned

Use **fixed hand-tuned weights**. Rationale: this is a mockup whose job is to
*look alive and legible*, and learned weights drift toward noisy, hard-to-read
activations (the opposite of calm). Hand-tuned weights let you guarantee the
pretty behaviors: "food to the east → east lobe lunges → engulf." Keep the
sigmoid/tanh squashing from the original (`sig` at line 178). Encode weights
inline in `netStep`, same shape as the reference (lines 179–193).

If learning is ever wanted, the honest-but-cheap option is a **slow Hebbian
nudge** on display weights (`w += η·pre·post`, η≈0.0005, clamped) so edges that
co-fire brighten over minutes — visible as the graph "warming up" without ever
producing erratic motion. Deferred; not needed for the brief's goal.

### 2.2 Wiring outputs back to locomotion

Concrete hooks into the existing `step(dt)` (reference lines 227–297):

- **Per-arc extension** — replace the `strength`/`nearAng` block (lines 261–266)
  with extension driven by `(driveX, driveY)`:

  ```js
  const dvx=N[12].a*2-1, dvy=N[13].a*2-1;        // remap 0..1 → −1..1
  const driveAng=Math.atan2(dvy,dvx);
  const driveMag=Math.hypot(dvx,dvy);            // 0..~1.4
  const strength=AMO.r*(0.30+0.45*N[14].a)*driveMag;   // commit scales it
  for(let i=0;i<NP;i++){
    const a=(i/NP)*Math.PI*2; let d=Math.abs(a-driveAng); d=Math.min(d,Math.PI*2-d);
    const w=Math.max(0,1-d/0.85); const want=strength*w*w;
    AMO.pseudo[i]+=(want-AMO.pseudo[i])*Math.min(1,dt*1.2);
  }
  ```

- **Multiple competing pseudopods** — track 2–3 candidate lobes anchored to arc
  centers. Each accumulates a `score` from the chemo input of its arc; `commit`
  (N[14]) sharpens a softmax over scores so the **winner** grows and the losers
  retract:

  ```js
  // candidate lobes anchored at arc centers, e.g. the 4 chemo arcs
  for(const lobe of lobes) lobe.score += (chemoFor(lobe.ang)-lobe.score)*dt*2;
  const sharp=1+N[14].a*4;                        // commit sharpens the contest
  const tot=lobes.reduce((s,l)=>s+Math.exp(l.score*sharp),0);
  for(const lobe of lobes){
    lobe.win=Math.exp(lobe.score*sharp)/tot;      // softmax share 0..1
    lobe.ext += ((lobe.win>0.5? lobe.win:0)*AMO.r*0.6 - lobe.ext)*dt*1.2; // winner extends, losers ease to 0
  }
  ```

  Then add each lobe's `ext` into the arc's pseudopod controls with the same
  cosine lobe. Result: 2–3 lobes visibly *compete*, one wins, the rest retract —
  honest amoeboid locomotion, driven by `commit`.

- **Commit / retract** — `commit` high → softmax sharp → single dominant lobe.
  `commit` low → lobes share, body stays rounder. No separate retract output
  needed; retract is the absence of winning.

- **Expel** — when `expel` (N[16]) crosses a threshold, push a vacuole outward
  and reset `vacFill`:

  ```js
  if(N[16].a>0.6 && AMO.vacFill>0.7){
    vacuoles.push({ax:Math.cos(AMO.dir+Math.PI)*AMO.r*0.6,
                   ay:Math.sin(AMO.dir+Math.PI)*AMO.r*0.6,
                   r:10, color:S.vacuole, life:1, expel:1});
    AMO.vacFill=0;                                 // emptied
  }
  // fill ramps back up
  AMO.vacFill=Math.min(1, AMO.vacFill+dt*0.05);
  ```

- **Engulf** — keep the existing overlap test (lines 269–272) but gate the
  *forced* path on `N[15].a` instead of the bare `tryEngulf` flag.

- **Energy** — decrement on movement (`AMO.energy -= speed*dt*k`), restore on
  digest (in the `fd.eat>=1` block, lines 278–282). Feeds input #5. This closes
  the loop so the brain genuinely budgets effort.

### 2.3 Smoothing so it looks alive, not strobing

- Keep the original easing toward targets (`n.a += (v-n.a)*0.08`, lines 185–186).
  At ~60fps that is a ~200ms time constant — calm, no flicker. Keep it.
- Activations drive *targets*, never instantaneous geometry. Pseudopod `ext`,
  vacuole motion, and speed all ease (`*Math.min(1,dt*k)`) so outputs glide.
- **Sparse traveling pulses** only on edges whose source is active, exactly as
  lines 188–192, but throttle harder for the bigger graph (spawn rate
  `src.a*0.02*dt*60`, and cap total live pulses at ~6) so the net never looks
  like a light show. Pulses are the *only* fast-moving element; everything else
  breathes.

---

## 3. INTERACTION

Reuse the reference's `pointerdown` model (lines 206–212). Each node tap fires
that neuron and the effect must be *visible on the amoeba*:

| tapped node | immediate amoeba effect |
|-------------|--------------------------|
| `chemoN/E/S/W` | inject phantom gradient in that arc → that lobe starts to lunge |
| `hunger` | spike urge → whole body surges toward nearest food |
| `energy` | flash reserve → speed briefly boosts |
| `vacFill` | force-fill the vacuole → primes an expel |
| `driveX/Y` | push the drive vector that way → directional lunge |
| `commit` | sharpen the contest now → current leading lobe snaps out, others retract |
| `engulf` | force phagocytosis of overlapping/near food |
| `expel` | vacuole expels immediately (visible ejection) |

Implementation mirrors `fireNode` (lines 194–201): set `n.a=1; n.hold=0.5;
n.pulse=1`, push pulses on outgoing edges, and set the corresponding sim flag.

### Touch targets & gesture cleanliness

- **44px touch targets.** Visual node radius is small (7–10px, §5) but the *hit
  radius* is ≥22px (44px diameter), like the original's `<26` test (line 209).
  With 17 nodes in 3 columns the columns must be spaced so hit circles don't
  overlap: input column pitch ≥44px (8 nodes → ≥352px tall column, OK on a tall
  phone); output column ≥44px (5 nodes → 220px, easy). Hidden column has only 4
  nodes, lots of room.
- **Tap vs other gesture** — the scene has no pan/zoom (line 204 comment), so
  there is no ambiguity to resolve *inside* the canvas: every tap is either a
  node-fire or a drop-food. Keep the existing priority order: test nodes first
  (return on hit), then the panel rect (swallow, do nothing), then drop food.
  Add `touch-action:manipulation` is already set (line 10), which kills
  double-tap-zoom. Keep `passive:true`. No long-press, no drag — one gesture,
  one meaning. This is also what keeps it calm.
- Optional: a 120ms scale-pop on the tapped node (ease `n.tapPop` 1→0) for tactile
  feedback without sound.

---

## 4. LEGIBILITY ON A PHONE (the hard part)

The enemy is edge count. 17 fully-connected nodes across 3 layers would be
8·4 + 4·5 = 52 edges — a hairball. We draw **far fewer.**

### 4.1 Sparse connectivity — only meaningful edges (~14, not 52)

Hand-pick edges that carry real signal. Suggested edge set:

```
chemoE→dirX, chemoW→dirX                 (2)
chemoN→dirY, chemoS→dirY                 (2)
hunger→arousal, energy→arousal           (2)
vacFill→visceral, edgeProx→visceral      (2)
dirX→driveX, dirY→driveY                 (2)
arousal→commit, commit→engulf            (2)
visceral→expel                           (1)
arousal→driveX (or a single "gain" tie)  (1)
```

≈14 edges. Every edge is explainable in one phrase, which is the legibility
test. Inputs that don't feed a hidden node directly simply have no edge — that
*absence* is information ("energy doesn't steer, it modulates"). Sparse is not
just prettier; it's more honest about the computation.

### 4.2 Layout: three columns, trapezoid, bottom dock

Replace the bottom-right 168×118 box. For a 15+ node net that box is far too
small. Use a **translucent full-width bottom dock**:

```
panel: x=8, y=H-PH-8, w=W-16, h=PH      (PH ≈ 300 on a tall phone)
3 columns:  inX = x+34
            hidX = x + w*0.5
            outX = x + w-34
input nodes  : 8 evenly down  [y+24 .. y+h-24]   pitch ≈ (h-48)/7
hidden nodes : 4 evenly down, vertically centered (shorter span)
output nodes : 5 evenly down  [y+30 .. y+h-30]   pitch ≈ (h-60)/4
```

The dock spans full width so columns are far apart → long, shallow-angle edges →
fewer visual crossings. The hidden column sits in the vertical middle with only
4 nodes, so the silhouette is a calm hourglass/trapezoid, reading clearly as
in → think → out, left to right.

Layout math (drop-in replacement for `layoutNet`, reference lines 166–177):

```js
function layoutNet(){
  const PH=Math.min(300, H*0.42);            // dock height, capped
  const pad=8;
  netBox={x:pad, y:H-PH-pad, w:W-pad*2, h:PH};
  const x=netBox.x, y=netBox.y, w=netBox.w, h=netBox.h;
  const inX=x+34, hidX=x+w*0.5, outX=x+w-34;
  const colY=(n,count,top,bot)=>top+(bot-top)*(count<2?0.5:n/(count-1));
  for(let i=0;i<8;i++){ const nn=IN[i]; nn.x=inX;  nn.y=colY(i,8,y+24,y+h-24); }
  for(let i=0;i<4;i++){ const nn=HID[i];nn.x=hidX; nn.y=colY(i,4,y+h*0.30,y+h*0.70); }
  for(let i=0;i<5;i++){ const nn=OUT[i];nn.x=outX; nn.y=colY(i,5,y+30,y+h-30); }
}
```

(Index nodes by band — `IN`, `HID`, `OUT` — rather than one flat array, so the
column math stays readable.)

### 4.3 Color-coding bands (no text needed)

Three hues, low saturation, consistent with `S`:

- **inputs** — cool teal, the sensor color: `#5fbfe6` (slightly bluer than the
  body so "outside info" reads as cool).
- **hidden** — the body's own ink `#5fe6c8` (these *are* the amoeba's thought).
- **outputs** — warm `#ffd27f` (`S.netHot`) at rest-dim, full-warm when firing
  (action = warm). This makes the eye read left-cool → right-warm = sense → act.

Shape can reinforce without labels: inputs = small filled dots, hidden = ringed
dots, outputs = ringed dots with a tiny inner caret/triangle hinting
"actuator." Keep it subtle.

### 4.4 Edge opacity: rest vs active

- **Rest:** `S.netDim` ≈ `rgba(120,230,205,0.10)` — barely there, the graph is a
  faint constellation. Lower than the original 0.14 because there are more edges.
- **Active:** opacity ramps with `(A.a+B.a)*0.5`, up to ~0.7, width 0.8→2.0,
  exactly the original mechanism (lines 348–355). Only the firing path lights up,
  so at any instant maybe 2–3 edges are bright — calm.

### 4.5 Curved edges to cut crossings

Use a single quadratic bezier per edge, bowed toward the column gap, instead of
straight lines. Curving the input→hidden bundle slightly downward and the
hidden→output bundle slightly upward separates the two fans so they don't
overlap in the middle:

```js
function edgePath(c,A,B){
  const mx=(A.x+B.x)/2, my=(A.y+B.y)/2;
  const bow=(B.x>A.x? -1:1) * Math.min(28, Math.abs(B.y-A.y)*0.18);
  c.beginPath(); c.moveTo(A.x,A.y); c.quadraticCurveTo(mx, my+bow, B.x,B.y);
}
```

Pulses travel the curve by sampling the same quadratic at parameter `p` (use the
bezier point formula, not linear lerp like line 353). Curves read as organic —
on-theme for a cell — and meaningfully reduce the hairball.

### 4.6 "It's the amoeba's brain" without text

- Place the dock directly below the amoeba and draw a **single faint tendril**
  (one low-opacity curve) from the amoeba's nucleus down into the input column,
  so the eye connects body→brain. Pulse it occasionally. One line, not many.
- The cool→warm left-to-right gradient + the in/think/out trapezoid shape make
  the function self-evident.
- Optional minimal labels: only on the 5 outputs, 8px lowercase, 40% opacity,
  shown for ~2s after first interaction then fading. Inputs/hidden stay
  unlabeled. Less text = calmer; lean on color and position.

### 4.7 Anti-clutter checklist (the three-rejections rule)

- ≤14 edges, hand-picked, each explainable.
- Rest opacity ≤0.10 for edges; the resting graph nearly disappears.
- ≤6 live pulses total, ever.
- 3 hues only; no per-node colors.
- No labels except the optional fading output captions.
- One tendril linking body↔brain, not a web.
- Everything eases; only pulses move fast.

---

## 5. RECOMMENDED DEFAULTS

| thing | value | note |
|-------|-------|------|
| nodes | 8 in / 4 hidden / 5 out = **17** | meets "15+", lays out in 3 columns |
| chemo sensors | 4 cardinal (or 6 @ 60°) | 4 = shorter input column |
| edges | **~14**, hand-picked | not 52 |
| node visual radius | 7px rest, +3px when active | matches original (line 363) |
| node hit radius | **22px** (44px target) | matches original `<26` (line 209) |
| input column pitch | ≥44px | 8 nodes → ~352px span |
| output column pitch | ≥44px | 5 nodes → ~220px span |
| panel | **full-width dock**, `x=8, w=W-16` | replaces 168×118 corner box |
| panel height | `min(300, H·0.42)` | ~300px on a 844px phone |
| panel fill | `rgba(4,12,12,0.34)` | reuse `S.netPanel` |
| edge rest opacity | **0.10** | fainter than old 0.14 (more edges) |
| edge active opacity | up to 0.70 | only the firing path |
| edge width | 0.8 rest → 2.0 active | reuse original ramp |
| activation easing | `+= (v-a)*0.08` | ~200ms, calm (reuse) |
| pulse speed | `p += dt*2.6` | reuse original (line 191) |
| pulse spawn | `src.a*0.02*dt*60`, cap 6 live | throttled vs original 0.03 |
| colors | in `#5fbfe6`, hid `#5fe6c8`, out `#ffd27f` | cool→warm = sense→act |
| tendril | one curve, opacity ~0.08 | body↔brain link |

### ASCII layout sketch (portrait phone, ~390×844)

```
┌────────────────────────────── 390 ──────────────────────────────┐
│                                                                  │
│                         ·  food                                  │
│                       (   amoeba   )   <- pseudopods compete     │
│                        \  nucleus  /                             │
│                         '--.....--'                              │
│                              :  one faint tendril                │
│   ┌───────────────────────── dock (full width, ~300 tall) ────┐ │
│   │ chemoN ●                                                   │ │
│   │ chemoE ●        dirX ◉                                     │ │
│   │ chemoS ●                       driveX ◬                    │ │
│   │ chemoW ●        dirY ◉         driveY ◬                    │ │
│   │ hunger ●                       commit ◬                    │ │
│   │ energy ●        arousal ◉      engulf ◬                    │ │
│   │ vacFill●                       expel  ◬                    │ │
│   │ edgeProx●       visceral◉                                  │ │
│   │  INPUTS         HIDDEN          OUTPUTS                     │ │
│   │  (cool ●)       (teal ◉)        (warm ◬)                   │ │
│   └────────────────────────────────────────────────────────────┘ │
└──────────────────────────────────────────────────────────────────┘
edges: only the ~14 meaningful ones, faint at rest, curved, bowed apart.
```

### Node-by-node IO table (consolidated)

| idx | band | id | reads / drives |
|-----|------|------|----------------|
| 0 | in | chemoN | food gradient, North arc |
| 1 | in | chemoE | food gradient, East arc |
| 2 | in | chemoS | food gradient, South arc |
| 3 | in | chemoW | food gradient, West arc |
| 4 | in | hunger | `AMO.hunger` |
| 5 | in | energy | `AMO.energy` |
| 6 | in | vacFill | `AMO.vacFill` |
| 7 | in | edgeProx | proximity to field edge |
| 8 | hid | dirX | E−W → x heading |
| 9 | hid | dirY | S−N → y heading |
| 10 | hid | arousal | hunger + maxChemo − energy |
| 11 | hid | visceral | vacFill + edgeProx |
| 12 | out | driveX | x push + East/West arc extension |
| 13 | out | driveY | y push + North/South arc extension |
| 14 | out | commit | softmax sharpness → winning pseudopod |
| 15 | out | engulf | phagocytosis trigger |
| 16 | out | expel | contractile-vacuole ejection |

---

## 6. Implementation order (slots into the reference file)

1. Add `AMO.energy`, `AMO.vacFill` to the `AMO` struct (line 70).
2. Add `chemo(arcAng)` helper; compute the 8 inputs each `step`.
3. Replace `NET` with `IN`/`HID`/`OUT` banded arrays + the ~14-edge list.
4. Replace `layoutNet` (§4.2) and `netStep` (hand-tuned weights, §1).
5. Replace the pseudopod block in `step` with the competing-lobe softmax (§2.2).
6. Add expel/energy hooks into `step`.
7. Replace `drawNet` edges with curved `edgePath` + banded colors (§4.3–4.5).
8. Extend `fireNode` with the per-node effects table (§3).
9. Add the single body↔brain tendril (§4.6).

Each step is independently testable against the running mockup; nothing here
needs a library.

---

## 7. Sources (small sensor-actuator controller nets)

- Braitenberg, *Vehicles: Experiments in Synthetic Psychology* (1984) — the
  canonical case for direct sensor→actuator wiring producing lifelike behavior
  with a handful of nodes; the directional-chemo→directional-drive mapping here
  is a Braitenberg "vehicle 3" in spirit.
- Beer & Gallagher, "Evolving Dynamical Neural Networks for Adaptive Behavior,"
  *Adaptive Behavior* 1(1), 1992 — continuous-time recurrent nets as minimal
  agent controllers; motivates the small, named-unit hidden layer over a large
  anonymous one.
- Reynolds, "Steering Behaviors for Autonomous Characters" (GDC 1999) — the
  seek/arrive/wander vector blending used for `driveX/driveY` and the wander
  fallback (reference lines 249–250).
- Pfeifer & Bongard, *How the Body Shapes the Way We Think* (2006) — embodiment
  argument for keeping the controller honestly coupled to body state (energy,
  vacuole, edge) rather than decorative.

(These are design references; the brief is otherwise a direct extension of the
existing mockup's own conventions.)
