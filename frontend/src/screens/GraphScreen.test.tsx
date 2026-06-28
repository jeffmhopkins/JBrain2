import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { EgoGraph } from "../api/client";
import {
  GraphScreen,
  chooseLabels,
  clampScale,
  edgeLabelText,
  focalZoom,
  planEdges,
  settleLocal,
} from "./GraphScreen";

// A small whole-graph fixture with a genuine 2-hop reach from "me":
// 1-hop = Wife, Acme, Portland; 2-hop = Headquarters (via Acme), Sister (via Wife).
const GRAPH: EgoGraph = {
  root: "me",
  depth: 0,
  nodes: [
    { id: "me", kind: "Person", canonical_name: "Me", status: "confirmed", domain: "general" },
    { id: "wife", kind: "Person", canonical_name: "Wife", status: "confirmed", domain: "general" },
    {
      id: "acme",
      kind: "Organization",
      canonical_name: "Acme",
      status: "confirmed",
      domain: "general",
    },
    {
      id: "pdx",
      kind: "Place",
      canonical_name: "Portland",
      status: "confirmed",
      domain: "location",
    },
    {
      id: "hq",
      kind: "Place",
      canonical_name: "Headquarters",
      status: "confirmed",
      domain: "general",
    },
    {
      id: "sis",
      kind: "Person",
      canonical_name: "Sister",
      status: "confirmed",
      domain: "general",
    },
  ],
  edges: [
    { source: "me", target: "wife", predicate: "spouse" },
    { source: "me", target: "acme", predicate: "worksFor" },
    { source: "me", target: "pdx", predicate: "residence" },
    { source: "acme", target: "hq", predicate: "locatedAt" },
    { source: "wife", target: "sis", predicate: "sibling" },
  ],
};

function setup() {
  const loadFull = vi.fn(async () => GRAPH);
  const load = vi.fn(async () => GRAPH);
  const onOpenEntity = vi.fn();
  render(<GraphScreen onOpenEntity={onOpenEntity} load={load} loadFull={loadFull} />);
  return { loadFull, load, onOpenEntity };
}

async function loaded() {
  await waitFor(() => expect(screen.queryByText("loading graph…")).not.toBeInTheDocument());
}

const graphNode = (name: string) =>
  screen
    .getAllByRole("button", { name })
    .find((b) => b.classList.contains("graph-node")) as HTMLElement;
const panel = () => screen.getByRole("region", { name: "Entity detail" });

