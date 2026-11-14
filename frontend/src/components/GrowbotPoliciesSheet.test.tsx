import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { GrowbotPolicy } from "../api/client";
import {
  GrowbotPoliciesSheet,
  keptText,
  offered,
  offeredFor,
  walkKind,
} from "./GrowbotPoliciesSheet";

// The Trained walks sheet: the box's library, and the finished training jobs it adds from.

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function policy(over: Partial<GrowbotPolicy> = {}): GrowbotPolicy {
  return {
    id: "8d1c2f4e-0a3b-4c5d-9e6f-7a8b9c0d1e2f",
    name: "Quick trot",
    source_job: "j-1",
    source_spec: "growbot_heading_walk",
    bytes: 40960,
    created_at: "2026-11-13T10:00:00Z",
    contract: { facing: "nose", speed: false, pitch: false, control_hz: 25 },
    ...over,
  };
}

const CAMERA_JOB = {
  job_id: "j-2",
  spec: "growbot_camera_walk",
  title: "Camera walk",
  finished_at: "2026-11-14T08:00:00Z",
  added: false,
};

type Call = { url: string; method: string; body: unknown };

function serve(route: (c: Call) => Response) {
  const calls: Call[] = [];
  vi.stubGlobal("fetch", async (input: RequestInfo | URL, init?: RequestInit) => {
    const c = {
      url: String(input),
      method: init?.method ?? "GET",
      body: init?.body ? JSON.parse(String(init.body)) : null,
    };
    calls.push(c);
    return route(c);
  });
  return calls;
}

// The sheet hands its parent updates; this keeps the list they build.
function holder(start: GrowbotPolicy[]) {
  const box = { all: start };
  const onChange = vi.fn((update: (all: GrowbotPolicy[]) => GrowbotPolicy[]) => {
    box.all = update(box.all);
  });
  return { box, onChange };
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("GrowbotPoliciesSheet", () => {
  it("adds a finished job's walk, named for its kind and day", async () => {
    const made = policy({
      id: "1d1c2f4e-0a3b-4c5d-9e6f-7a8b9c0d1e2f",
      name: "Camera walk, Nov 14",
      contract: { facing: "camera", speed: true, pitch: true, control_hz: 25 },
    });
    const calls = serve((c) => (c.method === "POST" ? json(made, 201) : json([CAMERA_JOB])));
    const { box, onChange } = holder([policy()]);
    render(<GrowbotPoliciesSheet policies={[policy()]} onChange={onChange} onClose={vi.fn()} />);
    fireEvent.click(
      await screen.findByRole("button", {
        name: "Add the walk from Camera walk",
      }),
    );
    await screen.findByText(/Added Camera walk, Nov 14/);
    const post = calls.find((c) => c.method === "POST");
    expect(post).toMatchObject({
      url: "/api/growbot/policies",
      body: { job_id: "j-2" },
    });
    expect(String((post?.body as { name: string }).name)).toMatch(/^Camera walk/);
    expect(box.all).toEqual([made, policy()]);
    expect(screen.getByText("On the box", { selector: ".gb-tag" })).toBeInTheDocument();
  });

  it("says where walks come from when no job has finished", async () => {
    serve(() => json([]));
    render(<GrowbotPoliciesSheet policies={[]} onChange={vi.fn()} onClose={vi.fn()} />);
    await screen.findByText(/No finished GrowBot training yet/);
    expect(screen.getByText(/None yet/)).toBeInTheDocument();
  });

  it("renames on Enter and deletes on a second tap", async () => {
    const renamed = policy({ name: "Zoomies" });
    const calls = serve((c) =>
      c.method === "PATCH"
        ? json(renamed)
        : c.method === "DELETE"
          ? new Response(null, { status: 204 })
          : json([]),
    );
    const { box, onChange } = holder([policy()]);
    render(<GrowbotPoliciesSheet policies={[policy()]} onChange={onChange} onClose={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "Rename Quick trot" }));
    const field = screen.getByRole("textbox", {
      name: "New name for Quick trot",
    });
    fireEvent.change(field, { target: { value: "Zoomies" } });
    fireEvent.keyDown(field, { key: "Enter" });
    await waitFor(() => expect(box.all).toEqual([renamed]));
    expect(calls.find((c) => c.method === "PATCH")?.body).toEqual({
      name: "Zoomies",
    });

    fireEvent.click(screen.getByRole("button", { name: "Delete Quick trot" }));
    expect(calls.some((c) => c.method === "DELETE")).toBe(false);
    fireEvent.click(screen.getByRole("button", { name: "Tap again to delete Quick trot" }));
    await waitFor(() => expect(box.all).toEqual([]));
    await screen.findByText("Deleted Quick trot.");
  });

  it("shows the box's refusal", async () => {
    serve((c) => (c.method === "POST" ? json({ detail: "bad policy" }, 422) : json([CAMERA_JOB])));
    render(<GrowbotPoliciesSheet policies={[]} onChange={vi.fn()} onClose={vi.fn()} />);
    fireEvent.click(
      await screen.findByRole("button", {
        name: "Add the walk from Camera walk",
      }),
    );
    await screen.findByText(/bad policy/);
  });
});

