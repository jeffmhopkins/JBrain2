import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ImageSettings, LlmSettings, LocalModelInfo } from "../api/client";
import { LLMSettingsScreen } from "./LLMSettingsScreen";

// Build a LocalModelInfo with sensible defaults; tests override what they assert on.
function lm(over: Partial<LocalModelInfo> & Pick<LocalModelInfo, "id" | "label">): LocalModelInfo {
  const m: LocalModelInfo = {
    enabled: false,
    available: false,
    queued: false,
    remove_queued: false,
    loaded: false,
    supports_vision: false,
    supports_tools: true,
    tiers: [],
    quant: "Q8_0",
    size_gb: 0,
    disk_gb: null,
    download_gb: null,
    note: "",
    context_window: 32768,
    max_context_window: 32768,
    context_window_override: null,
    kv_gb: 0,
    ...over,
  };
  // Default effective-available to provisioned unless a test sets it explicitly.
  return { ...m, available: over.available ?? m.enabled };
}

function initialSettings(): LlmSettings {
  return {
    providers: [
      { id: "grok", label: "Grok 4.3", supports_reasoning: true, supports_vision: true },
      {
        id: "claude",
        label: "Claude Sonnet 4.6",
        supports_reasoning: false,
        supports_vision: true,
      },
      { id: "local", label: "Local model", supports_reasoning: false, supports_vision: true },
    ],
    reasoning_efforts: ["none", "low", "medium", "high"],
    reasoning_default: "low",
    tasks: [
      { id: "agent.turn", label: "Agent turn", provider: "grok", reasoning_effort: "medium" },
      {
        id: "integrate.note",
        label: "Integrate note",
        provider: "grok",
        reasoning_effort: "medium",
      },
      {
        id: "fact.adjudicate",
        label: "Fact adjudicate",
        provider: "grok",
        reasoning_effort: "medium",
      },
      {
        id: "entity.disambiguate",
        label: "Entity disambiguate",
        provider: "grok",
        reasoning_effort: "medium",
      },
      { id: "note.extract", label: "Note extract", provider: "grok", reasoning_effort: "low" },
    ],
    local_hosting_enabled: false,
    local_models: [
      lm({
        id: "qwen3-vl-30b",
        label: "Qwen3-VL 30B",
        supports_vision: true,
        tiers: ["vision", "low"],
        size_gb: 32,
      }),
    ],
    host_memory: null,
    jcode: {
      enabled: false,
      model: "",
      default: "qwen3-coder-next",
      planner: "gpt-oss-120b",
      planner_default: "gpt-oss-120b",
      planner_same: "same",
      options: [],
    },
  };
}

const USAGE = {
  today: { input_tokens: 41_200, output_tokens: 12_400, cost_usd: 0.08 },
  month: { input_tokens: 1_240_000, output_tokens: 338_000, cost_usd: 2.41 },
  by_task: [
    { task: "note.extract", input_tokens: 982_000, output_tokens: 241_000, cost_usd: 1.83 },
    // No price-table entry: the line must omit the cost cleanly.
    { task: "vision.ocr", input_tokens: 2_400_000, output_tokens: 990, cost_usd: null },
  ],
  days: [],
};