describe("GraphScreen", () => {
  it("opens on the whole-graph root (loadFull) and shows its 2-hop neighbourhood", async () => {
    const { loadFull, load } = setup();
    await loaded();
    expect(loadFull).toHaveBeenCalled();
    expect(load).not.toHaveBeenCalled();
    // focal "Me" plus 1-hop and 2-hop nodes are laid out.
    for (const name of ["Me", "Wife", "Acme", "Portland", "Headquarters", "Sister"]) {
      expect(graphNode(name)).toBeTruthy();
    }
    // panel reflects the focal entity and lists its direct relationships.
    expect(within(panel()).getByRole("heading", { name: "Me" })).toBeInTheDocument();
    expect(within(panel()).getByText("3 relationships")).toBeInTheDocument();
  });

  it("centres on an explicit root via load(rootId, 2)", async () => {
    const load = vi.fn(async () => GRAPH);
    render(<GraphScreen onOpenEntity={vi.fn()} rootId="me" load={load} />);
    await loaded();
    expect(load).toHaveBeenCalledWith("me", 2);
  });

  it("draws every direct connection of the focal — no cap, even for a hub", async () => {
    // 12 direct neighbours, well past the old cap of 8.
    const hub: EgoGraph = {
      root: "me",
      depth: 0,
      nodes: [
        { id: "me", kind: "Person", canonical_name: "Me", status: "confirmed", domain: "general" },
        ...Array.from({ length: 12 }, (_, i) => ({
          id: `n${i}`,
          kind: "Person" as const,
          canonical_name: `Friend ${i}`,
          status: "confirmed" as const,
          domain: "general" as const,
        })),
      ],
      edges: Array.from({ length: 12 }, (_, i) => ({
        source: "me",
        target: `n${i}`,
        predicate: "friend",
      })),
    };
    render(<GraphScreen onOpenEntity={vi.fn()} loadFull={async () => hub} />);
    await loaded();
    for (let i = 0; i < 12; i++) {
      expect(graphNode(`Friend ${i}`)).toBeTruthy();
    }
  });

  it("labels every connection, not just the focal's own edges", async () => {
    setup();
    await loaded();
    // the acme→hq edge ("locatedAt") touches neither the focal nor its panel,
    // yet its predicate still renders — connections are styled uniformly.
    await waitFor(() => expect(screen.getByText("located at")).toBeInTheDocument());
  });

  it("a type filter chip drops that whole type from the map", async () => {
    setup();
    await loaded();
    expect(graphNode("Portland")).toBeTruthy(); // a Place (1-hop)
    expect(graphNode("Headquarters")).toBeTruthy(); // a Place (2-hop)
    fireEvent.click(screen.getByRole("button", { name: /Place/ }));
    await waitFor(() =>
      expect(
        screen
          .queryAllByRole("button", { name: "Portland" })
          .find((b) => b.classList.contains("graph-node")),
      ).toBeUndefined(),
    );
    expect(
      screen
        .queryAllByRole("button", { name: "Headquarters" })
        .find((b) => b.classList.contains("graph-node")),
    ).toBeUndefined();
    // non-Place neighbours stay, and the panel still lists every relationship.
    expect(graphNode("Wife")).toBeTruthy();
    expect(within(panel()).getByText("3 relationships")).toBeInTheDocument();
  });

  it("tapping a neighbour node re-centres and pushes a breadcrumb", async () => {
    setup();
    await loaded();
    fireEvent.click(graphNode("Acme"));
    // panel now describes Acme and its links (Me + Headquarters).
    await waitFor(() =>
      expect(within(panel()).getByRole("heading", { name: "Acme" })).toBeInTheDocument(),
    );
    const crumbs = screen.getByLabelText("Breadcrumb");
    expect(within(crumbs).getByText("Me")).toBeInTheDocument();
    expect(within(crumbs).getByText("Acme")).toBeInTheDocument();
    // a crumb walks back to Me.
    fireEvent.click(within(crumbs).getByText("Me"));
    await waitFor(() =>
      expect(within(panel()).getByRole("heading", { name: "Me" })).toBeInTheDocument(),
    );
  });

  it("a relationship row in the panel re-centres on that entity", async () => {
    setup();
    await loaded();
    fireEvent.click(within(panel()).getByRole("button", { name: /Portland/ }));
    await waitFor(() =>
      expect(within(panel()).getByRole("heading", { name: "Portland" })).toBeInTheDocument(),
    );
  });

  it("Open entity navigates to the focal's full page", async () => {
    const { onOpenEntity } = setup();
    await loaded();
    fireEvent.click(screen.getByRole("button", { name: "Open entity →" }));
    expect(onOpenEntity).toHaveBeenCalledWith("me");
  });

  it("search jumps the focal to the chosen entity", async () => {
    setup();
    await loaded();
    const input = screen.getByLabelText("Search entities");
    fireEvent.change(input, { target: { value: "Sister" } });
    const results = screen.getByRole("list", { name: "Search results" });
    fireEvent.click(within(results).getByText("Sister"));
    await waitFor(() =>
      expect(within(panel()).getByRole("heading", { name: "Sister" })).toBeInTheDocument(),
    );
  });

  it("shows the calm empty state when there are no entities", async () => {
    const loadFull = vi.fn(async () => ({ root: "", depth: 0, nodes: [], edges: [] }));
    render(<GraphScreen onOpenEntity={vi.fn()} loadFull={loadFull} />);
    await waitFor(() =>
      expect(
        screen.getByText("no entities yet — they appear as notes are analyzed."),
      ).toBeInTheDocument(),
    );
  });

  it("shows the quiet error state when the load fails", async () => {
    const loadFull = vi.fn(async () => {
      throw new Error("down");
    });
    render(<GraphScreen onOpenEntity={vi.fn()} loadFull={loadFull} />);
    await waitFor(() =>
      expect(
        screen.getByText("couldn't load the graph — check the connection."),
      ).toBeInTheDocument(),
    );
  });

  it("explains a connectionless focal instead of leaving a lone disc", async () => {
    const lone: EgoGraph = {
      root: "me",
      depth: 0,
      nodes: [
        { id: "me", kind: "Person", canonical_name: "Me", status: "confirmed", domain: "general" },
      ],
      edges: [],
    };
    render(<GraphScreen onOpenEntity={vi.fn()} loadFull={async () => lone} />);
    await waitFor(() => expect(graphNode("Me")).toBeTruthy());
    expect(
      screen.getByText("no connections yet — links appear as more notes are analyzed."),
    ).toBeInTheDocument();
  });
});

