import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { type EgoGraph, type EntityOut, type FactOut, api } from "../api/client";
import {
  GraphScreen,
  chooseLabels,
  clampScale,
  edgeLabelText,
  focalZoom,
  planEdges,
} from "./GraphScreen";

const GRAPH: EgoGraph = {
  root: "me",
  depth: 2,
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
  ],
  edges: [
    { source: "me", target: "wife", predicate: "spouse" },
    { source: "me", target: "acme", predicate: "worksFor" },
    { source: "me", target: "pdx", predicate: "residence" },
  ],
};

function fact(over: Partial<FactOut> = {}): FactOut {
  return {
    id: "f1",
    entity_id: "me",
    entity_name: "Me",
    predicate: "occupation",
    qualifier: null,
    kind: "attribute",
    statement: "Software engineer",
    value_json: null,
    assertion: "asserted",
    status: "active",
    pinned: false,
    confidence: 0.9,
    valid_from: null,
    valid_to: null,
    reported_at: "2026-06-10T09:00:00Z",
    temporal_precision: "day",
    object_entity_id: null,
    object_entity_name: null,
    source_snippet: null,
    ...over,
  };
}

const ME_DETAIL: EntityOut = {
  id: "me",
  kind: "Person",
  canonical_name: "Me",
  status: "confirmed",
  aliases: [],
  domain: "general",
  predicates: [
    { predicate: "occupation", qualifier: null, current: fact(), history: [fact()] },
    {
      predicate: "owns",
      qualifier: null,
      current: fact({
        id: "f2",
        predicate: "owns",
        statement: "owns the F-150",
        object_entity_id: "truck",
        object_entity_name: "F-150",
      }),
      history: [],
    },
  ],
  inbound: [{ entity_id: "wife", name: "Wife", predicate: "spouse", statement: "married to" }],
  mentions: [],
};

function setup() {
  const load = vi.fn(async () => GRAPH);
  const onOpenEntity = vi.fn();
  render(<GraphScreen onOpenEntity={onOpenEntity} rootId="me" load={load} />);
  return { load, onOpenEntity };
}

async function loaded() {
  await waitFor(() => expect(screen.queryByText("loading graph…")).not.toBeInTheDocument());
}

