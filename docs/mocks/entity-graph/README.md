# Entity graph — layout/rendering direction mockups

Three interactive directions for an Obsidian-grade entity-graph render in the
PWA, presented per the `docs/DESIGN.md` "Options before commitment" rule (3–4
distinct *directions*, not color-swaps). Open each `.html` directly in a
browser — every file is self-contained, dark/light-toggleable (sun icon,
top-right), and uses the design tokens. Each ships a real mini force/zoom
engine so the *feel* is testable, not just the look.

**Scale.** Each mock builds its graph from a seeded generator that synthesises a
realistic personal knowledge graph — family, employers + colleagues, doctors +
conditions + drugs, financial accounts, places, events, hobbies — so the
directions are tested at the scale they need to survive: **A and B ≈155 nodes /
~280 edges, C ≈240 nodes / ~430 edges**. Change the `genGraph(N)` target at the
bottom of each file to stress them harder. Use the **fit** button (middle zoom
control) to frame the whole graph.

## Why revisit the current graph

`frontend/src/screens/GraphScreen.tsx` is a solid custom force layout, but it
falls short of the Obsidian feel in concrete, fixable ways:

- **Node size encodes hops, not connectedness.** Obsidian's readability comes
  from *degree-scaled* dots — hubs are visibly bigger. Today a 1-hop leaf and a
  1-hop hub look identical.
- **No neighbourhood highlight.** Hovering/holding a node should light its
  immediate links and dim everything else. This single interaction is most of
  what makes a dense graph legible. We have none.
- **No node dragging.** You can pan/zoom but can't grab a node to untangle a
  knot or feel the physics — a core Obsidian affordance.
- **Edge labels are always on.** Every relationship name renders at all times,
  so a busy graph reads as noise. Obsidian shows none by default.
- **Big icon discs over a constellation of dots.** The icon-disc look is
  JBrain's identity, but at overview scale it crowds; Obsidian's small dots +
  zoom-revealed labels scale to hundreds of nodes.

All three directions adopt the shared wins — **degree-scaled nodes, hover/tap
neighbourhood highlight, draggable nodes, focal-point zoom, labels that fade in
with zoom, edge labels on demand** — and differ in *layout philosophy*.

## The three directions

### A — Constellation (`graph-a-constellation.html`)
The faithful Obsidian clone. Small degree-scaled **dots** (type = colour, no
disc), a continuously-settling force layout, labels that fade in as you zoom,
hover/tap to light a node's neighbourhood and dim the rest, drag any node to
reflow live. Minimal chrome; a tapped node raises an entity peek card.
*Best for:* the cleanest, most familiar Obsidian feel and the densest graphs.
*Trade-off:* drops the entity-type icon discs that are part of JBrain's look.

### B — Orbits (`graph-b-orbits.html`)
Keeps JBrain's identity — **type-tinted icon discs**, the pinned "Me" hub — but
replaces force-chaos with **structured concentric depth rings**: root at centre,
1-hop on the inner ring, 2-hop on the outer, same-type entities clustered on an
arc, curved edges. A `1 hop / 2 hops` segment controls depth. **Edge labels stay
hidden until you touch a node**, then that node's links light up with their
predicate names.
*Best for:* the ego-centric "everything connects through Me" reading, and
keeping continuity with the entity-disc visual language.
*Trade-off:* less organic than a free force layout; rings impose order that may
hide true clustering.

### C — Clusters (`graph-c-clusters.html`)
For the whole-graph view at scale. A force layout that **pulls communities
together** and frames each in a soft, type-tinted **region (convex hull) with a
heading**, so structure is legible at a glance. A grouping segment switches
between **Type** and **Domain** (general / medical / financial / location);
group toggles fold whole communities; a **minimap** tracks the viewport; hover
lights a node's links.
*Best for:* sense-making over a large graph — "what are the big neighbourhoods,
and how do they connect" — and surfacing the domain firewalls visually.
*Trade-off:* the most chrome; the hull framing is overkill for a small graph.

## Mobile-first round (D, E, F) — informed by UX research