// A stateful stub: GET serves the fixture, PUT applies each task patch the way
// the backend does (a reasoning-capable provider keeps reasoning, others null it)
// and echoes it back.
function stubLlmFetch(seed?: LlmSettings) {
  const state = seed ?? initialSettings();
  const puts: { tasks: Record<string, { provider: string; reasoning_effort?: string }> }[] = [];
  const jcodePuts: string[] = [];
  const jcodePlannerPuts: string[] = [];
  const fetchMock = vi.fn<typeof fetch>(async (input, init) => {
    const path = String(input);
    // The code-mode executor selector PUTs here and gets the full snapshot back.
    if (path === "/api/settings/llm/jcode-model") {
      const body = JSON.parse(String(init?.body)) as { model: string };
      jcodePuts.push(body.model);
      state.jcode.model = body.model || state.jcode.default;
      return new Response(JSON.stringify(state), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    }
    // The planner selector PUTs here; "" reverts to the split default, "same" is single-model.
    if (path === "/api/settings/llm/jcode-planner") {
      const body = JSON.parse(String(init?.body)) as { planner: string };
      jcodePlannerPuts.push(body.planner);
      state.jcode.planner = body.planner || state.jcode.planner_default;
      return new Response(JSON.stringify(state), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    }
    // The AI-usage drawer self-fetches its telemetry; serve it so the stub
    // doesn't throw on a path the screen now legitimately calls.
    if (path === "/api/ops/llm-usage") {
      return new Response(JSON.stringify(USAGE), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    }
    // The screen also self-fetches the image service; serve a disabled snapshot so
    // these LLM-focused tests don't error on a path they don't care about.
    if (path === "/api/settings/image") {
      return new Response(
        JSON.stringify({ enabled: false, reachable: false, models: [], memory: null }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      );
    }
    // The Download action + its status poll — an install/uninstall auto-starts it.
    if (path === "/api/ops/local-provision") {
      return new Response(JSON.stringify({ oneshot: "jbrain-provision-1" }), {
        status: 202,
        headers: { "Content-Type": "application/json" },
      });
    }
    if (path === "/api/ops/local-provision/status") {
      return new Response(JSON.stringify({ state: "running", exit_code: null, log_tail: "" }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    }
    if (path !== "/api/settings/llm") throw new Error(`Unexpected fetch: ${path}`);
    if ((init?.method ?? "GET").toUpperCase() === "PUT") {
      const body = JSON.parse(String(init?.body)) as (typeof puts)[number];
      puts.push(body);
      for (const [id, patch] of Object.entries(body.tasks)) {
        const task = state.tasks.find((t) => t.id === id);
        if (!task) continue;
        task.provider = patch.provider as typeof task.provider;
        const reasons = state.providers.find((p) => p.id === patch.provider)?.supports_reasoning;
        task.reasoning_effort = reasons
          ? ((patch.reasoning_effort as typeof task.reasoning_effort) ??
            task.reasoning_effort ??
            "low")
          : null;
      }
    }
    return new Response(JSON.stringify(state), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  });
  vi.stubGlobal("fetch", fetchMock);
  return { puts, jcodePuts, jcodePlannerPuts, state };
}

beforeEach(() => stubLlmFetch());
afterEach(() => vi.unstubAllGlobals());

async function group(name: string): Promise<HTMLElement> {
  const heading = await screen.findByText(name);
  // Climb to the enclosing tier card (the heading's section ancestor).
  const section = heading.closest("section");
  if (!section) throw new Error(`no section for ${name}`);
  return section as HTMLElement;
}

describe("LLMSettingsScreen", () => {
  it("renders the tiers from fetched data", async () => {
    render(<LLMSettingsScreen />);
    expect(await screen.findByText("High reasoning")).toBeInTheDocument();
    expect(screen.getByText("Medium reasoning")).toBeInTheDocument();
    expect(screen.getByText("Low reasoning")).toBeInTheDocument();
    // The fixture's arbiter tasks (integrate.note, fact.adjudicate) land in the
    // high-reasoning bucket; agent.turn/note.extract are medium, the one-shots low.
    const high = await group("High reasoning");
    expect(within(high).getByText("2 tasks")).toBeInTheDocument();
  });

  it("hides the code-mode model card when jcode is disabled", async () => {
    // Default fixture has jcode.enabled = false.
    render(<LLMSettingsScreen />);
    await screen.findByText("High reasoning");
    expect(screen.queryByLabelText("Code mode executor model")).not.toBeInTheDocument();
  });

  function jcodeSeed(): LlmSettings {
    const seed = initialSettings();
    seed.local_hosting_enabled = true;
    seed.jcode = {
      enabled: true,
      model: "qwen3-coder-next",
      default: "qwen3-coder-next",
      planner: "gpt-oss-120b",
      planner_default: "gpt-oss-120b",
      planner_same: "same",
      options: [
        { id: "qwen3-coder-next", label: "Qwen3-Coder-Next 80B" },
        { id: "qwen3-vl-30b", label: "Qwen3-VL 30B" },
        { id: "gpt-oss-120b", label: "GPT-OSS 120B" },
      ],
    };
    return seed;
  }

  it("changes the code-mode executor model via the jcode card", async () => {
    const { jcodePuts } = stubLlmFetch(jcodeSeed());
    render(<LLMSettingsScreen />);
    const select = (await screen.findByLabelText("Code mode executor model")) as HTMLSelectElement;
    expect(select.value).toBe("qwen3-coder-next");
    fireEvent.change(select, { target: { value: "qwen3-vl-30b" } });
    await waitFor(() => expect(jcodePuts).toContain("qwen3-vl-30b"));

    // The card now matches the role-tier styling (.llm-group) and sits LAST — at the
    // bottom of the list, under the vision tier.
    const groupNodes = Array.from(document.querySelectorAll(".llm-group"));
    const codeMode = document.querySelector(".llm-group.llm-jcode");
    expect(codeMode).not.toBeNull();
    expect(groupNodes[groupNodes.length - 1]).toBe(codeMode);
  });

  it("changes the planner and collapses to a single model via the jcode card", async () => {
    const { jcodePlannerPuts } = stubLlmFetch(jcodeSeed());
    render(<LLMSettingsScreen />);
    const planner = (await screen.findByLabelText("Code mode planner model")) as HTMLSelectElement;
    // The split default (the reasoner) is the current planner selection.
    expect(planner.value).toBe("gpt-oss-120b");

    // Picking a specific model PUTs its id.
    fireEvent.change(planner, { target: { value: "qwen3-vl-30b" } });
    await waitFor(() => expect(jcodePlannerPuts).toContain("qwen3-vl-30b"));

    // Picking "Same as executor" PUTs the single-model sentinel.
    fireEvent.change(planner, { target: { value: "same" } });
    await waitFor(() => expect(jcodePlannerPuts).toContain("same"));
  });

  it("hides reasoning and shows the Claude note when a tier moves off grok", async () => {
    render(<LLMSettingsScreen />);
    const high = await group("High reasoning");
    // Reasoning segments present while on grok.
    expect(within(high).getByRole("group", { name: /reasoning/i })).toBeInTheDocument();

    fireEvent.change(within(high).getByLabelText(/High reasoning provider/i), {
      target: { value: "claude" },
    });

    await waitFor(() =>
      expect(within(high).queryByRole("group", { name: /reasoning/i })).not.toBeInTheDocument(),
    );
    expect(within(high).getByText("Claude manages thinking on its own.")).toBeInTheDocument();
  });

  it("issues an update when a tier's reasoning effort changes", async () => {
    const { puts } = stubLlmFetch();
    render(<LLMSettingsScreen />);
    const med = await group("Medium reasoning");
    const reasoning = within(med).getByRole("group", { name: /Medium reasoning reasoning/i });

    fireEvent.click(within(reasoning).getByRole("button", { name: "High" }));

    // Every grok task in the tier gets the new level on the wire.
    await waitFor(() => expect(puts.length).toBeGreaterThan(0));
    const lastPatch = puts[puts.length - 1]?.tasks ?? {};
    expect(lastPatch["agent.turn"]).toEqual({ provider: "grok", reasoning_effort: "high" });
  });

  it("offers the reasoning control for a reasoning-capable local model", async () => {
    // A task pinned to a local gpt-oss (supports_reasoning) shows the segments and
    // sends the chosen level — the control is capability-driven, not grok-only.
    const s = initialSettings();
    s.providers = [
      { id: "grok", label: "Grok 4.3", supports_reasoning: true, supports_vision: true },
      {
        id: "gpt-oss-120b",
        label: "GPT-OSS 120B",
        supports_reasoning: true,
        supports_vision: false,
      },
      { id: "qwen3-30b", label: "Qwen3 30B", supports_reasoning: false, supports_vision: false },
    ];
    s.tasks = [
      { id: "agent.turn", label: "Agent turn", provider: "gpt-oss-120b", reasoning_effort: "low" },
    ];
    const { puts } = stubLlmFetch(s);
    render(<LLMSettingsScreen />);
    const med = await group("Medium reasoning");
    const reasoning = within(med).getByRole("group", { name: /Medium reasoning reasoning/i });

    fireEvent.click(within(reasoning).getByRole("button", { name: "High" }));

    await waitFor(() => expect(puts.length).toBeGreaterThan(0));
    const lastPatch = puts[puts.length - 1]?.tasks ?? {};
    expect(lastPatch["agent.turn"]).toEqual({ provider: "gpt-oss-120b", reasoning_effort: "high" });
  });

  it("drops the reasoning control for a non-reasoning local model", async () => {
    const s = initialSettings();
    s.providers = [
      { id: "grok", label: "Grok 4.3", supports_reasoning: true, supports_vision: true },
      { id: "qwen3-30b", label: "Qwen3 30B", supports_reasoning: false, supports_vision: false },
    ];
    s.tasks = [
      { id: "agent.turn", label: "Agent turn", provider: "qwen3-30b", reasoning_effort: null },
    ];
    stubLlmFetch(s);
    render(<LLMSettingsScreen />);
    const med = await group("Medium reasoning");
    expect(within(med).queryByRole("group", { name: /reasoning/i })).not.toBeInTheDocument();
    expect(within(med).getByText("This model takes no reasoning level.")).toBeInTheDocument();
  });

  it("omits text-only local models from the Vision tier's choices", async () => {
    const s = initialSettings();
    s.providers = [
      { id: "grok", label: "Grok 4.3", supports_reasoning: true, supports_vision: true },
      { id: "qwen3-vl-30b", label: "Qwen3-VL", supports_reasoning: false, supports_vision: true },
      { id: "gpt-oss-120b", label: "GPT-OSS", supports_reasoning: false, supports_vision: false },
    ];
    s.tasks = [
      { id: "vision.ocr", label: "Vision OCR", provider: "grok", reasoning_effort: null },
      { id: "session.title", label: "Session title", provider: "grok", reasoning_effort: "low" },
    ];
    vi.stubGlobal(
      "fetch",
      vi.fn<typeof fetch>(
        async () =>
          new Response(JSON.stringify(s), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          }),
      ),
    );
    render(<LLMSettingsScreen />);

    const vision = await group("Vision");
    const visionSelect = within(vision).getByLabelText(/Vision provider/i) as HTMLSelectElement;
    const visionOptions = Array.from(visionSelect.options).map((o) => o.value);
    expect(visionOptions).toContain("qwen3-vl-30b");
    expect(visionOptions).not.toContain("gpt-oss-120b");

    // The text reasoner is still available to a non-vision tier.
    const low = await group("Low reasoning");
    const lowSelect = within(low).getByLabelText(/Low reasoning provider/i) as HTMLSelectElement;
    expect(Array.from(lowSelect.options).map((o) => o.value)).toContain("gpt-oss-120b");
  });

  it("shows enabled models with state, chips, and footprint", async () => {
    const s = initialSettings();
    s.local_hosting_enabled = true;
    s.local_models = [
      lm({
        id: "qwen3-vl-30b",
        label: "Qwen3-VL 30B",
        enabled: true,
        supports_vision: true,
        tiers: ["vision", "low"],
        size_gb: 32,
        // Provisioned here: a real measured footprint that differs from the estimate.
        disk_gb: 31.7,
      }),
      lm({
        id: "gpt-oss-120b",
        label: "GPT-OSS 120B",
        enabled: true,
        tiers: ["high"],
        quant: "MXFP4",
        size_gb: 59,
        // Enabled but weights not yet on disk → falls back to the flagged estimate.
      }),
    ];
    vi.stubGlobal(
      "fetch",
      vi.fn<typeof fetch>(
        async () =>
          new Response(JSON.stringify(s), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          }),
      ),
    );
    render(<LLMSettingsScreen />);

    // The On-box LLMs section is open by default; its meta count reflects the roster.
    const toggle = await screen.findByRole("button", { name: /On-box LLMs/i });
    expect(toggle).toHaveTextContent("2 available");

    // The Available tab (default) shows the enabled roster.
    expect(await screen.findByText("Qwen3-VL 30B")).toBeInTheDocument();
    // Enabled-but-not-resident reads "available" (both, here).
    expect(screen.getAllByText("available")).toHaveLength(2);
    // The text reasoner shows a reasoning chip, not a vision chip.
    const gpt = screen.getByText("GPT-OSS 120B").closest(".llm-local-row") as HTMLElement;
    expect(within(gpt).getByText("reasoning")).toBeInTheDocument();
    expect(within(gpt).queryByText("vision")).not.toBeInTheDocument();
    // A provisioned model shows its real measured footprint; one still downloading
    // shows the catalog estimate, flagged with "~".
    const qwen = screen.getByText("Qwen3-VL 30B").closest(".llm-local-row") as HTMLElement;
    expect(within(qwen).getByText(/Q8_0 · 31\.7 GB/)).toBeInTheDocument();
    expect(within(gpt).getByText(/MXFP4 · ~59 GB/)).toBeInTheDocument();
  });

  it("offers un-provisioned catalog models with Install in the Catalog tab", async () => {
    const s = initialSettings();
    s.local_hosting_enabled = true;
    s.local_models = [
      lm({
        id: "qwen3-vl-30b",
        label: "Qwen3-VL 30B",
        enabled: true,
        supports_vision: true,
        tiers: ["vision", "low"],
        size_gb: 32,
        disk_gb: 31.7,
      }),
      lm({
        id: "gpt-oss-120b",
        label: "GPT-OSS 120B",
        enabled: false,
        tiers: ["high"],
        quant: "MXFP4",
        size_gb: 59,
      }),
    ];
    vi.stubGlobal(
      "fetch",
      vi.fn<typeof fetch>(
        async () =>
          new Response(JSON.stringify(s), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          }),
      ),
    );
    render(<LLMSettingsScreen />);

    // The On-box LLMs section meta counts the installed roster.
    const toggle = await screen.findByRole("button", { name: /On-box LLMs/i });
    expect(toggle).toHaveTextContent("1 available");

    // The provisioned model is in the Available roster (default tab); the
    // un-provisioned one shows under the Catalogue tab with an Install button.
    expect(await screen.findByText("Qwen3-VL 30B")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("tab", { name: /Catalogue/i }));
    const gpt = (await screen.findByText("GPT-OSS 120B")).closest(".llm-local-row") as HTMLElement;
    expect(within(gpt).getByRole("button", { name: "Install" })).toBeInTheDocument();
  });

  it("shows loaded models and unloads them from memory", async () => {
    const s = initialSettings();
    s.local_hosting_enabled = true;
    s.host_memory = { total_gb: 128, used_gb: 92 };
    s.local_models = [
      lm({
        id: "qwen3-vl-30b",
        label: "Qwen3-VL 30B",
        enabled: true,
        loaded: true,
        supports_vision: true,
        tiers: ["vision", "low"],
        size_gb: 32,
        disk_gb: 32,
        kv_gb: 2,
      }),
    ];
    const calls: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn<typeof fetch>(async (input, init) => {
        const path = String(input);
        const method = (init?.method ?? "GET").toUpperCase();
        if (path === "/api/settings/llm" && method === "GET")
          return new Response(JSON.stringify(s), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          });
        if (path.endsWith("/local-models/qwen3-vl-30b/unload") && method === "POST") {
          calls.push(path);
          const m0 = s.local_models[0];
          if (m0) m0.loaded = false;
          return new Response(JSON.stringify({ loaded: [], reachable: true }), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          });
        }
        throw new Error(`unexpected fetch: ${method} ${path}`);
      }),
    );
    render(<LLMSettingsScreen />);

    // The On-box LLMs section is open by default and shows the loaded count.
    const toggle = await screen.findByRole("button", { name: /On-box LLMs/i });
    expect(toggle).toHaveTextContent("1 resident");

    // The resident model reads "resident" and offers an Unload button. It lives in both the
    // Resident and Available tabs; the Available tab is the default.
    expect(await screen.findByText("resident")).toBeInTheDocument();
    // The always-visible shared meter sums the resident footprint (32 weights + 2 KV).
    expect(screen.getByText("34 GB resident")).toBeInTheDocument();
    expect(screen.getByText("128 GB total")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Unload" }));

    await waitFor(() => expect(calls).toHaveLength(1));
    // After unload it flips to available and the button is gone.
    expect(await screen.findByText("available")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Unload" })).not.toBeInTheDocument();
  });

  it("stages (previews) then loads a model, evicting the resident one", async () => {
    const s = initialSettings();
    s.local_hosting_enabled = true;
    s.host_memory = { total_gb: 128, used_gb: 62 };
    s.local_models = [
      lm({
        id: "glm-46",
        label: "GLM-4.6",
        enabled: true,
        loaded: true, // resident — the one the preview will evict
        size_gb: 58,
        disk_gb: 58,
        kv_gb: 4,
      }),
      lm({
        id: "qwen3-vl-30b",
        label: "Qwen3-VL 30B",
        enabled: true, // available, not resident — the stage target
        size_gb: 40,
        disk_gb: 40,
        kv_gb: 3,
      }),
    ];
    const calls: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn<typeof fetch>(async (input, init) => {
        const path = String(input);
        const method = (init?.method ?? "GET").toUpperCase();
        if (path === "/api/settings/llm" && method === "GET")
          return new Response(JSON.stringify(s), { status: 200 });
        // Stage = dry-run: loading qwen would evict the resident GLM.
        if (path.endsWith("/qwen3-vl-30b/plan-load") && method === "POST") {
          calls.push(`plan ${path}`);
          return new Response(
            JSON.stringify({
              model_id: "qwen3-vl-30b",
              measured: true,
              already_resident: false,
              fits: false,
              over: false,
              victims: [{ id: "glm-46", label: "GLM-4.6", gb: 62 }],
              resident_gb: 62,
              projected_gb: 43,
              ceiling_gb: 96,
              total_gb: 128,
            }),
            { status: 200 },
          );
        }
        // Load = commit: GLM evicted, qwen resident.
        if (path.endsWith("/qwen3-vl-30b/load") && method === "POST") {
          calls.push(`load ${path}`);
          const glm = s.local_models[0];
          const qwen = s.local_models[1];
          if (glm) glm.loaded = false;
          if (qwen) qwen.loaded = true;
          return new Response(JSON.stringify({ loaded: ["qwen3-vl-30b"], reachable: true }), {
            status: 200,
          });
        }
        throw new Error(`unexpected fetch: ${method} ${path}`);
      }),
    );
    render(<LLMSettingsScreen />);
    // The On-box LLMs section + Available tab (with staging) are the defaults.
    await screen.findByRole("button", { name: /On-box LLMs/i });

    // The available (non-resident) model offers Stage.
    const qwenRow = (await screen.findByText("Qwen3-VL 30B")).closest(
      ".llm-local-row",
    ) as HTMLElement;
    fireEvent.click(within(qwenRow).getByRole("button", { name: "Stage" }));

    // The preview appears: the commit bar names the eviction, and GLM is flagged "will evict".
    expect(await screen.findByText("Load now")).toBeInTheDocument();
    expect(await screen.findByText("will evict")).toBeInTheDocument();
    expect(calls.some((c) => c.includes("plan"))).toBe(true);

    // Commit → the load endpoint runs, GLM evicted, qwen resident.
    fireEvent.click(screen.getByRole("button", { name: "Load now" }));
    await waitFor(() => expect(calls.some((c) => c.includes("load"))).toBe(true));
    // qwen now resident, offers Unload; the preview commit bar is gone.
    const qwenAfter = (await screen.findByText("Qwen3-VL 30B")).closest(
      ".llm-local-row",
    ) as HTMLElement;
    expect(await within(qwenAfter).findByText("resident")).toBeInTheDocument();
    expect(screen.queryByText("Load now")).not.toBeInTheDocument();
  });

  it("refuses to load a model that can't fit the box (over-box preview)", async () => {
    const s = initialSettings();
    s.local_hosting_enabled = true;
    s.host_memory = { total_gb: 20, used_gb: 2 };
    s.local_models = [
      lm({ id: "gpt-oss-120b", label: "GPT-OSS 120B", enabled: true, size_gb: 63, disk_gb: 63 }),
    ];
    let loadCalled = false;
    vi.stubGlobal(
      "fetch",
      vi.fn<typeof fetch>(async (input, init) => {
        const path = String(input);
        const method = (init?.method ?? "GET").toUpperCase();
        if (path === "/api/settings/llm" && method === "GET")
          return new Response(JSON.stringify(s), { status: 200 });
        if (path.endsWith("/plan-load") && method === "POST")
          return new Response(
            JSON.stringify({
              model_id: "gpt-oss-120b",
              measured: true,
              already_resident: false,
              fits: false,
              over: true,
              over_box: true,
              victims: [],
              resident_gb: 2,
              projected_gb: 65,
              ceiling_gb: 15,
              total_gb: 20,
            }),
            { status: 200 },
          );
        if (path.endsWith("/load") && method === "POST") {
          loadCalled = true;
          return new Response(JSON.stringify({ loaded: [], reachable: true }), { status: 200 });
        }
        throw new Error(`unexpected fetch: ${method} ${path}`);
      }),
    );
    render(<LLMSettingsScreen />);
    await screen.findByRole("button", { name: /On-box LLMs/i });
    fireEvent.click(await screen.findByRole("button", { name: "Stage" }));

    // The commit bar refuses: the button reads "Can't load" and is disabled, and clicking it
    // does not call the load endpoint.
    const cantLoad = await screen.findByRole("button", { name: "Can't load" });
    expect(cantLoad).toBeDisabled();
    expect(screen.getByText(/Too big for this box/i)).toBeInTheDocument();
    fireEvent.click(cantLoad);
    expect(loadCalled).toBe(false);
  });

  it("edits an idle model's context window via the dropdown", async () => {
    const s = initialSettings();
    s.local_hosting_enabled = true;
    s.host_memory = { total_gb: 128, used_gb: 0 };
    s.local_models = [
      lm({
        id: "gpt-oss-120b",
        label: "GPT-OSS 120B",
        enabled: true,
        tiers: ["high"],
        quant: "MXFP4",
        size_gb: 59,
        disk_gb: 59,
        context_window: 131072,
        max_context_window: 131072,
        kv_gb: 4.5,
      }),
    ];
    let putBody: { context_window: number | null } | null = null;
    vi.stubGlobal(
      "fetch",
      vi.fn<typeof fetch>(async (input, init) => {
        const path = String(input);
        const method = (init?.method ?? "GET").toUpperCase();
        if (path === "/api/settings/llm" && method === "GET")
          return new Response(JSON.stringify(s), { status: 200 });
        if (path.endsWith("/context-window") && method === "PUT") {
          putBody = JSON.parse(String(init?.body));
          const m0 = s.local_models[0];
          if (m0) m0.context_window_override = putBody?.context_window ?? null;
          return new Response(JSON.stringify(s), { status: 200 });
        }
        throw new Error(`unexpected fetch: ${method} ${path}`);
      }),
    );
    render(<LLMSettingsScreen />);
    await screen.findByRole("button", { name: /On-box LLMs/i });

    const select = (await screen.findByLabelText("context window")) as HTMLSelectElement;
    // Defaults to the catalog window (128k) and offers the capped choices.
    expect(select.value).toBe("131072");
    fireEvent.change(select, { target: { value: "65536" } });
    await waitFor(() => expect(putBody).toEqual({ context_window: 65536 }));
  });

  it("offers windows above the served default up to the native ceiling", async () => {
    const s = initialSettings();
    s.local_hosting_enabled = true;
    s.host_memory = { total_gb: 128, used_gb: 0 };
    // Serves 32k by default but is natively 256k — the picker exposes the bigger
    // windows so the operator can opt into a -c the weights support.
    s.local_models = [
      lm({
        id: "qwen3-coder-next",
        label: "Qwen3-Coder-Next 80B",
        enabled: true,
        tiers: ["high"],
        quant: "UD-Q4_K_XL",
        size_gb: 50,
        disk_gb: 50,
        context_window: 32768,
        max_context_window: 262144,
        kv_gb: 1.3,
      }),
    ];
    let putBody: { context_window: number | null } | null = null;
    vi.stubGlobal(
      "fetch",
      vi.fn<typeof fetch>(async (input, init) => {
        const path = String(input);
        const method = (init?.method ?? "GET").toUpperCase();
        if (path === "/api/settings/llm" && method === "GET")
          return new Response(JSON.stringify(s), { status: 200 });
        if (path.endsWith("/context-window") && method === "PUT") {
          putBody = JSON.parse(String(init?.body));
          const m0 = s.local_models[0];
          if (m0) m0.context_window_override = putBody?.context_window ?? null;
          return new Response(JSON.stringify(s), { status: 200 });
        }
        throw new Error(`unexpected fetch: ${method} ${path}`);
      }),
    );
    render(<LLMSettingsScreen />);
    await screen.findByRole("button", { name: /On-box LLMs/i });

    const select = (await screen.findByLabelText("context window")) as HTMLSelectElement;
    expect(select.value).toBe("32768"); // the served default
    const values = Array.from(select.options).map((o) => o.value);
    // The native window (256k) and intermediate steps above the default are offered.
    expect(values).toContain("262144");
    expect(values).toContain("131072");
    fireEvent.change(select, { target: { value: "262144" } });
    await waitFor(() => expect(putBody).toEqual({ context_window: 262144 }));
  });

  it("exposes the 500k and 1M steps for a model with a million-token ceiling", async () => {
    const s = initialSettings();
    s.local_hosting_enabled = true;
    s.host_memory = { total_gb: 128, used_gb: 0 };
    // Llama 4 Scout's native window reaches 1M — the picker surfaces the 500k/1M steps
    // and labels the million-token window "1M" (not "1000k").
    s.local_models = [
      lm({
        id: "llama-4-scout-int4",
        label: "Llama 4 Scout · vision (int4)",
        enabled: true,
        tiers: ["vision", "low"],
        quant: "UD-Q4_K_XL",
        size_gb: 59,
        disk_gb: 59,
        context_window: 32768,
        max_context_window: 1000000,
        kv_gb: 1.5,
      }),
    ];
    let putBody: { context_window: number | null } | null = null;
    vi.stubGlobal(
      "fetch",
      vi.fn<typeof fetch>(async (input, init) => {
        const path = String(input);
        const method = (init?.method ?? "GET").toUpperCase();
        if (path === "/api/settings/llm" && method === "GET")
          return new Response(JSON.stringify(s), { status: 200 });
        if (path.endsWith("/context-window") && method === "PUT") {
          putBody = JSON.parse(String(init?.body));
          const m0 = s.local_models[0];
          if (m0) m0.context_window_override = putBody?.context_window ?? null;
          return new Response(JSON.stringify(s), { status: 200 });
        }
        throw new Error(`unexpected fetch: ${method} ${path}`);
      }),
    );
    render(<LLMSettingsScreen />);
    await screen.findByRole("button", { name: /On-box LLMs/i });

    const select = (await screen.findByLabelText("context window")) as HTMLSelectElement;
    const options = Array.from(select.options);
    const values = options.map((o) => o.value);
    expect(values).toContain("500000");
    expect(values).toContain("1000000");
    // The million-token window reads as "1M", the 500k step as "500k".
    expect(options.find((o) => o.value === "1000000")?.textContent).toBe("1M");
    expect(options.find((o) => o.value === "500000")?.textContent).toBe("500k");
    fireEvent.change(select, { target: { value: "1000000" } });
    await waitFor(() => expect(putBody).toEqual({ context_window: 1000000 }));
  });

  it("locks the context window while a model is loaded", async () => {
    const s = initialSettings();
    s.local_hosting_enabled = true;
    s.host_memory = { total_gb: 128, used_gb: 0 };
    s.local_models = [
      lm({
        id: "gpt-oss-120b",
        label: "GPT-OSS 120B",
        enabled: true,
        loaded: true,
        tiers: ["high"],
        quant: "MXFP4",
        size_gb: 59,
        disk_gb: 59,
        context_window: 131072,
        max_context_window: 131072,
        kv_gb: 4.5,
      }),
    ];
    vi.stubGlobal(
      "fetch",
      vi.fn<typeof fetch>(async () => new Response(JSON.stringify(s), { status: 200 })),
    );
    render(<LLMSettingsScreen />);
    await screen.findByRole("button", { name: /On-box LLMs/i });

    const select = (await screen.findByLabelText("context window")) as HTMLSelectElement;
    expect(select.disabled).toBe(true);
    expect(screen.getByText(/unload to change/)).toBeInTheDocument();
  });

  it("queues an un-provisioned model for install and starts its download (no system update)", async () => {
    const s = initialSettings();
    s.local_hosting_enabled = true;
    s.host_memory = { total_gb: 128, used_gb: 0 };
    s.local_models = [
      lm({ id: "qwen3-235b-a22b", label: "Qwen3-235B-A22B", tiers: ["high"], size_gb: 104 }),
    ];
    const calls: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn<typeof fetch>(async (input, init) => {
        const path = String(input);
        const method = (init?.method ?? "GET").toUpperCase();
        if (path === "/api/settings/llm" && method === "GET")
          return new Response(JSON.stringify(s), { status: 200 });
        if (path.endsWith("/qwen3-235b-a22b/install") && method === "POST") {
          calls.push(path);
          const m0 = s.local_models[0];
          if (m0) m0.queued = true;
          return new Response(JSON.stringify(s), { status: 200 });
        }
        if (path === "/api/ops/local-provision" && method === "POST") {
          calls.push(path);
          return new Response(JSON.stringify({ oneshot: "jbrain-provision-1" }), { status: 202 });
        }
        if (path === "/api/ops/local-provision/status")
          return new Response(
            JSON.stringify({ state: "running", exit_code: null, log_tail: "[local-llm] ↓" }),
            { status: 200 },
          );
        throw new Error(`unexpected fetch: ${method} ${path}`);
      }),
    );
    render(<LLMSettingsScreen />);
    await screen.findByRole("button", { name: /On-box LLMs/i });
    // The un-provisioned model lives in the Catalog tab.
    fireEvent.click(screen.getByRole("tab", { name: /Catalog/i }));

    // No queue bar until something is queued.
    expect(screen.queryByRole("button", { name: /Download now/i })).not.toBeInTheDocument();
    // A single tap both queues the install AND kicks the download — no system update.
    fireEvent.click(await screen.findByRole("button", { name: "Install" }));
    await waitFor(() =>
      expect(calls).toContain("/api/settings/llm/local-models/qwen3-235b-a22b/install"),
    );
    await waitFor(() => expect(calls).toContain("/api/ops/local-provision"));
    // No system-update endpoint is ever touched.
    expect(calls).not.toContain("/api/ops/update");

    // The queue bar appears with the GB tally.
    expect(await screen.findByText(/1 to download · 104 GB/)).toBeInTheDocument();
  });

  it("surfaces the download bar for a pending uninstall with nothing queued to install", async () => {
    const s = initialSettings();
    s.local_hosting_enabled = true;
    s.host_memory = { total_gb: 128, used_gb: 0 };
    // One provisioned model queued for removal, nothing queued for install.
    s.local_models = [
      lm({
        id: "qwen3-vl-30b",
        label: "Qwen3-VL 30B",
        enabled: true,
        remove_queued: true,
        size_gb: 32,
        disk_gb: 32,
      }),
    ];
    const calls: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn<typeof fetch>(async (input, init) => {
        const path = String(input);
        const method = (init?.method ?? "GET").toUpperCase();
        if (path === "/api/settings/llm" && method === "GET")
          return new Response(JSON.stringify(s), { status: 200 });
        if (path === "/api/ops/local-provision" && method === "POST") {
          calls.push(path);
          return new Response(JSON.stringify({ oneshot: "jbrain-provision-1" }), { status: 202 });
        }
        if (path === "/api/ops/local-provision/status")
          return new Response(JSON.stringify({ state: "running", exit_code: null, log_tail: "" }), {
            status: 200,
          });
        throw new Error(`unexpected fetch: ${method} ${path}`);
      }),
    );
    render(<LLMSettingsScreen />);
    await screen.findByRole("button", { name: /On-box LLMs/i });
    fireEvent.click(screen.getByRole("tab", { name: /Catalog/i }));

    // The bar reports the pending removal and offers an in-app trigger to apply it.
    expect(await screen.findByText(/1 to remove/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Apply now/i }));
    await waitFor(() => expect(calls).toContain("/api/ops/local-provision"));
  });

  it("renders a live download bar from a queued model's on-disk bytes", async () => {
    const s = initialSettings();
    s.local_hosting_enabled = true;
    s.host_memory = { total_gb: 128, used_gb: 0 };
    s.local_models = [
      lm({
        id: "qwen3-235b-a22b",
        label: "Qwen3-235B-A22B",
        tiers: ["high"],
        size_gb: 104,
        queued: true,
        download_gb: 52, // half-way through the download
      }),
    ];
    vi.stubGlobal(
      "fetch",
      vi.fn<typeof fetch>(async () => new Response(JSON.stringify(s), { status: 200 })),
    );
    render(<LLMSettingsScreen />);
    await screen.findByRole("button", { name: /On-box LLMs/i });
    fireEvent.click(screen.getByRole("tab", { name: /Catalog/i }));

    // 52 / 104 GB on disk → 50%.
    expect(await screen.findByText(/52 \/ 104 GB · 50%/)).toBeInTheDocument();
  });

  it("points at the CLI when local hosting is off", async () => {
    render(<LLMSettingsScreen />); // default fixture: hosting off
    const toggle = await screen.findByRole("button", { name: /On-box LLMs/i });
    expect(toggle).toHaveTextContent("off");
    // The LLM section is open by default, so the CLI hint shows without a click.
    expect(await screen.findByText(/enable-local-models/)).toBeInTheDocument();
  });

  it("surfaces the image service: shared-meter segment, rows, and stop/free", async () => {
    const s = initialSettings();
    s.local_hosting_enabled = true;
    s.host_memory = { total_gb: 128, used_gb: 36 };
    s.local_models = [
      lm({
        id: "gpt-oss-120b",
        label: "GPT-OSS 120B",
        enabled: true,
        loaded: true,
        tiers: ["high"],
        quant: "MXFP4",
        size_gb: 59,
        disk_gb: 30,
        kv_gb: 4,
      }),
    ];
    const img: ImageSettings = {
      enabled: true,
      reachable: true,
      models: [
        {
          id: "qwen-image",
          label: "Qwen-Image · generate (fp8)",
          kind: "generate",
          enabled: true,
          recommended: true,
          size_gb: 28,
          disk_gb: 27.3,
          vram_gb: 20,
          note: "",
        },
        {
          id: "qwen-image-edit",
          label: "Qwen-Image-Edit · edit",
          kind: "edit",
          enabled: false,
          recommended: false,
          size_gb: 44,
          disk_gb: null,
          vram_gb: 38,
          note: "",
        },
      ],
      memory: { total_gb: 128, free_gb: 96 }, // 32 GB resident → a bar segment
    };
    const calls: string[] = [];
    const resp = (o: unknown, status = 200) =>
      new Response(JSON.stringify(o), { status, headers: { "Content-Type": "application/json" } });
    vi.stubGlobal(
      "fetch",
      vi.fn<typeof fetch>(async (input, init) => {
        const path = String(input);
        const method = (init?.method ?? "GET").toUpperCase();
        if (path === "/api/ops/llm-usage") return resp(USAGE);
        if (path === "/api/settings/llm") return resp(s);
        if (path === "/api/settings/image" && method === "GET") return resp(img);
        if (path === "/api/settings/image/free" && method === "POST") {
          calls.push("free");
          img.memory = { total_gb: 128, free_gb: 128 };
          return resp(img);
        }
        if (path === "/api/settings/image/service/stop" && method === "POST") {
          calls.push("stop");
          return resp({ service: "comfyui", action: "stop" }, 202);
        }
        throw new Error(`Unexpected fetch: ${path}`);
      }),
    );
    render(<LLMSettingsScreen />);

    // The always-visible shared meter carries an image segment (128 - 96 = 32 GB),
    // and the Image section's meta reads "running" — before opening the section.
    const imgToggle = await screen.findByRole("button", { name: /Image models/i });
    expect(imgToggle).toHaveTextContent("running");
    expect(document.querySelector(".llm-mem-img")).not.toBeNull();

    // Open the Image section to reach its service controls + catalog rows.
    fireEvent.click(imgToggle);
    const section = (await screen.findByText("Image · ComfyUI")).closest(
      ".onbox-svc",
    ) as HTMLElement;
    expect(within(section).getByText("running")).toBeInTheDocument();
    // The Installed tab (default) shows the enabled image model.
    expect(await screen.findByText("Qwen-Image · generate (fp8)")).toBeInTheDocument();

    // Free unloads the resident model; Stop halts the service — both proxy through.
    fireEvent.click(within(section).getByText("Free"));
    await waitFor(() => expect(calls).toContain("free"));
    fireEvent.click(within(section).getByText("Stop"));
    await waitFor(() => expect(calls).toContain("stop"));
  });

  it("renders the shared meter, two sections, and omnibox tabs", async () => {
    const s = initialSettings();
    s.local_hosting_enabled = true;
    s.host_memory = { total_gb: 128, used_gb: 34 };
    s.local_models = [
      lm({
        id: "qwen3-vl-30b",
        label: "Qwen3-VL 30B",
        enabled: true,
        loaded: true,
        size_gb: 32,
        disk_gb: 32,
        kv_gb: 2,
      }),
    ];
    vi.stubGlobal(
      "fetch",
      vi.fn<typeof fetch>(async () => new Response(JSON.stringify(s), { status: 200 })),
    );
    render(<LLMSettingsScreen />);

    // Both section toggles are present; the shared meter is visible without expanding.
    expect(await screen.findByRole("button", { name: /On-box LLMs/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Image models/i })).toBeInTheDocument();
    expect(screen.getByText("34 GB resident")).toBeInTheDocument();

    // The LLM section (open by default) carries Resident / Available / Catalogue tabs
    // (reversed order); Available is the active segment.
    expect(screen.getByRole("tab", { name: /Resident/i })).toBeInTheDocument();
    const available = screen.getByRole("tab", { name: /Available/i });
    expect(available).toBeInTheDocument();
    expect(available.className).toContain("seg-on");
    expect(screen.getByRole("tab", { name: /Catalogue/i })).toBeInTheDocument();
  });

  it("filters by tab: Available / Resident (empty hint) / Catalogue", async () => {
    const s = initialSettings();
    s.local_hosting_enabled = true;
    s.host_memory = { total_gb: 128, used_gb: 0 };
    s.local_models = [
      lm({ id: "qwen3-vl-30b", label: "Qwen3-VL 30B", enabled: true, size_gb: 32, disk_gb: 32 }),
      lm({ id: "gpt-oss-120b", label: "GPT-OSS 120B", size_gb: 59 }), // un-provisioned
    ];
    vi.stubGlobal(
      "fetch",
      vi.fn<typeof fetch>(async () => new Response(JSON.stringify(s), { status: 200 })),
    );
    render(<LLMSettingsScreen />);
    await screen.findByRole("button", { name: /On-box LLMs/i });

    // Available (default): the enabled roster, not the un-provisioned model.
    expect(await screen.findByText("Qwen3-VL 30B")).toBeInTheDocument();
    expect(screen.queryByText("GPT-OSS 120B")).not.toBeInTheDocument();

    // Resident: nothing loaded → the tab's empty hint (which points at staging).
    fireEvent.click(screen.getByRole("tab", { name: /Resident/i }));
    expect(await screen.findByText(/Stage an available model to load one/i)).toBeInTheDocument();

    // Catalogue: the full catalogue (both models).
    fireEvent.click(screen.getByRole("tab", { name: /Catalogue/i }));
    expect(await screen.findByText("GPT-OSS 120B")).toBeInTheDocument();
    expect(screen.getByText("Qwen3-VL 30B")).toBeInTheDocument();
  });

  it("uninstalls a provisioned model from the Catalog tab", async () => {
    const s = initialSettings();
    s.local_hosting_enabled = true;
    s.host_memory = { total_gb: 128, used_gb: 0 };
    s.local_models = [
      lm({ id: "qwen3-vl-30b", label: "Qwen3-VL 30B", enabled: true, size_gb: 32, disk_gb: 32 }),
    ];
    const calls: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn<typeof fetch>(async (input, init) => {
        const path = String(input);
        const method = (init?.method ?? "GET").toUpperCase();
        if (path === "/api/settings/llm" && method === "GET")
          return new Response(JSON.stringify(s), { status: 200 });
        if (path.endsWith("/qwen3-vl-30b/uninstall") && method === "POST") {
          calls.push(path);
          const m0 = s.local_models[0];
          if (m0) m0.remove_queued = true;
          return new Response(JSON.stringify(s), { status: 200 });
        }
        // Uninstall applies through the same sync one-shot the Download action uses.
        if (path === "/api/ops/local-provision" && method === "POST") {
          calls.push(path);
          return new Response(JSON.stringify({ oneshot: "jbrain-provision-1" }), { status: 202 });
        }
        if (path === "/api/ops/local-provision/status")
          return new Response(JSON.stringify({ state: "running", exit_code: null, log_tail: "" }), {
            status: 200,
          });
        throw new Error(`unexpected fetch: ${method} ${path}`);
      }),
    );
    render(<LLMSettingsScreen />);
    await screen.findByRole("button", { name: /On-box LLMs/i });
    fireEvent.click(screen.getByRole("tab", { name: /Catalog/i }));

    // A provisioned model in Catalog offers a tap-to-confirm Uninstall (danger) button:
    // the first tap only arms it, a second tap confirms and queues the removal.
    fireEvent.click(await screen.findByRole("button", { name: "Uninstall" }));
    expect(calls).toEqual([]); // armed — nothing fired yet
    fireEvent.click(screen.getByRole("button", { name: /confirm/i }));
    await waitFor(() =>
      expect(calls).toContain("/api/settings/llm/local-models/qwen3-vl-30b/uninstall"),
    );
    // The row now reads "uninstalling".
    expect(await screen.findByText("uninstalling")).toBeInTheDocument();
  });

  it("offers Enable + Remove for a disabled-but-on-disk model in the Catalog tab", async () => {
    const s = initialSettings();
    s.local_hosting_enabled = true;
    s.host_memory = { total_gb: 128, used_gb: 0 };
    // On disk (disk_gb set) but NOT enabled — an orphaned alt dropped from the roster.
    s.local_models = [
      lm({
        id: "qwen3-235b-a22b",
        label: "Qwen3-235B-A22B",
        tiers: ["high"],
        size_gb: 104,
        disk_gb: 97,
      }),
    ];
    const calls: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn<typeof fetch>(async (input, init) => {
        const path = String(input);
        const method = (init?.method ?? "GET").toUpperCase();
        if (path === "/api/settings/llm" && method === "GET")
          return new Response(JSON.stringify(s), { status: 200 });
        if (path.endsWith("/qwen3-235b-a22b/uninstall") && method === "POST") {
          calls.push(path);
          const m0 = s.local_models[0];
          if (m0) m0.remove_queued = true;
          return new Response(JSON.stringify(s), { status: 200 });
        }
        if (path === "/api/ops/local-provision" && method === "POST") {
          calls.push(path);
          return new Response(JSON.stringify({ oneshot: "jbrain-provision-1" }), { status: 202 });
        }
        if (path === "/api/ops/local-provision/status")
          return new Response(JSON.stringify({ state: "running", exit_code: null, log_tail: "" }), {
            status: 200,
          });
        throw new Error(`unexpected fetch: ${method} ${path}`);
      }),
    );
    render(<LLMSettingsScreen />);
    await screen.findByRole("button", { name: /On-box LLMs/i });
    fireEvent.click(screen.getByRole("tab", { name: /Catalog/i }));

    // On disk but disabled: Enable (re-add, no download) + a danger Remove — never a
    // plain Install, and the meta shows the on-disk size, not the ~estimate.
    expect(await screen.findByRole("button", { name: "Enable" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Install" })).not.toBeInTheDocument();
    expect(screen.getByText(/97 GB on disk/)).toBeInTheDocument();

    // Remove reclaims the orphaned weights — a tap-to-confirm danger button.
    fireEvent.click(screen.getByRole("button", { name: "Remove" }));
    expect(calls).toEqual([]); // armed — nothing fired yet
    fireEvent.click(screen.getByRole("button", { name: /confirm/i }));
    await waitFor(() =>
      expect(calls).toContain("/api/settings/llm/local-models/qwen3-235b-a22b/uninstall"),
    );
    expect(await screen.findByText("uninstalling")).toBeInTheDocument();
  });

  it("does not offer Uninstall in the Available tab — Catalogue only", async () => {
    const s = initialSettings();
    s.local_hosting_enabled = true;
    s.host_memory = { total_gb: 128, used_gb: 0 };
    s.local_models = [
      lm({ id: "qwen3-vl-30b", label: "Qwen3-VL 30B", enabled: true, size_gb: 32, disk_gb: 32 }),
    ];
    vi.stubGlobal(
      "fetch",
      vi.fn<typeof fetch>(async () => new Response(JSON.stringify(s), { status: 200 })),
    );
    render(<LLMSettingsScreen />);
    // Available tab is the default; its row offers Stage (the load preview), not Uninstall.
    const row = (await screen.findByText("Qwen3-VL 30B")).closest(".llm-local-row") as HTMLElement;
    expect(within(row).queryByRole("button", { name: "Uninstall" })).not.toBeInTheDocument();
    expect(within(row).getByRole("button", { name: "Stage" })).toBeInTheDocument();
    // Uninstall lives in the Catalogue tab.
    fireEvent.click(screen.getByRole("tab", { name: /Catalogue/i }));
    expect(await screen.findByRole("button", { name: "Uninstall" })).toBeInTheDocument();
  });

  it("toggles a model available / unavailable from the Catalogue tab", async () => {
    const s = initialSettings();
    s.local_hosting_enabled = true;
    s.host_memory = { total_gb: 128, used_gb: 0 };
    s.local_models = [
      lm({
        id: "qwen3-vl-30b",
        label: "Qwen3-VL 30B",
        enabled: true,
        available: true,
        size_gb: 32,
        disk_gb: 32,
      }),
    ];
    const calls: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn<typeof fetch>(async (input, init) => {
        const path = String(input);
        const method = (init?.method ?? "GET").toUpperCase();
        if (path === "/api/settings/llm" && method === "GET")
          return new Response(JSON.stringify(s), { status: 200 });
        if (path.endsWith("/available") && method === "PUT") {
          calls.push(path);
          const on = (JSON.parse(String(init?.body)) as { available: boolean }).available;
          const m0 = s.local_models[0];
          if (m0) m0.available = on;
          return new Response(JSON.stringify(s), { status: 200 });
        }
        throw new Error(`unexpected fetch: ${method} ${path}`);
      }),
    );
    render(<LLMSettingsScreen />);
    await screen.findByRole("button", { name: /On-box LLMs/i });
    // Available tab (default): the model is in the roster.
    expect(await screen.findByText("Qwen3-VL 30B")).toBeInTheDocument();

    // Catalogue tab → make it unavailable.
    fireEvent.click(screen.getByRole("tab", { name: /Catalogue/i }));
    fireEvent.click(await screen.findByRole("button", { name: "Make unavailable" }));
    await waitFor(() => expect(calls).toHaveLength(1));

    // It drops out of the Available tab; the Catalogue row now offers "Make available".
    fireEvent.click(screen.getByRole("tab", { name: /Available/i }));
    expect(screen.queryByText("Qwen3-VL 30B")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("tab", { name: /Catalogue/i }));
    expect(await screen.findByRole("button", { name: "Make available" })).toBeInTheDocument();
  });

  it("arming Uninstall without a second tap does not queue a removal", async () => {
    const s = initialSettings();
    s.local_hosting_enabled = true;
    s.host_memory = { total_gb: 128, used_gb: 0 };
    s.local_models = [
      lm({ id: "qwen3-vl-30b", label: "Qwen3-VL 30B", enabled: true, size_gb: 32, disk_gb: 32 }),
    ];
    const calls: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn<typeof fetch>(async (input, init) => {
        const path = String(input);
        const method = (init?.method ?? "GET").toUpperCase();
        if (path === "/api/settings/llm" && method === "GET")
          return new Response(JSON.stringify(s), { status: 200 });
        if (path.endsWith("/uninstall")) calls.push(`${method} ${path}`);
        return new Response(JSON.stringify(s), { status: 200 });
      }),
    );
    render(<LLMSettingsScreen />);
    await screen.findByRole("button", { name: /On-box LLMs/i });
    fireEvent.click(screen.getByRole("tab", { name: /Catalog/i }));

    // One tap only arms it (label flips to Confirm?); no request fires, the model stays.
    fireEvent.click(await screen.findByRole("button", { name: "Uninstall" }));
    expect(screen.getByRole("button", { name: /confirm/i })).toBeInTheDocument();
    await waitFor(() => expect(calls).toEqual([]));
  });

  it("cancels a queued uninstall via Keep (DELETE, no confirm)", async () => {
    const s = initialSettings();
    s.local_hosting_enabled = true;
    s.host_memory = { total_gb: 128, used_gb: 0 };
    s.local_models = [
      lm({
        id: "qwen3-vl-30b",
        label: "Qwen3-VL 30B",
        enabled: true,
        remove_queued: true,
        size_gb: 32,
        disk_gb: 32,
      }),
    ];
    const calls: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn<typeof fetch>(async (input, init) => {
        const path = String(input);
        const method = (init?.method ?? "GET").toUpperCase();
        if (path === "/api/settings/llm" && method === "GET")
          return new Response(JSON.stringify(s), { status: 200 });
        if (path.endsWith("/qwen3-vl-30b/uninstall")) {
          calls.push(`${method} ${path}`);
          if (method === "DELETE") {
            const m0 = s.local_models[0];
            if (m0) m0.remove_queued = false;
          }
          return new Response(JSON.stringify(s), { status: 200 });
        }
        throw new Error(`unexpected fetch: ${method} ${path}`);
      }),
    );
    const confirm = vi.spyOn(window, "confirm");
    render(<LLMSettingsScreen />);
    await screen.findByRole("button", { name: /On-box LLMs/i });
    fireEvent.click(screen.getByRole("tab", { name: /Catalog/i }));

    // A queued removal swaps Uninstall → Keep; clicking it backs the removal out
    // with a DELETE and no confirm prompt.
    fireEvent.click(await screen.findByRole("button", { name: "Keep" }));
    await waitFor(() =>
      expect(calls).toContain("DELETE /api/settings/llm/local-models/qwen3-vl-30b/uninstall"),
    );
    expect(confirm).not.toHaveBeenCalled();
    expect(await screen.findByRole("button", { name: "Uninstall" })).toBeInTheDocument();
    confirm.mockRestore();
  });

  it("shows Install and Uninstall side-by-side in the Catalog tab", async () => {
    const s = initialSettings();
    s.local_hosting_enabled = true;
    s.host_memory = { total_gb: 128, used_gb: 0 };
    s.local_models = [
      lm({ id: "qwen3-vl-30b", label: "Qwen3-VL 30B", enabled: true, size_gb: 32, disk_gb: 32 }),
      lm({ id: "gpt-oss-120b", label: "GPT-OSS 120B", size_gb: 59 }), // un-provisioned
    ];
    vi.stubGlobal(
      "fetch",
      vi.fn<typeof fetch>(async () => new Response(JSON.stringify(s), { status: 200 })),
    );
    render(<LLMSettingsScreen />);
    await screen.findByRole("button", { name: /On-box LLMs/i });
    fireEvent.click(screen.getByRole("tab", { name: /Catalog/i }));

    const provisioned = (await screen.findByText("Qwen3-VL 30B")).closest(
      ".llm-local-row",
    ) as HTMLElement;
    expect(within(provisioned).getByRole("button", { name: "Uninstall" })).toBeInTheDocument();
    const unprovisioned = screen.getByText("GPT-OSS 120B").closest(".llm-local-row") as HTMLElement;
    expect(within(unprovisioned).getByRole("button", { name: "Install" })).toBeInTheDocument();
  });

  it("lets a per-task override diverge from its tier", async () => {
    const { state } = stubLlmFetch();
    render(<LLMSettingsScreen />);
    const med = await group("Medium reasoning");

    // Expand the per-task overrides, then move one task off grok.
    fireEvent.click(within(med).getByRole("button", { name: /Per-task overrides/i }));
    const taskSelect = await within(med).findByLabelText(/Agent turn provider/i);
    fireEvent.change(taskSelect, { target: { value: "local" } });

    await waitFor(() =>
      expect(state.tasks.find((t) => t.id === "agent.turn")?.provider).toBe("local"),
    );
    // The siblings stay on grok — the tier control now reflects "mixed".
    expect(state.tasks.find((t) => t.id === "note.extract")?.provider).toBe("grok");
    await waitFor(() =>
      expect(
        (within(med).getByLabelText(/Medium reasoning provider/i) as HTMLSelectElement).value,
      ).toBe("mixed"),
    );
  });

  it("AI usage drawer: expands to today/month and per-task spend, k/M + null cost", async () => {
    render(<LLMSettingsScreen />);
    fireEvent.click(await screen.findByRole("button", { name: /AI usage/i }));

    expect(await screen.findByText("41k in · 12k out · ~$0.08")).toBeInTheDocument();
    // The month line shows both in the collapsed-header summary and the row.
    expect(screen.getAllByText("1.2M in · 338k out · ~$2.41").length).toBeGreaterThan(0);
    expect(screen.getByText("note.extract")).toBeInTheDocument();
    expect(screen.getByText("982k in · 241k out · ~$1.83")).toBeInTheDocument();
    // vision.ocr has no price-table entry — tokens only, no guessed cost.
    expect(screen.getByText("2.4M in · 990 out")).toBeInTheDocument();
  });
});