describe("GraphScreen", () => {
  it("loads the root ego graph and renders nodes + type chips", async () => {
    const { load } = setup();
    await loaded();
    expect(load).toHaveBeenCalledWith("me", 2);
    for (const name of ["Me", "Wife", "Acme", "Portland"]) {
      expect(screen.getByText(name)).toBeInTheDocument();
    }
    const bar = screen.getByLabelText("Type filter");
    // Chips derive from the data: People (2), Orgs (1), Places (1), plus All.
    expect(within(bar).getByRole("button", { name: /People/ })).toBeInTheDocument();
    expect(within(bar).getByRole("button", { name: /Orgs/ })).toBeInTheDocument();
    expect(within(bar).getByRole("button", { name: "All" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
  });

  it("tap a type chip shows only that type; All resets (additive model, empty = All)", async () => {
    setup();
    await loaded();
    const bar = screen.getByLabelText("Type filter");
    fireEvent.click(within(bar).getByRole("button", { name: /Orgs/ }));
    expect(within(bar).getByRole("button", { name: /Orgs/ })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    // 3 of 4 nodes (People×2, Place) are hidden; the focal-less overview pill says so.
    expect(screen.getByText(/showing only orgs · 3 hidden/)).toBeInTheDocument();
    // Adding a second type is additive, not radio.
    fireEvent.click(within(bar).getByRole("button", { name: /People/ }));
    expect(screen.getByText(/showing 2 types · 1 hidden/)).toBeInTheDocument();
    fireEvent.click(within(bar).getByRole("button", { name: "All" }));
    expect(screen.queryByText(/hidden/)).not.toBeInTheDocument();
  });

  it("tap enters radial focus; Overview returns (no hidden gesture)", async () => {
    setup();
    await loaded();
    expect(screen.queryByRole("button", { name: "Overview" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByText("Wife").closest("button") as HTMLElement);
    const overview = await screen.findByRole("button", { name: "Overview" });
    // Breadcrumb reflects the focal entity (the crumb, not the node label).
    expect(screen.getByText("Wife", { selector: ".graph-crumb" })).toBeInTheDocument();
    fireEvent.click(overview);
    await waitFor(() =>
      expect(screen.queryByRole("button", { name: "Overview" })).not.toBeInTheDocument(),
    );
  });

  it("tapping the focal node opens the peek sheet; Open entity navigates", async () => {
    const getEntity = vi.spyOn(api, "getEntity").mockResolvedValue(ME_DETAIL);
    const { onOpenEntity } = setup();
    await loaded();
    const meNode = () =>
      screen
        .getAllByText("Me")
        .map((l) => l.closest("button"))
        .find((b) => b?.classList.contains("graph-node")) as HTMLElement;
    fireEvent.click(meNode()); // overview -> focus on Me
    await screen.findByRole("button", { name: "Overview" });
    fireEvent.click(meNode()); // focal tap -> peek sheet
    expect(await screen.findByRole("button", { name: "Open entity →" })).toBeInTheDocument();
    await waitFor(() => expect(screen.getByText("Software engineer")).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: "Open entity →" }));
    expect(onOpenEntity).toHaveBeenCalledWith("me");
    getEntity.mockRestore();
  });

  it("shows the calm empty state when the root has no graph", async () => {
    const load = vi.fn(async () => ({ root: "me", depth: 2, nodes: [], edges: [] }));
    render(<GraphScreen onOpenEntity={vi.fn()} rootId="me" load={load} />);
    await waitFor(() =>
      expect(
        screen.getByText("no entities yet — they appear as notes are analyzed."),
      ).toBeInTheDocument(),
    );
  });

  it("shows the quiet error state when the load fails", async () => {
    const load = vi.fn(async () => {
      throw new Error("down");
    });
    render(<GraphScreen onOpenEntity={vi.fn()} rootId="me" load={load} />);
    await waitFor(() =>
      expect(
        screen.getByText("couldn't load the graph — check the connection."),
      ).toBeInTheDocument(),
    );
  });

  it("explains a single-node graph instead of leaving a lone disc", async () => {
    const [me] = GRAPH.nodes;
    const lone: EgoGraph = { root: "me", depth: 2, nodes: me ? [me] : [], edges: [] };
    const load = vi.fn(async () => lone);
    render(<GraphScreen onOpenEntity={vi.fn()} rootId="me" load={load} />);
    await waitFor(() => expect(screen.getByText("Me")).toBeInTheDocument());
    expect(
      screen.getByText("no connections yet — links appear as more notes are analyzed."),
    ).toBeInTheDocument();
  });

  it("peek sheet lists relationships; tapping one off the map opens it", async () => {
    vi.spyOn(api, "getEntity").mockResolvedValue(ME_DETAIL);
    const { onOpenEntity } = setup();
    await loaded();
    const meNode = () =>
      screen
        .getAllByText("Me")
        .map((l) => l.closest("button"))
        .find((b) => b?.classList.contains("graph-node")) as HTMLElement;
    fireEvent.click(meNode());
    await screen.findByRole("button", { name: "Overview" });
    fireEvent.click(meNode());
    expect(await screen.findByText("relationships (2)")).toBeInTheDocument();
    // The F-150 isn't a node on the loaded map → it opens the full entity page.
    fireEvent.click(screen.getByText("F-150 →").closest("button") as HTMLElement);
    expect(onOpenEntity).toHaveBeenCalledWith("truck");
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
    // world point under the anchor maps back to the same screen pixel
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
    expect(fwd?.label).toBe("owns · owned by"); // both predicates, arrow both ends
    expect(bwd?.skip).toBe(true); // drawn once, by its partner
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