The first three (A–C) are desktop-grade renders but lean on **hover, wheel-zoom,
and sub-44px chrome** — wrong for a phone-first, one-thumb system. Three UX
research passes (mobile touch-graph patterns, mobile graph IA, and the JBrain
design-system constraints) converged on a clear brief: **never show the whole
hairball on a phone — open on a *local* graph; replace hover with tap-to-select
+ a bottom Sheet; give small dots invisible ≥44px hit targets; prefer
deterministic/typed layouts over free force (no fat-finger overlap, no
gesture-vs-physics fights); pre-settle and freeze any sim; and make search the
front door.** D, E, F each take that brief a different way.

### D — Focus + Sheet (`graph-d-focus-sheet.html`)
The Google-Maps model. A **local graph** (focal entity + its 1-hop neighbours,
deterministic radial, fits the region) sits up top; a **persistent draggable
bottom sheet** lists the entity's relationships as fat tappable rows. **Tap a
node or a row to re-centre** (breadcrumb tracks the walk); **drag the sheet
handle** to trade graph height for detail. A search field is the front door.
*Best default* — lowest-risk, most phone-native, scales to dense hubs (the list
never overlaps). Pinch-zoom is a bonus, not required.

### E — Orbit Deck (`graph-e-orbit-deck.html`)
No force layout at all. A **deterministic orbit** centres the focal entity and
arranges neighbours on one ring **grouped into type sectors** — always legible,
never overlapping, no panning needed. Below, a **swipeable card deck** walks the
neighbours one at a time; the active card lights its node + edge, and **Dive**
re-orbits on it (breadcrumb tracks the path). *Best for* sequential, one-thumb
exploration of a typed knowledge graph; the swipe-deck is the hop-by-hop spine.

### F — Cluster Drill (`graph-f-cluster-drill.html`)
Semantic zoom for large graphs — the phone **never paints 240 raw nodes**. It
opens on a handful of **community bubbles** (group by **Type** or **Domain**,
sized by count); **tap a bubble to expand its members** into tappable discs
(capped, with a `+N` disc that opens the full list in a sheet); tap a member for
its detail sheet; the breadcrumb collapses back a tier. *Best for* sense-making
over the whole graph and seeing the domain firewalls as distinct communities —
the only direction that gives global structure on a 390px screen.

All three: tap-only (no hover), every control ≥44px, primary actions in the
bottom half (one-thumb), bottom-Sheet detail (reuses the system's shared Sheet
paradigm), safe-area padding, and no continuously-running physics.

**D · 2-hop variant (`graph-d-focus-sheet-2hop.html`)** — D with a deeper local
graph: focal at centre, 1-hop neighbours on the inner ring, their neighbours
(2-hop) clustered just outside each parent. A bottom-left **1 / 2 hops** toggle
switches depth; pinch-zoom and a draggable sheet handle the extra density. The
sheet still lists the *centred* entity's direct relationships. ("Me" shows ~11
nodes at 1 hop, ~19 at 2 hops.)

## V1 parity round — Force Map (`graph-v1-forcemap.html`)

A direct port of **JBrain v1's** graph geometry, built to answer "I don't like
the current 1-hop traversal display." v1's `web/src/pages/GraphPage.tsx` renders
the *whole* knowledge graph with `react-force-graph-2d`: a free force layout
(charge `-180`, `forceCollide`, link spring), **degree-scaled dots**
(`r = √deg · NODE_REL`), kind-coloured nodes, a **click-to-focus** that rings the
node and filters to its **N-hop neighbourhood** via a `1 / 2 / 3 / All hops`
selector, a second click to open, a kind filter, and `zoomToFit` on settle.

This mock reproduces that feel in JBrain2's design tokens + phone frame:

- **Whole-graph force layout, no anchored root** — every node is placed by the
  sim, exactly as v1's react-force-graph does. "Me" lands central only because
  it is the biggest hub. This is the key departure from today's
  `GraphScreen.tsx`, which re-lays a deterministic focal/1-hop/2-hop ring on
  every re-centre.
