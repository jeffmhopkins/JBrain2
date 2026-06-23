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
