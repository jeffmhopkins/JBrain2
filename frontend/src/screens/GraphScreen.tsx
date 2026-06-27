// The entity graph "Map" — a mobile-first local view (mock
// docs/mocks/entity-graph/graph-n1-labeled-zoom.html). The screen centres on one
// focal entity and lays its capped 1–2 hop neighbourhood out with a small force
// pass — focal pinned at the middle, neighbours settled organically (charge +
// collide + link springs), pre-settled synchronously with no perpetual
// animation — so it never becomes an unreadable hairball on a phone and never
// overlaps tap targets. Every connection renders identically and carries its
// predicate label; a type-filter chip rail drops whole entity types from the
// map. A persistent bottom panel lists the focal's relationships as fat tappable
// rows; tapping a node or a row re-centres (the breadcrumb tracks the walk).
// Search is the front door. Tap-only: hover is never the affordance.

import { type PointerEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { type EgoGraph, api } from "../api/client";
import { ChevronRightIcon, SearchIcon } from "../components/icons";
import {
  ENTITY_TYPE_COLOR,
  EntityTypeIcon,
  type EntityTypeKey,
  resolveEntityKind,
} from "../entities/kinds";

interface GraphScreenProps {
  onOpenEntity: (entityId: string) => void;
  /** Entity to centre on at open; when absent, the whole graph loads (root = "Me"). */
  rootId?: string;
  load?: (entityId: string, depth: number) => Promise<EgoGraph>;
  loadFull?: () => Promise<EgoGraph>;
}

type Phase = "loading" | "ready" | "empty" | "error";

const SCALE_MIN = 0.45;
const SCALE_MAX = 2.4;

export function clampScale(s: number): number {
  return Math.max(SCALE_MIN, Math.min(SCALE_MAX, s));
}

/** A pan/zoom transform: content maps screen = world·scale + (tx, ty). */
export interface ViewTransform {
  scale: number;
  tx: number;
  ty: number;
}

/**
 * Zoom about a fixed screen point `(fx, fy)` (stage-local) so whatever sits
 * under it stays put across the scale change — the anchor-point invariant.
 */
export function focalZoom(v: ViewTransform, fx: number, fy: number, factor: number): ViewTransform {
  const scale = clampScale(v.scale * factor);
  const k = scale / v.scale;
  return { scale, tx: fx - (fx - v.tx) * k, ty: fy - (fy - v.ty) * k };
}

/** Humanize a predicate for an edge label: "worksFor" → "works for". */
export function edgeLabelText(predicate: string): string {
  return predicate
    .replace(/[_]+/g, " ")
    .replace(/([a-z0-9])([A-Z])/g, "$1 $2")
    .toLowerCase()
    .trim();
}

export interface EdgePlan {
  /** Symmetric fan index for parallel connections (0 = straight). */
  idx: number;
  arrowStart: boolean;
  arrowEnd: boolean;
  /** This edge is folded into its reciprocal partner and isn't drawn. */
  skip: boolean;
  label: string;
}

/**
 * Decide how each directed edge renders. A pair of opposite edges (A→B and
 * B→A) collapses into one connection with arrowheads at both ends; everything
 * else stays a directional arrow, with same-pair siblings fanned out.
 */
export function planEdges(
  edges: readonly { source: string; target: string; predicate: string }[],
): Map<string, EdgePlan> {
  const groups = new Map<string, { source: string; target: string; predicate: string }[]>();
  for (const e of edges) {
    const pair = e.source < e.target ? `${e.source}|${e.target}` : `${e.target}|${e.source}`;
    let list = groups.get(pair);
    if (!list) {
      list = [];
      groups.set(pair, list);
    }
    list.push(e);
  }
  const key = (e: { source: string; target: string; predicate: string }) =>
    `${e.source}|${e.target}|${e.predicate}`;
  const plan = new Map<string, EdgePlan>();
  for (const [pair, list] of groups) {
    const [p, q] = pair.split("|");
    if (list.length === 2) {
      const fwd = list.find((e) => e.source === p);
      const bwd = list.find((e) => e.source === q);
      if (fwd && bwd) {
        const fl = edgeLabelText(fwd.predicate);
        const bl = edgeLabelText(bwd.predicate);
        plan.set(key(fwd), {
          idx: 0,
          arrowStart: true,
          arrowEnd: true,
          skip: false,
          label: fl === bl ? fl : `${fl} · ${bl}`,
        });
        plan.set(key(bwd), { idx: 0, arrowStart: false, arrowEnd: false, skip: true, label: "" });
        continue;
      }
    }
    const k = list.length;
    list.forEach((e, i) => {
      const arrowEnd = e.target === q;
      plan.set(key(e), {
        idx: i - (k - 1) / 2,
        arrowStart: !arrowEnd,
        arrowEnd,
        skip: false,
        label: edgeLabelText(e.predicate),
      });
    });
  }
  return plan;
}

/** Minimal node shape the label grid needs. */
export interface LabelNode {
  x: number;
  y: number;
  op: number;
}

/**
 * Decide which node labels to show, screen-space and density-aware: bucket
 * on-screen nodes into cells and keep the highest-priority label per cell.
 * `forced` labels (focal, focus ring) always win; below a legibility floor only
 * forced labels render.
 */
export function chooseLabels(args: {
  nodes: ReadonlyMap<string, LabelNode>;
  scale: number;
  tx: number;
  ty: number;
  w: number;
  h: number;
  forced: ReadonlySet<string>;
  priority: (id: string) => number;
  cell?: number;
  fontPx?: number;
  floorPx?: number;
}): Set<string> {
  const { nodes, scale, tx, ty, w, h, forced, priority } = args;
  const cell = args.cell ?? 96;
  const fontPx = args.fontPx ?? 12;
  const floorPx = args.floorPx ?? 9;
  const shown = new Set<string>(forced);
  if (fontPx * scale < floorPx) return shown;
  const cols = Math.max(1, Math.ceil(w / cell));
  const best = new Map<number, string>();
  for (const [id, n] of nodes) {
    if (forced.has(id) || n.op < 0.5) continue;
    const sx = n.x * scale + tx;
    const sy = n.y * scale + ty;
    if (sx < 0 || sy < 0 || sx > w || sy > h) continue;
    const c = Math.floor(sy / cell) * cols + Math.floor(sx / cell);
    const cur = best.get(c);
    if (cur === undefined || priority(id) > priority(cur)) best.set(c, id);
  }
  for (const id of best.values()) shown.add(id);
  return shown;
}

const REDUCED = (): boolean =>
  typeof window !== "undefined" &&
  window.matchMedia?.("(prefers-reduced-motion: reduce)").matches === true;

// Disc diameters per hop (focal / 1-hop / 2-hop); radii are half of these and
// drive where edges meet the circle.
const DISC = { 0: 60, 1: 44, 2: 32 } as const;
const radiusOf = (hop: number) => (hop === 0 ? 30 : hop === 1 ? 22 : 16);
// First-ring fan: how many neighbours a node shows on the inner ring, and how
// many of each of theirs reach the outer ring — capped so the phone stays legible.
const FIRST_CAP = { 1: 12, 2: 8 } as const;
const SECOND_CAP = 4;

interface Pos {
  x: number;
  y: number;
  hop: number;
}

// Deterministic PRNG so a focal's layout is stable across renders (seeded from
// the focal id) — no Math.random, which would jitter the map every paint.
function mulberry32(seed: number): () => number {
  let a = seed;
  return () => {
    a |= 0;
    a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}
function hashStr(s: string): number {
  let h = 2166136261;
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return h >>> 0;
}

/**
 * Settle the local neighbourhood with a small force pass — charge repulsion,
 * collide (so type discs never overlap) and link springs — pinning the focal at
 * the origin. Runs to a fixed iteration count synchronously, so the map reads
 * organically (not as fixed rings) without any perpetual animation. Positions
 * are deterministic for a given focal/set, keeping re-paints stable.
 */
export function settleLocal(
  ids: readonly string[],
  links: readonly { source: string; target: string }[],
  hop: ReadonlyMap<string, number>,
  seed: string,
): Map<string, Pos> {
  const rng = mulberry32(hashStr(seed));
  const focal = ids.find((id) => hop.get(id) === 0);
  const P = new Map<string, { x: number; y: number; vx: number; vy: number }>();
  for (const id of ids) {
    if (id === focal) {
      P.set(id, { x: 0, y: 0, vx: 0, vy: 0 });
      continue;
    }
    const a = rng() * Math.PI * 2;
    const r = 90 + rng() * 120;
    P.set(id, { x: Math.cos(a) * r, y: Math.sin(a) * r, vx: 0, vy: 0 });
  }
  let alpha = 1;
  for (let it = 0; it < 300; it++) {
    alpha = Math.max(0.04, alpha * 0.985);
    for (let i = 0; i < ids.length; i++) {
      const idA = ids[i];
      const a = idA === undefined ? undefined : P.get(idA);
      if (!idA || !a) continue;
      for (let j = i + 1; j < ids.length; j++) {
        const idB = ids[j];
        const b = idB === undefined ? undefined : P.get(idB);
        if (!idB || !b) continue;
        const dx = a.x - b.x;
        const dy = a.y - b.y;
        let d2 = dx * dx + dy * dy;
        if (d2 < 1) d2 = 1;
        const d = Math.sqrt(d2);
        const rep = Math.min(80, 9000 / d2); // charge: capped inverse-square
        a.vx += (dx / d) * rep;
        a.vy += (dy / d) * rep;
        b.vx -= (dx / d) * rep;
        b.vy -= (dy / d) * rep;
        const min = radiusOf(hop.get(idA) ?? 2) + radiusOf(hop.get(idB) ?? 2) + 22;
        if (d < min) {
          const push = ((min - d) / d) * 0.5; // collide
          a.vx += dx * push;
          a.vy += dy * push;
          b.vx -= dx * push;
          b.vy -= dy * push;
        }
      }
    }
    for (const e of links) {
      const a = P.get(e.source);
      const b = P.get(e.target);
      if (!a || !b) continue;
      const dx = b.x - a.x;
      const dy = b.y - a.y;
      const d = Math.hypot(dx, dy) || 1;
      const rest = hop.get(e.source) === 0 || hop.get(e.target) === 0 ? 120 : 92;
      const f = (d - rest) * 0.04;
      a.vx += (dx / d) * f;
      a.vy += (dy / d) * f;
      b.vx -= (dx / d) * f;
      b.vy -= (dy / d) * f;
    }
    for (const id of ids) {
      const p = P.get(id);
      if (!p) continue;
      if (id === focal) {
        p.x = 0;
        p.y = 0;
        p.vx = 0;
        p.vy = 0;
        continue;
      }
      p.vx += -p.x * 0.006; // gentle centring keeps the set framed
      p.vy += -p.y * 0.006;
      p.vx *= 0.84;
      p.vy *= 0.84;
      const sp = Math.hypot(p.vx, p.vy);
      if (sp > 24) {
        p.vx *= 24 / sp;
        p.vy *= 24 / sp;
      }
      p.x += p.vx * alpha;
      p.y += p.vy * alpha;
    }
  }
  const out = new Map<string, Pos>();
  for (const id of ids) {
    const p = P.get(id);
    if (p) out.set(id, { x: p.x, y: p.y, hop: hop.get(id) ?? 2 });
  }
  return out;
}
interface Rel {
  id: string;
  name: string;
  kind: string;
  predicate: string;
  dir: "out" | "in";
}

export function GraphScreen({
  onOpenEntity,
  rootId,
  load = api.getNeighbors,
  loadFull = api.getFullGraph,
}: GraphScreenProps) {
  const [phase, setPhase] = useState<Phase>("loading");
  const [graph, setGraph] = useState<EgoGraph | null>(null);
  const [focal, setFocal] = useState<string | null>(null);
  const [depth, setDepth] = useState<1 | 2>(2);
  const [trail, setTrail] = useState<string[]>([]);
  const [query, setQuery] = useState("");
  const [searchOpen, setSearchOpen] = useState(false);
  const [panelH, setPanelH] = useState(300);
  // Entity types excluded from the on-graph neighbourhood (the focal is always
  // shown). The panel still lists every relationship; only the map is filtered.
  const [typeOff, setTypeOff] = useState<Set<EntityTypeKey>>(() => new Set());

  // The dataset both the map and the panel read: a named root's 2-hop ego when
  // given, else the whole graph (centred on "Me"). Re-centring explores within it.
  useEffect(() => {
    let stale = false;
    setPhase("loading");
    (rootId ? load(rootId, 2) : loadFull())
      .then((g) => {
        if (stale) return;
        setGraph(g);
        const start =
          rootId ??
          (g.root && g.nodes.some((n) => n.id === g.root)
            ? g.root
            : (mostConnected(g) ?? g.nodes[0]?.id ?? null));
        setFocal(start);
        setTrail(start ? [start] : []);
        setPhase(g.nodes.length > 0 ? "ready" : "empty");
      })
      .catch(() => {
        if (!stale) setPhase("error");
      });
    return () => {
      stale = true;
    };
  }, [rootId, load, loadFull]);

  const nodeById = useMemo(() => {
    const m = new Map<string, EgoGraph["nodes"][number]>();
    for (const n of graph?.nodes ?? []) m.set(n.id, n);
    return m;
  }, [graph]);

  // Adjacency (ordered, deduped) + the representative edge per neighbour pair,
  // used for the ring layout and the panel's relationship rows.
  const { adjacency, relOf, degree } = useMemo(() => {
    const adj = new Map<string, string[]>();
    const rel = new Map<string, Map<string, { predicate: string; dir: "out" | "in" }>>();
    const seen = (m: Map<string, string[]>, a: string) => {
      let s = m.get(a);
      if (!s) {
        s = [];
        m.set(a, s);
      }
      return s;
    };
    const relSeen = (a: string) => {
      let s = rel.get(a);
      if (!s) {
        s = new Map();
        rel.set(a, s);
      }
      return s;
    };
    for (const e of graph?.edges ?? []) {
      const sa = seen(adj, e.source);
      const ta = seen(adj, e.target);
      if (!sa.includes(e.target)) sa.push(e.target);
      if (!ta.includes(e.source)) ta.push(e.source);
      const rs = relSeen(e.source);
      if (!rs.has(e.target)) rs.set(e.target, { predicate: e.predicate, dir: "out" });
      const rt = relSeen(e.target);
      if (!rt.has(e.source)) rt.set(e.source, { predicate: e.predicate, dir: "in" });
    }
    const deg = new Map<string, number>();
    for (const n of graph?.nodes ?? []) deg.set(n.id, adj.get(n.id)?.length ?? 0);
    return { adjacency: adj, relOf: rel, degree: deg };
  }, [graph]);

  // The local layout for the current focal + depth + type filter, in world
  // coordinates with the focal pinned at the origin. We select the same capped
  // 1–2 hop neighbourhood (so it never becomes a hairball), excluding any
  // filtered-out types, then settle it with a small force pass so the map reads
  // organically rather than as fixed rings.
  const layout = useMemo(() => {
    if (!focal || !nodeById.has(focal)) return new Map<string, Pos>();
    const allowed = (id: string) =>
      id === focal || !typeOff.has(resolveEntityKind(nodeById.get(id)?.kind ?? "Thing"));
    const first = (adjacency.get(focal) ?? []).filter(allowed).slice(0, FIRST_CAP[depth]);
    const placed = new Set<string>([focal, ...first]);
    const hop = new Map<string, number>([[focal, 0]]);
    for (const id of first) hop.set(id, 1);
    if (depth === 2) {
      for (const fid of first) {
        let n = 0;
        for (const sid of adjacency.get(fid) ?? []) {
          if (n >= SECOND_CAP) break;
          if (placed.has(sid) || !allowed(sid)) continue;
          placed.add(sid);
          hop.set(sid, 2);
          n++;
        }
      }
    }
    const ids = [...placed];
    const links = (graph?.edges ?? []).filter((e) => placed.has(e.source) && placed.has(e.target));
    return settleLocal(ids, links, hop, focal);
  }, [focal, depth, adjacency, nodeById, graph, typeOff]);

  // Visible edges (both endpoints placed) with reciprocal-merge + geometry.
  const edges = useMemo(() => {
    const vis = (graph?.edges ?? []).filter((e) => layout.has(e.source) && layout.has(e.target));
    const plan = planEdges(vis);
    const out: {
      key: string;
      x1: number;
      y1: number;
      x2: number;
      y2: number;
      mx: number;
      my: number;
      label: string;
      arrowStart: boolean;
      arrowEnd: boolean;
      showLabel: boolean;
    }[] = [];
    for (const e of vis) {
      const key = `${e.source}|${e.target}|${e.predicate}`;
      const pl = plan.get(key);
      if (pl?.skip) continue;
      const a = layout.get(e.source);
      const b = layout.get(e.target);
      if (!a || !b) continue;
      const dx = b.x - a.x;
      const dy = b.y - a.y;
      const len = Math.hypot(dx, dy) || 1;
      const ra = radiusOf(a.hop);
      const rb = radiusOf(b.hop);
      out.push({
        key,
        x1: a.x + (dx / len) * ra,
        y1: a.y + (dy / len) * ra,
        x2: b.x - (dx / len) * rb,
        y2: b.y - (dy / len) * rb,
        mx: (a.x + b.x) / 2,
        my: (a.y + b.y) / 2,
        label: pl?.label ?? edgeLabelText(e.predicate),
        arrowStart: pl?.arrowStart ?? false,
        arrowEnd: pl?.arrowEnd ?? true,
        // every connection is labelled the same way — show wherever the segment
        // has room, so labels don't pile up on very short edges.
        showLabel: len > 46,
      });
    }
    return out;
  }, [graph, layout]);

  // The focal's direct relationships, for the panel rows (unique neighbours).
  const rels = useMemo<Rel[]>(() => {
    if (!focal) return [];
    const out: Rel[] = [];
    for (const [id, meta] of relOf.get(focal) ?? []) {
      const n = nodeById.get(id);
      if (!n) continue;
      out.push({
        id,
        name: n.canonical_name,
        kind: n.kind,
        predicate: meta.predicate,
        dir: meta.dir,
      });
    }
    return out;
  }, [focal, relOf, nodeById]);

  // Entity types present in the whole graph, with counts — drives the filter
  // chips. Ordered most-common-first so the busiest types lead.
  const kinds = useMemo(() => {
    const counts = new Map<EntityTypeKey, number>();
    for (const n of graph?.nodes ?? []) {
      const k = resolveEntityKind(n.kind);
      counts.set(k, (counts.get(k) ?? 0) + 1);
    }
    return [...counts.entries()].sort((a, b) => b[1] - a[1]);
  }, [graph]);

  const searchResults = useMemo(() => {
    const all = graph?.nodes ?? [];
    const q = query.trim().toLowerCase();
    const matches = q
      ? all.filter((n) => n.canonical_name.toLowerCase().includes(q))
      : [...all].sort((a, b) => (degree.get(b.id) ?? 0) - (degree.get(a.id) ?? 0));
    return matches.slice(0, 10);
  }, [graph, query, degree]);

  // ---- view transform (pan/zoom), applied imperatively ----
  const stageRef = useRef<HTMLDivElement>(null);
  const viewportRef = useRef<HTMLDivElement>(null);
  const nodeEls = useRef(new Map<string, HTMLButtonElement>());
  const view = useRef<ViewTransform>({ scale: 1, tx: 0, ty: 0 });

  const updateLabels = useCallback(() => {
    const stage = stageRef.current;
    const w = stage?.clientWidth || 360;
    const h = stage?.clientHeight || 560;
    const forced = new Set<string>();
    if (focal) forced.add(focal);
    for (const [id, p] of layout) if (p.hop === 1) forced.add(id);
    const ln = new Map<string, LabelNode>();
    for (const [id, p] of layout) ln.set(id, { x: p.x, y: p.y, op: 1 });
    const shown = chooseLabels({
      nodes: ln,
      scale: view.current.scale,
      tx: view.current.tx,
      ty: view.current.ty,
      w,
      h,
      forced,
      priority: (id) => -(layout.get(id)?.hop ?? 9),
    });
    for (const [id, el] of nodeEls.current) el.dataset.label = shown.has(id) ? "on" : "off";
  }, [focal, layout]);

  const applyView = useCallback(() => {
    const vp = viewportRef.current;
    const v = view.current;
    if (vp) vp.style.transform = `translate(${v.tx}px, ${v.ty}px) scale(${v.scale})`;
    updateLabels();
  }, [updateLabels]);

  // Centre the fresh local layout whenever the focal, depth, or panel size
  // changes — the focal (world origin) sits at the middle of the stage.
  // biome-ignore lint/correctness/useExhaustiveDependencies: panelH is read via the stage size at apply time; it must retrigger centring.
  useEffect(() => {
    const stage = stageRef.current;
    view.current = {
      scale: 1,
      tx: (stage?.clientWidth || 360) / 2,
      ty: (stage?.clientHeight || 560) / 2,
    };
    applyView();
  }, [focal, depth, panelH, applyView]);

  useEffect(() => {
    const onResize = () => applyView();
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, [applyView]);

  // ---- navigation ----
  const recenter = useCallback(
    (id: string) => {
      if (!nodeById.has(id)) {
        onOpenEntity(id);
        return;
      }
      setFocal(id);
      setTrail((t) => {
        const i = t.indexOf(id);
        return i >= 0 ? t.slice(0, i + 1) : [...t, id];
      });
    },
    [nodeById, onOpenEntity],
  );
  const toggleType = useCallback((k: EntityTypeKey) => {
    setTypeOff((prev) => {
      const next = new Set(prev);
      if (next.has(k)) next.delete(k);
      else next.add(k);
      return next;
    });
  }, []);
  const jumpCrumb = (i: number) => {
    setTrail((t) => t.slice(0, i + 1));
    setFocal(trail[i] ?? null);
  };
  const pickSearch = (id: string) => {
    setSearchOpen(false);
    setQuery("");
    setFocal(id);
    setTrail([id]);
  };

  // ---- pan / pinch / wheel / double-tap ----
  const ptrs = useRef(new Map<number, { x: number; y: number }>());
  const gesture = useRef({ sd: 0, ss: 1, panX: 0, panY: 0, midX: 0, midY: 0 });
  const stageOffset = () => {
    const r = stageRef.current?.getBoundingClientRect();
    return { ox: r?.left ?? 0, oy: r?.top ?? 0 };
  };
  function onPointerDown(e: PointerEvent<HTMLDivElement>) {
    if (searchOpen) setSearchOpen(false);
    if ((e.target as HTMLElement).closest(".graph-node, .graph-depth")) return;
    e.currentTarget.setPointerCapture?.(e.pointerId);
    ptrs.current.set(e.pointerId, { x: e.clientX, y: e.clientY });
    const g = gesture.current;
    const { ox, oy } = stageOffset();
    if (ptrs.current.size === 1) {
      g.panX = e.clientX - view.current.tx;
      g.panY = e.clientY - view.current.ty;
    } else if (ptrs.current.size === 2) {
      const [a, b] = [...ptrs.current.values()];
      if (!a || !b) return;
      g.sd = Math.hypot(a.x - b.x, a.y - b.y);
      g.ss = view.current.scale;
      g.midX = (a.x + b.x) / 2 - ox;
      g.midY = (a.y + b.y) / 2 - oy;
    }
  }
  function onPointerMove(e: PointerEvent<HTMLDivElement>) {
    if (!ptrs.current.has(e.pointerId)) return;
    ptrs.current.set(e.pointerId, { x: e.clientX, y: e.clientY });
    const g = gesture.current;
    const v = view.current;
    if (ptrs.current.size === 1) {
      v.tx = e.clientX - g.panX;
      v.ty = e.clientY - g.panY;
      applyView();
    } else if (ptrs.current.size === 2 && g.sd > 0) {
      const [a, b] = [...ptrs.current.values()];
      if (!a || !b) return;
      const { ox, oy } = stageOffset();
      const d = Math.hypot(a.x - b.x, a.y - b.y);
      const mx = (a.x + b.x) / 2 - ox;
      const my = (a.y + b.y) / 2 - oy;
      const s1 = clampScale(g.ss * (d / g.sd));
      const k = s1 / v.scale;
      v.tx = mx - (mx - v.tx) * k;
      v.ty = my - (my - v.ty) * k;
      v.scale = s1;
      v.tx += mx - g.midX;
      v.ty += my - g.midY;
      g.midX = mx;
      g.midY = my;
      applyView();
    }
  }
  function onPointerUp(e: PointerEvent<HTMLDivElement>) {
    e.currentTarget.releasePointerCapture?.(e.pointerId);
    ptrs.current.delete(e.pointerId);
    const g = gesture.current;
    if (ptrs.current.size < 2) g.sd = 0;
    const p = [...ptrs.current.values()][0];
    if (p) {
      g.panX = p.x - view.current.tx;
      g.panY = p.y - view.current.ty;
    }
  }

  // Desktop wheel / trackpad-pinch zoom, focal at the cursor.
  useEffect(() => {
    const stage = stageRef.current;
    if (!stage) return;
    const handler = (e: WheelEvent) => {
      if ((e.target as HTMLElement).closest(".graph-depth")) return;
      e.preventDefault();
      const r = stage.getBoundingClientRect();
      const next = focalZoom(
        view.current,
        e.clientX - r.left,
        e.clientY - r.top,
        Math.exp(-e.deltaY * 0.0015),
      );
      view.current = next;
      applyView();
    };
    stage.addEventListener("wheel", handler, { passive: false });
    return () => stage.removeEventListener("wheel", handler);
  }, [applyView]);

  // ---- draggable panel handle ----
  const drag = useRef<{ y: number; h: number } | null>(null);
  function onHandleDown(e: PointerEvent<HTMLDivElement>) {
    drag.current = { y: e.clientY, h: panelH };
    e.currentTarget.setPointerCapture?.(e.pointerId);
  }
  function onHandleMove(e: PointerEvent<HTMLDivElement>) {
    const d = drag.current;
    if (!d) return;
    const max = (stageRef.current?.parentElement?.clientHeight ?? 640) - 120;
    setPanelH(Math.max(140, Math.min(max, d.h + (d.y - e.clientY))));
  }
  function onHandleUp() {
    drag.current = null;
  }

  if (phase === "loading") return <main className="screen-body graph-state">loading graph…</main>;
  if (phase === "error")
    return (
      <main className="screen-body graph-state">
        couldn't load the graph — check the connection.
      </main>
    );
  if (phase === "empty" || !graph || !focal)
    return (
      <main className="screen-body graph-state">
        no entities yet — they appear as notes are analyzed.
      </main>
    );

  const focalNode = nodeById.get(focal);
  const reduced = REDUCED();

  return (
    <main className="screen-body graph-screen">
      <div className="graph-search-bar">
        <div className="graph-search">
          <SearchIcon size={18} />
          <input
            type="text"
            className="graph-search-input"
            placeholder="Jump to an entity…"
            value={query}
            onChange={(e) => {
              setQuery(e.target.value);
              setSearchOpen(true);
            }}
            onFocus={() => setSearchOpen(true)}
            aria-label="Search entities"
          />
        </div>
        {searchOpen && searchResults.length > 0 && (
          <ul className="graph-results" aria-label="Search results">
            {searchResults.map((n) => (
              <li key={n.id}>
                <button type="button" className="graph-result" onClick={() => pickSearch(n.id)}>
                  <EntityTypeIcon kind={n.kind} size={28} />
                  <span className="graph-result-name">{n.canonical_name}</span>
                  <span className="graph-result-kind">
                    {KIND_LABEL[resolveEntityKind(n.kind)]} · {degree.get(n.id) ?? 0}
                  </span>
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>

      <div className="graph-crumbs" aria-label="Breadcrumb">
        {trail.map((id, i) => (
          <button
            type="button"
            key={id}
            className={`graph-crumb${i === trail.length - 1 ? " is-current" : ""}`}
            onClick={() => jumpCrumb(i)}
          >
            {nodeById.get(id)?.canonical_name ?? "—"}
          </button>
        ))}
      </div>

      {kinds.length > 1 && (
        <fieldset className="graph-filters" aria-label="Filter by type">
          {kinds.map(([k, count]) => {
            const on = !typeOff.has(k);
            return (
              <button
                type="button"
                key={k}
                className={`graph-filter${on ? "" : " is-off"}`}
                aria-pressed={on}
                onClick={() => toggleType(k)}
              >
                <span className="graph-filter-dot" style={{ background: ENTITY_TYPE_COLOR[k] }} />
                {KIND_LABEL[k]}
                <span className="graph-filter-count">{count}</span>
              </button>
            );
          })}
        </fieldset>
      )}

      <div
        className="graph-stage"
        ref={stageRef}
        role="application"
        aria-label="Entity graph — tap a node to re-centre, pinch to zoom"
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onPointerCancel={onPointerUp}
      >
        <div className="graph-viewport" ref={viewportRef} data-reduced={reduced}>
          {/* biome-ignore lint/a11y/noSvgWithoutTitle: edges are decorative; nodes carry the labels. */}
          <svg className="graph-edges">
            <defs>
              <marker
                id="graph-arrow"
                viewBox="0 0 8 8"
                refX="8"
                refY="4"
                markerWidth="6"
                markerHeight="6"
                orient="auto-start-reverse"
              >
                <path d="M0 0 L8 4 L0 8 Z" />
              </marker>
            </defs>
            {edges.map((e) => (
              <g key={e.key}>
                <line
                  x1={e.x1}
                  y1={e.y1}
                  x2={e.x2}
                  y2={e.y2}
                  className="graph-edge"
                  markerStart={e.arrowStart ? "url(#graph-arrow)" : undefined}
                  markerEnd={e.arrowEnd ? "url(#graph-arrow)" : undefined}
                />
                {e.showLabel && (
                  <text className="graph-edge-label" x={e.mx} y={e.my}>
                    {e.label}
                  </text>
                )}
              </g>
            ))}
          </svg>
          {[...layout].map(([id, p]) => {
            const node = nodeById.get(id);
            if (!node) return null;
            return (
              <button
                type="button"
                key={id}
                ref={(el) => {
                  if (el) nodeEls.current.set(id, el);
                  else nodeEls.current.delete(id);
                }}
                className={`graph-node${p.hop === 0 ? " is-focal" : ""}`}
                data-hop={p.hop}
                style={{ left: `${p.x}px`, top: `${p.y}px` }}
                aria-label={node.canonical_name}
                onClick={() => recenter(id)}
              >
                <EntityTypeIcon kind={node.kind} size={DISC[p.hop as 0 | 1 | 2] ?? 32} />
                <span className="graph-node-label">{node.canonical_name}</span>
              </button>
            );
          })}
        </div>

        <fieldset className="graph-depth" aria-label="Depth">
          <button
            type="button"
            className={depth === 1 ? "is-on" : ""}
            aria-pressed={depth === 1}
            onClick={() => setDepth(1)}
          >
            1 hop
          </button>
          <button
            type="button"
            className={depth === 2 ? "is-on" : ""}
            aria-pressed={depth === 2}
            onClick={() => setDepth(2)}
          >
            2 hops
          </button>
        </fieldset>

        {rels.length === 0 && (
          <p className="graph-empty-note">
            no connections yet — links appear as more notes are analyzed.
          </p>
        )}
      </div>

      <section className="graph-panel" style={{ height: `${panelH}px` }} aria-label="Entity detail">
        <div
          className="graph-panel-grab"
          onPointerDown={onHandleDown}
          onPointerMove={onHandleMove}
          onPointerUp={onHandleUp}
          onPointerCancel={onHandleUp}
        >
          <span className="graph-panel-handle" aria-hidden="true" />
        </div>
        <header className="graph-panel-head">
          {focalNode && <EntityTypeIcon kind={focalNode.kind} size={46} />}
          <div className="graph-panel-meta">
            <h2 className="graph-panel-title">{focalNode?.canonical_name ?? "—"}</h2>
            <p className="graph-panel-sub">
              {focalNode ? KIND_LABEL[resolveEntityKind(focalNode.kind)] : ""} · {rels.length} links
              {focalNode && focalNode.domain !== "general" && (
                <span className="graph-panel-domain"> · ● {focalNode.domain} · firewalled</span>
              )}
            </p>
          </div>
        </header>
        <div className="graph-panel-body">
          {rels.length > 0 ? (
            <>
              <p className="graph-rels-head">{rels.length} relationships</p>
              <ul className="graph-rels">
                {rels.map((r) => (
                  <li key={`${r.id}-${r.predicate}-${r.dir}`}>
                    <button type="button" className="graph-rel" onClick={() => recenter(r.id)}>
                      <EntityTypeIcon kind={r.kind} size={34} />
                      <span className="graph-rel-mid">
                        <span className="graph-rel-obj">{r.name}</span>
                        <span className="graph-rel-pred">
                          {r.dir === "out" ? r.predicate : `${r.predicate} of`}
                        </span>
                      </span>
                      <ChevronRightIcon size={18} />
                    </button>
                  </li>
                ))}
              </ul>
            </>
          ) : (
            <p className="graph-rels-empty">no relationships yet.</p>
          )}
        </div>
        <footer className="graph-panel-foot">
          <button
            type="button"
            className="graph-panel-open"
            onClick={() => focal && onOpenEntity(focal)}
          >
            Open entity →
          </button>
        </footer>
      </section>
    </main>
  );
}

// Friendlier chip labels than the raw schema keys.
const KIND_LABEL: Record<EntityTypeKey, string> = {
  Person: "Person",
  Organization: "Organization",
  Place: "Place",
  Event: "Event",
  Product: "Product",
  Animal: "Animal",
  CreativeWork: "Media",
  MedicalCondition: "Condition",
  MedicalProcedure: "Procedure",
  Drug: "Drug",
  Thing: "Thing",
};

/** The most-connected node — the sensible focal when a graph has no "Me" root. */
function mostConnected(g: EgoGraph): string | null {
  const deg = new Map<string, number>();
  for (const e of g.edges) {
    deg.set(e.source, (deg.get(e.source) ?? 0) + 1);
    deg.set(e.target, (deg.get(e.target) ?? 0) + 1);
  }
  let best: string | null = null;
  let bestN = -1;
  for (const n of g.nodes) {
    const d = deg.get(n.id) ?? 0;
    if (d > bestN) {
      bestN = d;
      best = n.id;
    }
  }
  return best;
}
