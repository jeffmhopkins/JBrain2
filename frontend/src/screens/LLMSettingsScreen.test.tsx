import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { LlmSettings } from "../api/client";
import { LLMSettingsScreen } from "./LLMSettingsScreen";

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
      {
        id: "qwen3-vl-30b",
        label: "Qwen3-VL 30B",
        enabled: false,
        supports_vision: true,
        supports_tools: true,
        tiers: ["vision", "low"],
        quant: "Q8_0",
        size_gb: 32,
        note: "",
      },
    ],
  };
}

// A stateful stub: GET serves the fixture, PUT applies each task patch the way
// the backend does (grok keeps reasoning, others null it) and echoes it back.
function stubLlmFetch() {
  const state = initialSettings();
  const puts: { tasks: Record<string, { provider: string; reasoning_effort?: string }> }[] = [];
  const fetchMock = vi.fn<typeof fetch>(async (input, init) => {
    const path = String(input);
    if (path !== "/api/settings/llm") throw new Error(`Unexpected fetch: ${path}`);
    if ((init?.method ?? "GET").toUpperCase() === "PUT") {
      const body = JSON.parse(String(init?.body)) as (typeof puts)[number];
      puts.push(body);
      for (const [id, patch] of Object.entries(body.tasks)) {
        const task = state.tasks.find((t) => t.id === id);
        if (!task) continue;
        task.provider = patch.provider as typeof task.provider;
        task.reasoning_effort =
          patch.provider === "grok"
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

  it("shows the local-models drawer with state and the enable command", async () => {
    const s = initialSettings();
    s.local_hosting_enabled = true;
    s.local_models = [
      {
        id: "qwen3-vl-30b",
        label: "Qwen3-VL 30B",
        enabled: true,
        supports_vision: true,
        supports_tools: true,
        tiers: ["vision", "low"],
        quant: "Q8_0",
        size_gb: 32,
        note: "",
      },
      {
        id: "gpt-oss-120b",
        label: "GPT-OSS 120B",
        enabled: false,
        supports_vision: false,
        supports_tools: true,
        tiers: ["high"],
        quant: "MXFP4",
        size_gb: 59,
        note: "",
      },
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
    expect(toggle).toHaveTextContent("1 of 2 enabled");
    fireEvent.click(toggle);

    expect(await screen.findByText("Qwen3-VL 30B")).toBeInTheDocument();
    // The enabled one reads "enabled"; the other reads "available".
    expect(screen.getByText("enabled")).toBeInTheDocument();
    expect(screen.getByText("available")).toBeInTheDocument();
    // The text reasoner shows a reasoning chip, not a vision chip.
    const gpt = screen.getByText("GPT-OSS 120B").closest(".llm-local-row") as HTMLElement;
    expect(within(gpt).getByText("reasoning")).toBeInTheDocument();
    expect(within(gpt).queryByText("vision")).not.toBeInTheDocument();
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
});