describe("GrowbotPoliciesSheet's switches", () => {
  it("turns a policy off, sets its motion and picks it for its family", async () => {
    const older = policy({
      id: "2d1c2f4e-0a3b-4c5d-9e6f-7a8b9c0d1e2f",
      name: "Old trot",
    });
    const calls = serve((c) => {
      if (c.method !== "PATCH") return json([]);
      const id = c.url.split("/").pop();
      const base = id === older.id ? older : policy();
      return json({ ...base, ...(c.body as object) });
    });
    const { box, onChange } = holder([policy(), older]);
    const view = render(
      <GrowbotPoliciesSheet policies={box.all} onChange={onChange} onClose={vi.fn()} />,
    );
    // The newest of the family is on the pad, so only the older one offers itself.
    expect(screen.queryByRole("button", { name: "Use Quick trot for its family" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Use Old trot for its family" }));
    await waitFor(() => expect(box.all.find((p) => p.id === older.id)?.active).toBe(true));
    view.rerender(
      <GrowbotPoliciesSheet policies={box.all} onChange={onChange} onClose={vi.fn()} />,
    );
    expect(
      screen.getByRole("button", { name: "Use Quick trot for its family" }),
    ).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Offer Quick trot on the pad" }));
    await waitFor(() => expect(box.all.find((p) => p.id === policy().id)?.enabled).toBe(false));
    fireEvent.click(screen.getByRole("button", { name: "Quick trot is a trick" }));
    await waitFor(() => expect(box.all.find((p) => p.id === policy().id)?.motion).toBe("trick"));
    // Tapping the motion it already has sends nothing.
    fireEvent.click(screen.getByRole("button", { name: "Old trot is a walk" }));
    expect(calls.filter((c) => c.method === "PATCH").map((c) => c.body)).toEqual([
      { active: true },
      { enabled: false },
      { motion: "trick" },
    ]);
  });
});

describe("offered", () => {
  it("keeps the enabled ones of a motion, one per family, the picked one over the newest", () => {
    const a = policy({ id: "a", created_at: "2026-11-13T10:00:00Z" });
    const b = policy({ id: "b", created_at: "2026-11-14T10:00:00Z" });
    const c = policy({ id: "c", source_spec: "spin", motion: "turn" });
    const d = policy({ id: "d", source_spec: "look", enabled: false });
    expect(offered([a, b, c, d], "walk").map((p) => p.id)).toEqual(["b"]);
    expect(offered([a, { ...b, enabled: false }], "walk").map((p) => p.id)).toEqual(["a"]);
    expect(offered([{ ...a, active: true }, b], "walk").map((p) => p.id)).toEqual(["a"]);
    expect(offered([a, b, c, d], "turn").map((p) => p.id)).toEqual(["c"]);
    expect(offered([a, b, c, d], "trick")).toEqual([]);
  });
});

describe("offeredFor", () => {
  it("follows a pick to what its family now offers", () => {
    const a = policy({ id: "a", active: true });
    const b = policy({ id: "b", created_at: "2026-11-14T10:00:00Z" });
    const t = policy({ id: "t", motion: "turn", active: true });
    expect(offeredFor([a, b, t], offered([a, b, t], "walk"), "a")?.id).toBe("a");
    // Picking b, or switching a off, moves the pad's pick to b.
    const now = [{ ...a, active: false }, { ...b, active: true }, t];
    expect(offeredFor(now, offered(now, "walk"), "a")?.id).toBe("b");
    const off = [{ ...a, enabled: false }, b, t];
    expect(offeredFor(off, offered(off, "walk"), "a")?.id).toBe("b");
    // A walk pick that became a turn falls to its family's walk; a lost id is no pick.
    expect(offeredFor([a, b, t], offered([a, b, t], "walk"), "t")?.id).toBe("a");
    expect(offeredFor([a, b, t], offered([a, b, t], "walk"), "gone")).toBeNull();
  });
});

describe("GrowbotPoliciesSheet after a delete", () => {
  it("offers the walk's job again", async () => {
    serve((c) =>
      c.method === "DELETE"
        ? new Response(null, { status: 204 })
        : json([{ ...CAMERA_JOB, job_id: "j-1", added: true }]),
    );
    const { onChange } = holder([policy()]);
    render(<GrowbotPoliciesSheet policies={[policy()]} onChange={onChange} onClose={vi.fn()} />);
    await screen.findByText("On the box", { selector: ".gb-tag" });
    fireEvent.click(screen.getByRole("button", { name: "Delete Quick trot" }));
    fireEvent.click(screen.getByRole("button", { name: "Tap again to delete Quick trot" }));
    await screen.findByRole("button", {
      name: "Add the walk from Camera walk",
    });
  });
});

describe("walkKind", () => {
  it("names the lead end and whether it takes speed", () => {
    expect(walkKind(policy())).toBe("nose first, heading only");
    expect(
      walkKind(
        policy({
          contract: {
            facing: "camera",
            speed: true,
            pitch: true,
            control_hz: 25,
          },
        }),
      ),
    ).toBe("camera end first, with speed");
  });
  it("pulls the walks kept in the training repo and says what changed", async () => {
    const kept = policy({
      id: "2d1c2f4e-0a3b-4c5d-9e6f-7a8b9c0d1e2f",
      name: "Look v1",
      source_job: `repo:look-v1@${"a".repeat(40)}`,
      source_spec: "growbot_look",
    });
    const sync = {
      commit: "1234567890abcdef1234567890abcdef12345678",
      added: ["Look v1"],
      updated: [],
      removed: [],
      skipped: [],
    };
    const calls = serve((c) =>
      c.url.endsWith("/refresh")
        ? json(sync)
        : c.url.endsWith("/sources")
          ? json([])
          : json([kept, policy()]),
    );
    const { box, onChange } = holder([policy()]);
    render(<GrowbotPoliciesSheet policies={[policy()]} onChange={onChange} onClose={vi.fn()} />);
    fireEvent.click(
      await screen.findByRole("button", { name: "Refresh walks from the training repo" }),
    );
    await screen.findByText("Added Look v1 (training repo at 1234567).");
    expect(calls.some((c) => c.method === "POST" && c.url.endsWith("/refresh"))).toBe(true);
    expect(box.all).toEqual([kept, policy()]);
  });

  it("says why a refresh from the training repo failed", async () => {
    serve((c) =>
      c.url.endsWith("/refresh")
        ? json({ detail: "policies/index.toml policy 1: slug must be" }, 422)
        : json([]),
    );
    render(<GrowbotPoliciesSheet policies={[]} onChange={vi.fn()} onClose={vi.fn()} />);
    fireEvent.click(
      await screen.findByRole("button", { name: "Refresh walks from the training repo" }),
    );
    await screen.findByText(/slug must be/);
  });

  it("words every kind of refresh outcome", () => {
    const base = { commit: "", added: [], updated: [], removed: [], skipped: [] };
    expect(keptText(base)).toBe("Up to date with the training repo.");
    expect(
      keptText({
        ...base,
        commit: "f".repeat(40),
        updated: ["A"],
        removed: ["B"],
        skipped: ["C: not a policy"],
      }),
    ).toBe("Updated A. Removed B. Couldn't take C: not a policy (training repo at fffffff).");
  });
});