- **Degree-scaled dots** — hubs are visibly bigger; type = colour.
- **Tap-to-focus = hop-depth filter (the fix).** Tapping a node rings it, lights
  its neighbourhood, and dims everything else **in place** — the map never
  re-lays-out. The `1 · 2 · 3 · All` segmented control expands/contracts the lit
  neighbourhood by BFS depth, so "going a hop deeper" is a *highlight* change on
  a stable map, not a fresh ring. A second tap (or **Open entity →**) enters it;
  **Isolate neighbourhood** frames just the focus's hop set.
- Shared wins from A–F carry over: labels fade in with zoom and cull on overlap
  (focus + neighbours always labelled), draggable nodes, focal-point zoom, the
  legend doubles as a kind filter, **fit** frames the whole graph.

*Best for:* the familiar v1 "one organic map of everything" reading, where
exploration is re-focusing on a stable layout rather than rebuilding a local
ring. *Trade-off:* a true whole-graph render needs the dot+zoom-label discipline
(no big icon discs at overview) and a settled/frozen sim on a phone — both
applied here.

## Mobile force round (M1–M4) — keep the force feel, fix the phone

The V1 Force Map above nails v1's geometry but is a desktop-grade whole-graph
render: on a 390px screen it's a hairball with overlapping tap targets. These
four keep a **real force/spring/charge layout** (the explicit ask) but make it
mobile-native — tap-only, ≥44px hit targets, bottom-sheet detail, and a
pre-settled sim that **freezes** instead of jittering forever. Each is
self-contained and shares the design tokens, phone frame, seeded ~158-node
dataset, and the v1 force engine.

### M1 — Local Force (`graph-m1-local-force.html`)
Never paints the hairball. A real force sim runs over **only the focal's capped
1–2 hop neighbourhood** (~20–30 nodes), as type-tinted icon discs with ≥44px
invisible hit halos. Tapping any node, search result, or a fat relationship row
in the persistent bottom sheet **re-focuses** — the local map springs/fades into
the next neighbourhood while a breadcrumb tracks the walk. *Best for:* the force
feel with zero hairball risk; the closest mobile analogue to today's screen but
organic instead of a fixed ring.

### M2 — Force + Sheet (`graph-m2-force-sheet.html`)
The Google-Maps model. A **frozen whole-graph force map** fills the top ~58%
(small degree-scaled dots, zoom-revealed labels, ≥44px halos); a **persistent
draggable bottom sheet** below trades map height for list height. Tapping a node
**flies the camera** to centre it, lights its neighbourhood, and syncs the sheet
to that entity; tapping a sheet row flies to that neighbour. *Best default* —
lowest-risk, map and list stay locked together.

### M3 — Fisheye Force (`graph-m3-fisheye.html`)
Keeps the whole organic map but solves density with a **focus+context lens**: a
render-time Sarkar–Brown radial fisheye that **spreads + magnifies the cluster
under your finger** so tight nodes become thumb-tappable, while the periphery
compresses to stay in context (nothing scrolls off). The lens follows the
finger (or locks to a node); labels reveal only under it; S/M/L strength.
*Best for:* exploring a dense map without losing the global picture.

### M4 — Cluster Force Drill (`graph-m4-cluster-force.html`)
Force at **two tiers**, never the raw hairball. The overview is a force system of
**community bubbles** (charge + collide, sized by member count); a **Type ⇄
Domain** toggle splits the firewalled medical / financial / location communities
into their own bubbles. Tapping a bubble drills into a **local member force sim**
(capped, with a `+N more` sheet); a breadcrumb collapses back. *Best for:*
global structure + seeing the domain firewalls as distinct communities.

## Notes for implementation

- All three are reachable from the current `GraphScreen` data contract
  (`EgoGraph { nodes, edges }`) with no API change; degree is derived
  client-side from `edges`, exactly as these mocks do.
- A and C run a live `requestAnimationFrame` sim; honour
  `prefers-reduced-motion` by settling to a static layout (skip the animation,
  keep drag).
- Labels here use the existing `--fs-graph-label` register and the SVG
  `paint-order: stroke` halo trick already in `styles.css`.
- The directions are not exclusive — e.g. B's "edge labels on touch" and the
  shared "hover-neighbourhood highlight" are wins worth taking regardless of the
  layout chosen.