describe("settleLocal", () => {
  const hop = new Map([
    ["me", 0],
    ["a", 1],
    ["b", 1],
    ["c", 2],
  ]);
  const links = [
    { source: "me", target: "a" },
    { source: "me", target: "b" },
    { source: "a", target: "c" },
  ];

  it("pins the focal at the origin and places every node", () => {
    const pos = settleLocal(["me", "a", "b", "c"], links, hop, "me");
    expect(pos.get("me")).toMatchObject({ x: 0, y: 0, hop: 0 });
    expect(pos.size).toBe(4);
    for (const id of ["a", "b", "c"]) {
      const p = pos.get(id);
      expect(p).toBeDefined();
      expect(Number.isFinite(p?.x) && Number.isFinite(p?.y)).toBe(true);
    }
  });

  it("is deterministic for a given focal and set (stable re-paints)", () => {
    const a = settleLocal(["me", "a", "b", "c"], links, hop, "me");
    const b = settleLocal(["me", "a", "b", "c"], links, hop, "me");
    for (const id of ["a", "b", "c"]) {
      expect(a.get(id)?.x).toBe(b.get(id)?.x);
      expect(a.get(id)?.y).toBe(b.get(id)?.y);
    }
  });

  it("separates neighbours so discs don't overlap", () => {
    const pos = settleLocal(["me", "a", "b", "c"], links, hop, "me");
    const pa = pos.get("a");
    const pb = pos.get("b");
    const d = Math.hypot((pa?.x ?? 0) - (pb?.x ?? 0), (pa?.y ?? 0) - (pb?.y ?? 0));
    expect(d).toBeGreaterThan(30);
  });
});

describe("focalZoom", () => {
  it("clamps scale to the [0.45, 2.4] range", () => {
    expect(clampScale(10)).toBe(2.4);
    expect(clampScale(0.01)).toBe(0.45);
    expect(clampScale(1)).toBe(1);
  });

  it("keeps the anchor point fixed across the scale change", () => {
    const v = { scale: 1, tx: 50, ty: 20 };
    const z = focalZoom(v, 100, 60, 2);
    expect(z.scale).toBe(2);
    const worldX = (100 - v.tx) / v.scale;
    const worldY = (60 - v.ty) / v.scale;
    expect(worldX * z.scale + z.tx).toBeCloseTo(100);
    expect(worldY * z.scale + z.ty).toBeCloseTo(60);
  });

  it("anchors zoom-out at the cursor too", () => {
    const v = { scale: 2, tx: -30, ty: 10 };
    const z = focalZoom(v, 200, 140, 0.5);
    expect(z.scale).toBe(1);
    expect(((200 - v.tx) / v.scale) * z.scale + z.tx).toBeCloseTo(200);
    expect(((140 - v.ty) / v.scale) * z.scale + z.ty).toBeCloseTo(140);
  });
});

