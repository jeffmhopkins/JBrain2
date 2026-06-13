// The entity graph "Map": a force-directed overview that drills into a radial
// ego focus on tap (docs/DESIGN.md, graph-view mockups). One dataset (the
// root's 2-hop ego) backs both modes; focus re-lays-out the same nodes around
// a focal entity, so node elements persist across the transition (object
// constancy) and the force layout is never re-run on a filter — survivors keep
// their positions, filtered types just fade. Return to overview is an explicit
// labelled control, never a hidden gesture. The chip bar doubles as the legend:
// "All" is the empty selection, and each tap toggles a type (additive).

import {
  type KeyboardEvent,
  type MouseEvent,
  type PointerEvent,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { type EgoGraph, type EntityList, type EntityOut, api } from "../api/client";
import { Sheet } from "../components/Sheet";
import { ChevronLeftIcon, FitIcon, MinusIcon, PlusIcon } from "../components/icons";
import { EntityTypeIcon, type EntityTypeKey, resolveEntityKind } from "../entities/kinds";

interface GraphScreenProps {
  onOpenEntity: (entityId: string) => void;
  /** Entity to center on; when absent, the most-recently-seen entity is used. */
  rootId?: string;
  load?: (entityId: string, depth: number) => Promise<EgoGraph>;
  list?: (q?: string, kind?: string) => Promise<EntityList>;
}

type Phase = "loading" | "ready" | "empty" | "error";
type Mode = "overview" | "focus";

interface Sim {
  x: number;
  y: number;
  vx: number;
  vy: number;
  tx: number;
  ty: number;
  op: number;
  top: number;
}

// Friendlier chip labels than the raw schema keys.
const KIND_LABEL: Record<EntityTypeKey, string> = {
  Person: "People",
  Organization: "Orgs",
  Place: "Places",
  Event: "Events",
  Product: "Products",
  Animal: "Animals",
  CreativeWork: "Media",
  MedicalCondition: "Conditions",
  MedicalProcedure: "Procedures",
  Drug: "Drugs",
  Thing: "Things",
};

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
 * With `transform-origin: 0 0`, naively changing `scale` pivots about the
 * stage corner, which is the "doesn't zoom where I want" bug; this counters it.
 */
export function focalZoom(v: ViewTransform, fx: number, fy: number, factor: number): ViewTransform {
  const scale = clampScale(v.scale * factor);
  const k = scale / v.scale;
  return { scale, tx: fx - (fx - v.tx) * k, ty: fy - (fy - v.ty) * k };
}

/** Minimal node shape the label grid needs (Sim is a structural superset). */
export interface LabelNode {
  x: number;
  y: number;
  op: number;
}

/**
 * Decide which node labels to show, screen-space and density-aware (sigma.js
 * LabelGrid): bucket on-screen nodes into ~cell-sized cells and keep the
 * highest-priority label per cell, so labels appear wherever there's actually
 * room — not only when zoomed past a fixed scale. `forced` labels (focal, the
 * tapped node, the focus ring) always win; below a legibility floor only forced
 * labels render. Deterministic priority keeps the choice stable (no flicker).
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
  if (fontPx * scale < floorPx) return shown; // text too small to read; forced only
  const cols = Math.max(1, Math.ceil(w / cell));
  const best = new Map<number, string>();
  for (const [id, n] of nodes) {
    if (forced.has(id) || n.op < 0.5) continue;
    const sx = n.x * scale + tx;
    const sy = n.y * scale + ty;
    if (sx < 0 || sy < 0 || sx > w || sy > h) continue; // never label off-screen
    const c = Math.floor(sy / cell) * cols + Math.floor(sx / cell);
    const cur = best.get(c);
    if (cur === undefined || priority(id) > priority(cur)) best.set(c, id);
  }
  for (const id of best.values()) shown.add(id);
  return shown;
}

function selSummary(sel: ReadonlySet<EntityTypeKey>): string {
  if (sel.size === 1) {
    const [only] = sel;
    if (only) return `only ${KIND_LABEL[only].toLowerCase()}`;
  }
  return `${sel.size} types`;
}

const REDUCED = (): boolean =>
  typeof window !== "undefined" &&
  window.matchMedia?.("(prefers-reduced-motion: reduce)").matches === true;

export function GraphScreen({
  onOpenEntity,
  rootId,
  load = api.getNeighbors,
  list = api.listEntities,
}: GraphScreenProps) {
  const [phase, setPhase] = useState<Phase>("loading");
  const [graph, setGraph] = useState<EgoGraph | null>(null);
  const [root, setRoot] = useState<string | null>(rootId ?? null);
  const [mode, setMode] = useState<Mode>("overview");
  const [focal, setFocal] = useState<string | null>(null);
  const [trail, setTrail] = useState<string[]>([]);
  const [sel, setSel] = useState<ReadonlySet<EntityTypeKey>>(new Set());
  const [sheetId, setSheetId] = useState<string | null>(null);
  const [focusEmpty, setFocusEmpty] = useState(false);

  // Resolve a root: the prop wins; otherwise the most-recently-seen entity.
  useEffect(() => {
    if (rootId) {
      setRoot(rootId);
      return;
    }
    let stale = false;
    list()
      .then((r) => {
        if (stale) return;
        const first = r.items[0]?.id ?? null;
        setRoot(first);
        if (first === null) setPhase("empty");
      })
      .catch(() => {
        if (!stale) setPhase("error");
      });
    return () => {
      stale = true;
    };
  }, [rootId, list]);

  // Load the root's 2-hop ego — the single dataset both modes draw from.
  useEffect(() => {
    if (!root) return;
    let stale = false;
    setPhase("loading");
    load(root, 2)
      .then((g) => {
        if (stale) return;
        setGraph(g);
        setMode("overview");
        setFocal(null);
        setTrail([]);
        setPhase(g.nodes.length > 0 ? "ready" : "empty");
      })
      .catch(() => {
        if (!stale) setPhase("error");
      });
    return () => {
      stale = true;
    };
  }, [root, load]);

  // ---- derived graph structure ----
  const nodeKind = useMemo(() => {
    const m = new Map<string, EntityTypeKey>();
    for (const n of graph?.nodes ?? []) m.set(n.id, resolveEntityKind(n.kind));
    return m;
  }, [graph]);

  const adjacency = useMemo(() => {
    const m = new Map<string, Set<string>>();
    const link = (a: string, b: string) => {
      let set = m.get(a);
      if (!set) {
        set = new Set();
        m.set(a, set);
      }
      set.add(b);
    };
    for (const e of graph?.edges ?? []) {
      link(e.source, e.target);
      link(e.target, e.source);
    }
    return m;
  }, [graph]);

  const hops = useMemo(() => {
    const m = new Map<string, number>();
    if (!graph) return m;
    m.set(graph.root, 0);
    let frontier = [graph.root];
    for (let d = 1; d <= 2 && frontier.length; d++) {
      const next: string[] = [];
      for (const id of frontier)
        for (const nb of adjacency.get(id) ?? [])
          if (!m.has(nb)) {
            m.set(nb, d);
            next.push(nb);
          }
      frontier = next;
    }
    return m;
  }, [graph, adjacency]);

  const kindsPresent = useMemo(() => {
    const counts = new Map<EntityTypeKey, number>();
    for (const k of nodeKind.values()) counts.set(k, (counts.get(k) ?? 0) + 1);
    return [...counts.entries()]
      .sort((a, b) => b[1] - a[1])
      .map(([k, n]) => ({ kind: k, count: n }));
  }, [nodeKind]);

  const isHidden = useCallback(
    (id: string) => sel.size > 0 && !sel.has(nodeKind.get(id) ?? "Thing"),
    [sel, nodeKind],
  );

  const hiddenCount = useMemo(
    () => (graph ? graph.nodes.filter((n) => isHidden(n.id)).length : 0),
    [graph, isHidden],
  );

  // ---- imperative animation state (read by the rAF loop) ----
  const stageRef = useRef<HTMLDivElement>(null);
  const viewportRef = useRef<HTMLDivElement>(null);
  const nodeEls = useRef(new Map<string, HTMLButtonElement>());
  const edgeEls = useRef(new Map<string, SVGLineElement>());
  const labelShown = useRef(new Map<string, boolean>()); // last data-label per node
  const vw = useRef({
    nodes: new Map<string, Sim>(),
    mode: "overview" as Mode,
    focal: null as string | null,
    hidden: (_id: string): boolean => false,
    visAdj: new Map<string, string[]>(),
    scale: 1,
    tx: 0,
    ty: 0,
    w: 360,
    h: 560,
  });

  const edgeKey = (s: string, t: string, p: string) => `${s}|${t}|${p}`;

  // Build the sim + run the loop whenever the dataset changes.
  useEffect(() => {
    if (!graph) return;
    const stage = stageRef.current;
    const w = stage?.clientWidth || 360;
    const h = stage?.clientHeight || 560;
    vw.current.w = w;
    vw.current.h = h;
    const nodes = new Map<string, Sim>();
    for (const n of graph.nodes) {
      const d = hops.get(n.id) ?? 2;
      const r = d * 130 + Math.random() * 30;
      const a = Math.random() * Math.PI * 2;
      nodes.set(n.id, {
        x: w / 2 + Math.cos(a) * r,
        y: h / 2 + Math.sin(a) * r,
        vx: 0,
        vy: 0,
        tx: w / 2,
        ty: h / 2,
        op: 1,
        top: 1,
      });
    }
    const rootSim = nodes.get(graph.root);
    if (rootSim) {
      rootSim.x = w / 2;
      rootSim.y = h / 2;
    }
    vw.current.nodes = nodes;
    vw.current.scale = 1;
    vw.current.tx = 0;
    vw.current.ty = 0;

    let raf = 0;
    const reduced = REDUCED();
    const frame = () => {
      step(reduced);
      raf = requestAnimationFrame(frame);
    };
    raf = requestAnimationFrame(frame);
    return () => cancelAnimationFrame(raf);
  }, [graph, hops]);

  // Recompute targets/visibility whenever mode, focus, or the filter changes.
  // biome-ignore lint/correctness/useExhaustiveDependencies: relayout reads refs and stable derived maps; listing them would force needless reruns.
  useEffect(() => {
    const v = vw.current;
    v.mode = mode;
    v.focal = focal;
    v.hidden = isHidden;
    const vis = new Map<string, string[]>();
    for (const id of v.nodes.keys())
      vis.set(
        id,
        [...(adjacency.get(id) ?? [])].filter((n) => !isHidden(n)),
      );
    v.visAdj = vis;
    relayout();
  }, [mode, focal, sel, graph, adjacency, isHidden]);

  function relayout() {
    const v = vw.current;
    const cx = v.w / 2;
    const cy = v.h / 2;
    if (v.mode === "overview" || !v.focal) {
      for (const [id, n] of v.nodes) {
        n.top = v.hidden(id) ? 0 : 1;
        nodeEls.current.get(id)?.removeAttribute("data-anchor"); // labels below
      }
      setFocusEmpty(false);
      return;
    }
    const fo = v.focal;
    const ring = (v.visAdj.get(fo) ?? []).slice();
    const f = v.nodes.get(fo);
    if (f) {
      f.tx = cx;
      f.ty = cy;
      f.top = 1;
      nodeEls.current.get(fo)?.removeAttribute("data-anchor");
    }
    const R = Math.min(v.w, v.h) * 0.34;
    ring.forEach((id, i) => {
      const a = -Math.PI / 2 + (i * 2 * Math.PI) / ring.length;
      const n = v.nodes.get(id);
      if (n) {
        n.tx = cx + R * Math.cos(a);
        n.ty = cy + R * Math.sin(a);
        n.top = 1;
        // place the label on the node's outer side so it clears the spokes
        nodeEls.current.get(id)?.setAttribute("data-anchor", n.ty < cy ? "top" : "bottom");
      }
    });
    const onRing = new Set(ring);
    for (const [id, n] of v.nodes)
      if (id !== fo && !onRing.has(id)) {
        n.tx = n.x;
        n.ty = n.y;
        n.top = 0;
      }
    setFocusEmpty(ring.length === 0);
  }

  function step(reduced: boolean) {
    const v = vw.current;
    if (v.mode === "overview") physics(v);
    else {
      const k = reduced ? 1 : 0.16;
      for (const n of v.nodes.values()) {
        n.x += (n.tx - n.x) * k;
        n.y += (n.ty - n.y) * k;
      }
    }
    const ok = reduced ? 1 : 0.2;
    for (const n of v.nodes.values()) n.op += (n.top - n.op) * ok;
    paint(v);
  }

  function physics(v: typeof vw.current) {
    const live = [...v.nodes.entries()].filter(([id]) => !v.hidden(id));
    for (let i = 0; i < live.length; i++) {
      const ei = live[i];
      if (!ei) continue;
      for (let j = i + 1; j < live.length; j++) {
        const ej = live[j];
        if (!ej) continue;
        const a = ei[1];
        const b = ej[1];
        const dx = a.x - b.x;
        const dy = a.y - b.y;
        const d2 = dx * dx + dy * dy || 1;
        const d = Math.sqrt(d2);
        const rep = 2600 / d2;
        a.vx += (dx / d) * rep;
        a.vy += (dy / d) * rep;
        b.vx -= (dx / d) * rep;
        b.vy -= (dy / d) * rep;
      }
    }
    const rootId = graph?.root;
    for (const e of graph?.edges ?? []) {
      const a = v.nodes.get(e.source);
      const b = v.nodes.get(e.target);
      if (!a || !b || v.hidden(e.source) || v.hidden(e.target)) continue;
      const dx = b.x - a.x;
      const dy = b.y - a.y;
      const d = Math.sqrt(dx * dx + dy * dy) || 1;
      const f = (d - 118) * 0.012;
      a.vx += (dx / d) * f;
      a.vy += (dy / d) * f;
      b.vx -= (dx / d) * f;
      b.vy -= (dy / d) * f;
    }
    for (const [id, n] of live) {
      if (id === rootId) {
        n.x = v.w / 2;
        n.y = v.h / 2;
        n.vx = 0;
        n.vy = 0;
        continue;
      }
      n.vx += (v.w / 2 - n.x) * 0.004;
      n.vy += (v.h / 2 - n.y) * 0.004;
      n.vx *= 0.82;
      n.vy *= 0.82;
      n.x += n.vx;
      n.y += n.vy;
    }
  }

  function paint(v: typeof vw.current) {
    for (const [id, n] of v.nodes) {
      const el = nodeEls.current.get(id);
      if (!el) continue;
      el.style.left = `${n.x}px`;
      el.style.top = `${n.y}px`;
      el.style.opacity = `${n.op}`;
      el.style.pointerEvents = n.op > 0.5 ? "auto" : "none";
    }
    for (const e of graph?.edges ?? []) {
      const line = edgeEls.current.get(edgeKey(e.source, e.target, e.predicate));
      const a = v.nodes.get(e.source);
      const b = v.nodes.get(e.target);
      if (!line || !a || !b) continue;
      line.setAttribute("x1", `${a.x}`);
      line.setAttribute("y1", `${a.y}`);
      line.setAttribute("x2", `${b.x}`);
      line.setAttribute("y2", `${b.y}`);
      const shown =
        !v.hidden(e.source) &&
        !v.hidden(e.target) &&
        (v.mode === "overview" || e.source === v.focal || e.target === v.focal);
      line.style.opacity = shown ? `${Math.min(a.op, b.op)}` : "0";
    }
    const vp = viewportRef.current;
    if (vp) vp.style.transform = `translate(${v.tx}px, ${v.ty}px) scale(${v.scale})`;
    paintLabels(v);
  }

  // Per-node label visibility via the screen-space grid; the focal, the focus
  // ring, and the tapped node are forced on so tap-to-read never needs a zoom.
  function paintLabels(v: typeof vw.current) {
    const forced = new Set<string>();
    if (v.focal) {
      forced.add(v.focal);
      if (v.mode === "focus") for (const id of v.visAdj.get(v.focal) ?? []) forced.add(id);
    }
    const shown = chooseLabels({
      nodes: v.nodes,
      scale: v.scale,
      tx: v.tx,
      ty: v.ty,
      w: v.w,
      h: v.h,
      forced,
      // root/near nodes outrank distant ones; deterministic id tiebreak.
      priority: (id) => -(hops.get(id) ?? 9),
    });
    for (const [id, el] of nodeEls.current) {
      const on = shown.has(id);
      if (labelShown.current.get(id) !== on) {
        el.dataset.label = on ? "on" : "off";
        labelShown.current.set(id, on);
      }
    }
  }

  // ---- interactions ----
  function enterFocus(id: string) {
    vw.current.scale = 1;
    vw.current.tx = 0;
    vw.current.ty = 0;
    setMode("focus");
    setFocal(id);
    setTrail([id]);
  }
  function recenter(id: string) {
    setFocal(id);
    setTrail((t) => [...t, id]);
  }
  function exitFocus() {
    setMode("overview");
    setFocal(null);
    setTrail([]);
  }
  function onNodeTap(id: string) {
    if (mode === "overview") enterFocus(id);
    else if (id === focal) setSheetId(id);
    else recenter(id);
  }
  function jumpCrumb(i: number) {
    setTrail((t) => t.slice(0, i + 1));
    setFocal(trail[i] ?? null);
  }
  function toggleKind(k: EntityTypeKey) {
    setSel((prev) => {
      const next = new Set(prev);
      if (next.has(k)) next.delete(k);
      else next.add(k);
      return next;
    });
  }

  // pan / pinch / wheel / keyboard — one finger pans empty canvas, two fingers
  // pinch-zoom about the finger midpoint; node and control taps are excluded so
  // they never get captured (kills the pan-vs-tap conflict). All zoom is
  // focal-anchored (focalZoom): the point under the cursor/fingers stays put.
  const ptrs = useRef(new Map<number, { x: number; y: number }>());
  const gesture = useRef({ sd: 0, ss: 1, panX: 0, panY: 0, midX: 0, midY: 0 });

  // clientX/Y are page coords but tx/ty (and node positions) are stage-local —
  // subtract the stage's offset to anchor zoom in the right space.
  function stageOffset() {
    const r = stageRef.current?.getBoundingClientRect();
    return { ox: r?.left ?? 0, oy: r?.top ?? 0 };
  }

  // Keep at least a sliver of the laid-out graph on screen after pan/zoom.
  function clampPan() {
    const v = vw.current;
    const m = 64;
    let minx = Number.POSITIVE_INFINITY;
    let miny = Number.POSITIVE_INFINITY;
    let maxx = Number.NEGATIVE_INFINITY;
    let maxy = Number.NEGATIVE_INFINITY;
    for (const n of v.nodes.values()) {
      minx = Math.min(minx, n.x);
      miny = Math.min(miny, n.y);
      maxx = Math.max(maxx, n.x);
      maxy = Math.max(maxy, n.y);
    }
    if (!Number.isFinite(minx)) return;
    const s = v.scale;
    const loX = m - s * maxx;
    const hiX = v.w - m - s * minx;
    const loY = m - s * maxy;
    const hiY = v.h - m - s * miny;
    v.tx = loX > hiX ? (loX + hiX) / 2 : Math.min(hiX, Math.max(loX, v.tx));
    v.ty = loY > hiY ? (loY + hiY) / 2 : Math.min(hiY, Math.max(loY, v.ty));
  }

  function zoomAt(fx: number, fy: number, factor: number) {
    const next = focalZoom(vw.current, fx, fy, factor);
    vw.current.scale = next.scale;
    vw.current.tx = next.tx;
    vw.current.ty = next.ty;
    clampPan();
  }
  function zoom(f: number) {
    const stage = stageRef.current;
    if (stage) zoomAt(stage.clientWidth / 2, stage.clientHeight / 2, f);
  }
  function fit() {
    vw.current.scale = 1;
    vw.current.tx = 0;
    vw.current.ty = 0;
  }

  function onPointerDown(e: PointerEvent<HTMLDivElement>) {
    if ((e.target as HTMLElement).closest(".graph-node, .graph-overlay, .graph-zoom")) return;
    e.currentTarget.setPointerCapture?.(e.pointerId);
    ptrs.current.set(e.pointerId, { x: e.clientX, y: e.clientY });
    const g = gesture.current;
    const { ox, oy } = stageOffset();
    if (ptrs.current.size === 1) {
      g.panX = e.clientX - vw.current.tx;
      g.panY = e.clientY - vw.current.ty;
    } else if (ptrs.current.size === 2) {
      const [a, b] = [...ptrs.current.values()];
      if (!a || !b) return;
      g.sd = Math.hypot(a.x - b.x, a.y - b.y);
      g.ss = vw.current.scale;
      g.midX = (a.x + b.x) / 2 - ox;
      g.midY = (a.y + b.y) / 2 - oy;
    }
  }
  function onPointerMove(e: PointerEvent<HTMLDivElement>) {
    if (!ptrs.current.has(e.pointerId)) return;
    ptrs.current.set(e.pointerId, { x: e.clientX, y: e.clientY });
    const g = gesture.current;
    const v = vw.current;
    if (ptrs.current.size === 1) {
      v.tx = e.clientX - g.panX;
      v.ty = e.clientY - g.panY;
      clampPan();
    } else if (ptrs.current.size === 2 && g.sd > 0) {
      const [a, b] = [...ptrs.current.values()];
      if (!a || !b) return;
      const { ox, oy } = stageOffset();
      const d = Math.hypot(a.x - b.x, a.y - b.y);
      const mx = (a.x + b.x) / 2 - ox;
      const my = (a.y + b.y) / 2 - oy;
      const s1 = clampScale(g.ss * (d / g.sd));
      const k = s1 / v.scale;
      // zoom about the current midpoint, then translate by its movement (pan)
      v.tx = mx - (mx - v.tx) * k;
      v.ty = my - (my - v.ty) * k;
      v.scale = s1;
      v.tx += mx - g.midX;
      v.ty += my - g.midY;
      g.midX = mx;
      g.midY = my;
      clampPan();
    }
  }
  function onPointerUp(e: PointerEvent<HTMLDivElement>) {
    e.currentTarget.releasePointerCapture?.(e.pointerId);
    ptrs.current.delete(e.pointerId);
    const g = gesture.current;
    if (ptrs.current.size < 2) g.sd = 0; // clear stale pinch baseline on 2→1
    const p = [...ptrs.current.values()][0];
    if (p) {
      g.panX = p.x - vw.current.tx;
      g.panY = p.y - vw.current.ty;
    }
  }
  function onDoubleClick(e: MouseEvent<HTMLDivElement>) {
    if ((e.target as HTMLElement).closest(".graph-node, .graph-overlay, .graph-zoom")) return;
    const { ox, oy } = stageOffset();
    zoomAt(e.clientX - ox, e.clientY - oy, 1.8);
  }
  function onKeyDown(e: KeyboardEvent<HTMLDivElement>) {
    const v = vw.current;
    if (e.key === "+" || e.key === "=") zoom(1.25);
    else if (e.key === "-" || e.key === "_") zoom(0.8);
    else if (e.key === "0") fit();
    else if (e.key === "ArrowLeft") {
      v.tx += 40;
      clampPan();
    } else if (e.key === "ArrowRight") {
      v.tx -= 40;
      clampPan();
    } else if (e.key === "ArrowUp") {
      v.ty += 40;
      clampPan();
    } else if (e.key === "ArrowDown") {
      v.ty -= 40;
      clampPan();
    } else return;
    e.preventDefault();
  }

  // Desktop wheel / trackpad-pinch zoom, focal at the cursor. Bound natively so
  // it can preventDefault (React's onWheel can be passive).
  // biome-ignore lint/correctness/useExhaustiveDependencies: handler only reads refs (stable); rebinding per render is unnecessary.
  useEffect(() => {
    const stage = stageRef.current;
    if (!stage) return;
    const handler = (e: WheelEvent) => {
      if ((e.target as HTMLElement).closest(".graph-zoom, .graph-overlay")) return;
      e.preventDefault();
      const r = stage.getBoundingClientRect();
      zoomAt(e.clientX - r.left, e.clientY - r.top, Math.exp(-e.deltaY * 0.0015));
    };
    stage.addEventListener("wheel", handler, { passive: false });
    return () => stage.removeEventListener("wheel", handler);
  }, []);

  if (phase === "loading") return <main className="screen-body graph-state">loading graph…</main>;
  if (phase === "error")
    return (
      <main className="screen-body graph-state">
        couldn't load the graph — check the connection.
      </main>
    );
  if (phase === "empty" || !graph)
    return (
      <main className="screen-body graph-state">
        no entities yet — they appear as notes are analyzed.
      </main>
    );

  return (
    <main className="screen-body graph-screen">
      <div className="filter-bar" data-active={sel.size > 0} aria-label="Type filter">
        <button
          type="button"
          className={`fchip fchip-all${sel.size === 0 ? " fchip-on" : ""}`}
          aria-pressed={sel.size === 0}
          onClick={() => setSel(new Set())}
        >
          All
        </button>
        {kindsPresent.map(({ kind, count }) => (
          <button
            key={kind}
            type="button"
            className={`fchip fchip-type${sel.has(kind) ? " fchip-on" : ""}`}
            aria-pressed={sel.has(kind)}
            onClick={() => toggleKind(kind)}
          >
            {/* Empty box once a selection is active = "taps add a type" cue. */}
            {sel.size > 0 && <span className="fchip-ck" aria-hidden="true" />}
            <span className="fchip-swatch">
              <EntityTypeIcon kind={kind} size={16} />
            </span>
            {KIND_LABEL[kind]}
            <span className="fchip-count">{count}</span>
          </button>
        ))}
      </div>

      <div
        className="graph-stage"
        ref={stageRef}
        data-mode={mode}
        role="application"
        aria-label="Entity graph (zoom: +/- or scroll, fit: 0, pan: arrow keys)"
        // biome-ignore lint/a11y/noNoninteractiveTabindex: role=application canvas must be focusable for the keyboard zoom/pan handlers below.
        tabIndex={0}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onPointerCancel={onPointerUp}
        onDoubleClick={onDoubleClick}
        onKeyDown={onKeyDown}
      >
        <div className="graph-viewport" ref={viewportRef}>
          {/* biome-ignore lint/a11y/noSvgWithoutTitle: edges are decorative; the nodes carry the labels. */}
          <svg className="graph-edges">
            {graph.edges.map((e) => (
              <line
                key={edgeKey(e.source, e.target, e.predicate)}
                ref={(el) => {
                  const key = edgeKey(e.source, e.target, e.predicate);
                  if (el) edgeEls.current.set(key, el);
                  else edgeEls.current.delete(key);
                }}
              />
            ))}
          </svg>
          {graph.nodes.map((n) => {
            const focused = mode === "focus" && n.id === focal;
            const exemptShown = focused && isHidden(n.id);
            return (
              <button
                type="button"
                key={n.id}
                ref={(el) => {
                  if (el) nodeEls.current.set(n.id, el);
                  else nodeEls.current.delete(n.id);
                }}
                className={`graph-node${focused ? " is-focal" : ""}`}
                data-hop={hops.get(n.id) ?? 2}
                aria-label={n.canonical_name}
                onClick={() => onNodeTap(n.id)}
              >
                <EntityTypeIcon
                  kind={n.kind}
                  size={focused ? 64 : (hops.get(n.id) ?? 2) <= 1 ? 46 : 34}
                />
                {exemptShown && <span className="graph-exempt">shown · focus</span>}
                <span className="graph-node-label">{n.canonical_name}</span>
              </button>
            );
          })}
        </div>

        <span className="graph-lod" aria-hidden="true">
          {mode === "focus" ? "focus" : "overview"}
        </span>

        {mode === "focus" && (
          <div className="graph-overlay">
            <button type="button" className="graph-return" onClick={exitFocus}>
              <ChevronLeftIcon size={18} />
              Overview
            </button>
            <div className="graph-crumbs">
              {trail.map((id, i) => {
                const node = graph.nodes.find((n) => n.id === id);
                return (
                  <button
                    type="button"
                    key={id}
                    className={`graph-crumb${i === trail.length - 1 ? " is-current" : ""}`}
                    onClick={() => jumpCrumb(i)}
                  >
                    {node?.canonical_name ?? "—"}
                  </button>
                );
              })}
            </div>
          </div>
        )}

        {mode === "overview" && (
          <div className="graph-zoom">
            <button type="button" onClick={() => zoom(1.25)} aria-label="Zoom in">
              <PlusIcon size={20} />
            </button>
            <button type="button" onClick={fit} aria-label="Fit to view">
              <FitIcon size={18} />
            </button>
            <button type="button" onClick={() => zoom(0.8)} aria-label="Zoom out">
              <MinusIcon size={20} />
            </button>
          </div>
        )}

        {hiddenCount > 0 && (
          <div className="graph-hidden">
            <span>
              showing {selSummary(sel)} · {hiddenCount} hidden
            </span>
            <button type="button" onClick={() => setSel(new Set())}>
              All
            </button>
          </div>
        )}

        {mode === "focus" && focusEmpty && (
          <p className="graph-empty-note">no neighbours match these filters.</p>
        )}

        {mode === "overview" && graph.edges.length === 0 && (
          <p className="graph-empty-note">
            no connections yet — links appear as more notes are analyzed.
          </p>
        )}
      </div>

      <p className="graph-hint">
        {mode === "overview"
          ? graph.edges.length === 0
            ? "drag to pan · scroll or pinch to zoom"
            : "tap a node to focus · scroll or pinch to zoom · double-tap to zoom in"
          : "tap a neighbour to re-center · use Overview to return"}
      </p>

      {sheetId && (
        <EntityPeek
          entityId={sheetId}
          onClose={() => setSheetId(null)}
          onFocus={(id) => {
            setSheetId(null);
            if (mode === "overview") enterFocus(id);
            else recenter(id);
          }}
          onOpen={(id) => {
            setSheetId(null);
            onOpenEntity(id);
          }}
          onNeighbor={(id) => {
            setSheetId(null);
            // A neighbour already on the map re-centers in place; anything
            // further out opens its full page.
            if (nodeKind.has(id)) {
              if (mode === "overview") enterFocus(id);
              else recenter(id);
            } else {
              onOpenEntity(id);
            }
          }}
        />
      )}
    </main>
  );
}

