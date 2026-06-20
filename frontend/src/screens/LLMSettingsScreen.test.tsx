import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { LlmSettings, LocalModelInfo } from "../api/client";
import { LLMSettingsScreen } from "./LLMSettingsScreen";

// Build a LocalModelInfo with sensible defaults; tests override what they assert on.
function lm(over: Partial<LocalModelInfo> & Pick<LocalModelInfo, "id" | "label">): LocalModelInfo {
  return {
    enabled: false,
    loaded: false,
    supports_vision: false,
    supports_tools: true,
    tiers: [],
    quant: "Q8_0",
    size_gb: 0,
    disk_gb: null,
    note: "",
    context_window: 32768,
    context_window_override: null,
    staged: false,
    kv_gb: 0,
    ...over,
  };
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
  const fetchMock = vi.fn<typeof fetch>(async (input, init) => {
    const path = String(input);
    // The AI-usage drawer self-fetches its telemetry; serve it so the stub
    // doesn't throw on a path the screen now legitimately calls.
    if (path === "/api/ops/llm-usage") {
      return new Response(JSON.stringify(USAGE), {
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
  return { puts, state };
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
    expect(await screen.findByText("High-stakes reasoning")).toBeInTheDocument();
    expect(screen.getByText("Lightweight")).toBeInTheDocument();
    // 3 of the 5 fixture tasks land in high (agent.turn, integrate.note,
    // note.extract); entity.disambiguate + fact.adjudicate fall to lightweight.
    const high = await group("High-stakes reasoning");
    expect(within(high).getByText("3 tasks")).toBeInTheDocument();
  });

  it("hides reasoning and shows the Claude note when a tier moves off grok", async () => {
    render(<LLMSettingsScreen />);
    const high = await group("High-stakes reasoning");
    // Reasoning segments present while on grok.
    expect(within(high).getByRole("group", { name: /reasoning/i })).toBeInTheDocument();

    fireEvent.change(within(high).getByLabelText(/High-stakes reasoning provider/i), {
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
    const high = await group("High-stakes reasoning");
    const reasoning = within(high).getByRole("group", { name: /High-stakes reasoning reasoning/i });

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
    const high = await group("High-stakes reasoning");
    const reasoning = within(high).getByRole("group", { name: /High-stakes reasoning reasoning/i });

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
    const high = await group("High-stakes reasoning");
    expect(within(high).queryByRole("group", { name: /reasoning/i })).not.toBeInTheDocument();
    expect(within(high).getByText("This model takes no reasoning level.")).toBeInTheDocument();
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
    const light = await group("Lightweight");
    const lightSelect = within(light).getByLabelText(/Lightweight provider/i) as HTMLSelectElement;
    expect(Array.from(lightSelect.options).map((o) => o.value)).toContain("gpt-oss-120b");
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

    const toggle = await screen.findByRole("button", { name: /Local models/i });
    expect(toggle).toHaveTextContent("2 of 2 enabled");
    fireEvent.click(toggle);

    expect(await screen.findByText("Qwen3-VL 30B")).toBeInTheDocument();
    // Enabled-but-not-resident reads "idle" (both, here).
    expect(screen.getAllByText("idle")).toHaveLength(2);
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

  it("hides catalog models that aren't provisioned on this box", async () => {
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

    const toggle = await screen.findByRole("button", { name: /Local models/i });
    // The summary still counts the full catalog so "how many more could I install".
    expect(toggle).toHaveTextContent("1 of 2 enabled");
    fireEvent.click(toggle);

    expect(await screen.findByText("Qwen3-VL 30B")).toBeInTheDocument();
    // The un-provisioned model is absent from the list entirely.
    expect(screen.queryByText("GPT-OSS 120B")).not.toBeInTheDocument();
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

    const toggle = await screen.findByRole("button", { name: /Local models/i });
    // Summary surfaces runtime state with the resident footprint (weights + KV).
    expect(toggle).toHaveTextContent("1 loaded · 34 GB");
    fireEvent.click(toggle);

    // The resident model reads "loaded" and offers an Unload button.
    expect(await screen.findByText("loaded")).toBeInTheDocument();
    // The memory map sums the resident model's footprint (32 weights + 2 KV).
    expect(screen.getByText("34 GB used")).toBeInTheDocument();
    expect(screen.getByText("128 GB total")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Unload" }));

    await waitFor(() => expect(calls).toHaveLength(1));
    // After unload it flips to idle and the button is gone.
    expect(await screen.findByText("idle")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Unload" })).not.toBeInTheDocument();
  });

  it("stages then loads a model through the lifecycle", async () => {
    const s = initialSettings();
    s.local_hosting_enabled = true;
    s.host_memory = { total_gb: 128, used_gb: 0 };
    s.local_models = [
      lm({
        id: "qwen3-vl-30b",
        label: "Qwen3-VL 30B",
        enabled: true,
        size_gb: 32,
        disk_gb: 32,
        kv_gb: 1,
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
        if (path.endsWith("/stage") && method === "POST") {
          calls.push(`stage ${path}`);
          const m0 = s.local_models[0];
          if (m0) m0.staged = true;
          return new Response(JSON.stringify(s), { status: 200 });
        }
        if (path.endsWith("/load") && method === "POST") {
          calls.push(`load ${path}`);
          const m0 = s.local_models[0];
          if (m0) {
            m0.loaded = true;
            m0.staged = false;
          }
          return new Response(JSON.stringify({ loaded: ["qwen3-vl-30b"], reachable: true }), {
            status: 200,
          });
        }
        throw new Error(`unexpected fetch: ${method} ${path}`);
      }),
    );
    render(<LLMSettingsScreen />);
    fireEvent.click(await screen.findByRole("button", { name: /Local models/i }));

    // Idle → Stage.
    expect(await screen.findByText("idle")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Stage" }));
    // Staged → a Load button appears.
    expect(await screen.findByText("staged")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Load" }));
    // Loaded → reads loaded, offers Unload.
    expect(await screen.findByText("loaded")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Unload" })).toBeInTheDocument();
    expect(calls.some((c) => c.includes("stage"))).toBe(true);
    expect(calls.some((c) => c.includes("load"))).toBe(true);
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
    fireEvent.click(await screen.findByRole("button", { name: /Local models/i }));

    const select = (await screen.findByLabelText("context window")) as HTMLSelectElement;
    // Defaults to the catalog window (128k) and offers the capped choices.
    expect(select.value).toBe("131072");
    fireEvent.change(select, { target: { value: "65536" } });
    await waitFor(() => expect(putBody).toEqual({ context_window: 65536 }));
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
        kv_gb: 4.5,
      }),
    ];
    vi.stubGlobal(
      "fetch",
      vi.fn<typeof fetch>(async () => new Response(JSON.stringify(s), { status: 200 })),
    );
    render(<LLMSettingsScreen />);
    fireEvent.click(await screen.findByRole("button", { name: /Local models/i }));

    const select = (await screen.findByLabelText("context window")) as HTMLSelectElement;
    expect(select.disabled).toBe(true);
    expect(screen.getByText(/unload to change/)).toBeInTheDocument();
  });

  it("points at the CLI when local hosting is off", async () => {
    render(<LLMSettingsScreen />); // default fixture: hosting off
    const toggle = await screen.findByRole("button", { name: /Local models/i });
    expect(toggle).toHaveTextContent("off");
    fireEvent.click(toggle);
    expect(await screen.findByText(/enable-local-models/)).toBeInTheDocument();
  });

  it("lets a per-task override diverge from its tier", async () => {
    const { state } = stubLlmFetch();
    render(<LLMSettingsScreen />);
    const high = await group("High-stakes reasoning");

    // Expand the per-task overrides, then move one task off grok.
    fireEvent.click(within(high).getByRole("button", { name: /Per-task overrides/i }));
    const taskSelect = await within(high).findByLabelText(/Agent turn provider/i);
    fireEvent.change(taskSelect, { target: { value: "local" } });

    await waitFor(() =>
      expect(state.tasks.find((t) => t.id === "agent.turn")?.provider).toBe("local"),
    );
    // The siblings stay on grok — the tier control now reflects "mixed".
    expect(state.tasks.find((t) => t.id === "integrate.note")?.provider).toBe("grok");
    await waitFor(() =>
      expect(
        (within(high).getByLabelText(/High-stakes reasoning provider/i) as HTMLSelectElement).value,
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