describe("chooseLabels", () => {
  const node = (x: number, y: number, op = 1) => ({ x, y, op });
  const base = { scale: 1, tx: 0, ty: 0, w: 360, h: 560, priority: () => 0 };

  it("shows every label when nodes occupy different cells (room available)", () => {
    const nodes = new Map([
      ["a", node(20, 20)],
      ["b", node(300, 500)],
    ]);
    expect(chooseLabels({ ...base, nodes, forced: new Set() })).toEqual(new Set(["a", "b"]));
  });

  it("keeps only the higher-priority label when two share a cell", () => {
    const nodes = new Map([
      ["a", node(20, 20)],
      ["b", node(30, 30)],
    ]);
    const shown = chooseLabels({
      ...base,
      nodes,
      forced: new Set(),
      priority: (id) => (id === "a" ? 1 : 0),
    });
    expect(shown.has("a")).toBe(true);
    expect(shown.has("b")).toBe(false);
  });

  it("always shows forced labels — off-screen or below the legibility floor", () => {
    const nodes = new Map([["a", node(-999, -999)]]);
    const shown = chooseLabels({ ...base, scale: 0.1, nodes, forced: new Set(["a"]) });
    expect(shown.has("a")).toBe(true);
  });

  it("drops non-forced labels when text would be too small to read", () => {
    const nodes = new Map([["a", node(100, 100)]]);
    expect(chooseLabels({ ...base, scale: 0.3, nodes, forced: new Set() }).size).toBe(0);
  });
});

describe("edgeLabelText", () => {
  it("humanizes camelCase and snake_case predicates", () => {
    expect(edgeLabelText("worksFor")).toBe("works for");
    expect(edgeLabelText("seen_at")).toBe("seen at");
    expect(edgeLabelText("spouse")).toBe("spouse");
  });
});

describe("planEdges", () => {
  it("folds a reciprocal pair into one bidirectional link", () => {
    const plan = planEdges([
      { source: "a", target: "b", predicate: "owns" },
      { source: "b", target: "a", predicate: "ownedBy" },
    ]);
    const fwd = plan.get("a|b|owns");
    const bwd = plan.get("b|a|ownedBy");
    expect(fwd).toMatchObject({ arrowStart: true, arrowEnd: true, skip: false, idx: 0 });
    expect(fwd?.label).toBe("owns · owned by");
    expect(bwd?.skip).toBe(true);
  });

  it("shows a symmetric reciprocal as a single predicate", () => {
    const plan = planEdges([
      { source: "a", target: "b", predicate: "spouse" },
      { source: "b", target: "a", predicate: "spouse" },
    ]);
    expect(plan.get("a|b|spouse")?.label).toBe("spouse");
  });

  it("keeps a lone edge directional (arrow at the target)", () => {
    const plan = planEdges([{ source: "a", target: "b", predicate: "child" }]);
    expect(plan.get("a|b|child")).toMatchObject({
      arrowStart: false,
      arrowEnd: true,
      skip: false,
      label: "child",
    });
  });

  it("fans distinct same-direction edges and points each at its target", () => {
    const plan = planEdges([
      { source: "a", target: "b", predicate: "worksFor" },
      { source: "a", target: "b", predicate: "founded" },
    ]);
    const a = plan.get("a|b|worksFor");
    const b = plan.get("a|b|founded");
    expect(a?.arrowEnd && b?.arrowEnd).toBe(true);
    expect(a?.skip || b?.skip).toBe(false);
    expect(a?.idx).toBeCloseTo(-0.5);
    expect(b?.idx).toBeCloseTo(0.5);
  });
});