// A peek at one entity's attributes without leaving the graph — fetched on
// open, with the two onward actions (focus here, open the full page).
function EntityPeek({
  entityId,
  onClose,
  onFocus,
  onOpen,
  onNeighbor,
}: {
  entityId: string;
  onClose: () => void;
  onFocus: (id: string) => void;
  onOpen: (id: string) => void;
  onNeighbor: (id: string) => void;
}) {
  const [entity, setEntity] = useState<EntityOut | null>(null);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    let stale = false;
    api
      .getEntity(entityId)
      .then((e) => !stale && setEntity(e))
      .catch(() => !stale && setFailed(true));
    return () => {
      stale = true;
    };
  }, [entityId]);

  const title = entity?.canonical_name ?? "Entity";
  const attrs = (entity?.predicates ?? [])
    .map((p) => p.current)
    .filter((f): f is NonNullable<typeof f> => f !== null && f.object_entity_id === null)
    .slice(0, 8);

  // Relationships = outbound object-edges + inbound edges, the in-sheet path
  // back into the graph.
  const rels: { id: string; name: string; predicate: string }[] = [];
  for (const p of entity?.predicates ?? []) {
    const c = p.current;
    if (c?.object_entity_id) {
      rels.push({
        id: c.object_entity_id,
        name: c.object_entity_name ?? "—",
        predicate: c.predicate,
      });
    }
  }
  for (const ib of entity?.inbound ?? []) {
    rels.push({ id: ib.entity_id, name: ib.name, predicate: ib.predicate });
  }

  return (
    <Sheet title={title} onClose={onClose}>
      <div className="peek-head">
        {entity && <EntityTypeIcon kind={entity.kind} size={40} />}
        <div className="peek-meta">
          <span className="peek-kind">{entity?.kind ?? "…"}</span>
          {entity && entity.domain !== "general" && (
            <span className="peek-domain" data-domain={entity.domain}>
              ● {entity.domain} · firewalled
            </span>
          )}
        </div>
      </div>
      <div className="peek-actions">
        <button type="button" className="peek-btn primary" onClick={() => onFocus(entityId)}>
          Focus here
        </button>
        <button type="button" className="peek-btn" onClick={() => onOpen(entityId)}>
          Open entity →
        </button>
      </div>
      {failed && <p className="graph-state">couldn't load details.</p>}
      {attrs.length > 0 && (
        <ul className="peek-attrs">
          {attrs.map((f) => (
            <li key={f.id} className="peek-attr">
              <span className="peek-attr-pred">{f.predicate}</span>
              <span className="peek-attr-val">{f.statement}</span>
            </li>
          ))}
        </ul>
      )}
      {rels.length > 0 && (
        <div className="peek-rels">
          <h3 className="peek-rels-head">relationships ({rels.length})</h3>
          <ul className="peek-rels-list">
            {rels.slice(0, 12).map((r, i) => (
              <li key={`${r.id}-${r.predicate}-${i}`}>
                <button type="button" className="peek-rel" onClick={() => onNeighbor(r.id)}>
                  <span className="peek-rel-pred">{r.predicate}</span>
                  <span className="peek-rel-name">{r.name} →</span>
                </button>
              </li>
            ))}
          </ul>
        </div>
      )}
    </Sheet>
  );
}
