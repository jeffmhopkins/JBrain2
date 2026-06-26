import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "../api/client";
import type { JcodeEvent, JcodeModelStatus, JcodeSession } from "../jcode/types";
import { JcodeSessionScreen } from "./JcodeSessionScreen";

const MODEL_STATUS: JcodeModelStatus = {
  model: "qwen3-coder-next",
  served: "qwen3-coder-next",
  loaded: true,
  warming: false,
  hosting: true,
  size_gb: 49.6,
};

// The screen polls model residency on mount; default to settled (not warming) so the bar
// is hidden (each test overrides for its own case).
beforeEach(() => {
  vi.spyOn(api, "jcodeModelStatus").mockResolvedValue(MODEL_STATUS);
});

const SESSION: JcodeSession = {
  id: "j1",
  repo: "github.com/me/scratch-todo",
  branch: "main",
  work_branch: "jcode/spike",
  status: "ready",
  created_at: "2026-06-25T00:00:00Z",
  last_active_at: "2026-06-25T00:00:00Z",
};

async function* turn(): AsyncGenerator<JcodeEvent> {
  yield { type: "run", run_id: "run-1" };
  yield { type: "text", text: "On it." };
  yield { type: "tool_use", tool: "Edit", data: { command: "edit src/app.ts" } };
  yield { type: "tool_result", tool: "Edit", text: "+4 −0", data: { ok: true } };
  yield { type: "text", text: " Done." };
  yield { type: "done" };
}

describe("JcodeSessionScreen", () => {
  it("shows the loading bar while the coder warms onto the box", async () => {
    // `warming` drives the bar (not `loaded`, which races true mid-load).
    vi.spyOn(api, "jcodeModelStatus").mockResolvedValue({
      ...MODEL_STATUS,
      loaded: false,
      warming: true,
    });
    render(<JcodeSessionScreen session={SESSION} onClose={vi.fn()} />);
    expect(await screen.findByText(/Loading qwen3-coder-next onto the box/i)).toBeInTheDocument();
  });

  it("shows the loading bar mid-race when the model is listed but still warming", async () => {
    // The race the fix targets: gateway lists the model (loaded:true) before the warm
    // task finishes. The bar must follow `warming`, so loaded:true + warming:true → shown.
    vi.spyOn(api, "jcodeModelStatus").mockResolvedValue({
      ...MODEL_STATUS,
      loaded: true,
      warming: true,
    });
    render(<JcodeSessionScreen session={SESSION} onClose={vi.fn()} />);
    expect(await screen.findByText(/Loading qwen3-coder-next onto the box/i)).toBeInTheDocument();
  });

  it("shows the loading bar when an existing session opens with the model evicted", async () => {
    // No fresh warm fires on opening an existing session, so `warming` is false. The
    // `hosting && !loaded` fallback still surfaces the bar when the model isn't resident.
    vi.spyOn(api, "jcodeModelStatus").mockResolvedValue({
      ...MODEL_STATUS,
      loaded: false,
      warming: false,
    });
    render(<JcodeSessionScreen session={SESSION} onClose={vi.fn()} />);
    expect(await screen.findByText(/Loading qwen3-coder-next onto the box/i)).toBeInTheDocument();
  });

  it("streams a turn into the chat transcript", async () => {
    vi.spyOn(api, "jcodeTurn").mockImplementation(() => turn());
    render(<JcodeSessionScreen session={SESSION} onClose={vi.fn()} />);

    fireEvent.change(screen.getByPlaceholderText(/Tell jcode/i), {
      target: { value: "add a button" },
    });
    fireEvent.click(screen.getByLabelText("Send"));

    expect(await screen.findByText("add a button")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByText(/On it\. Done\./)).toBeInTheDocument());
    // The tool the turn used renders in the transcript.
    expect(screen.getByText("edit src/app.ts")).toBeInTheDocument();
  });

  it("renders the agent's reply as markdown, not raw text", async () => {
    async function* md(): AsyncGenerator<JcodeEvent> {
      yield { type: "text", text: "Added the **submit** button." };
      yield { type: "done" };
    }
    vi.spyOn(api, "jcodeTurn").mockImplementation(() => md());
    render(<JcodeSessionScreen session={SESSION} onClose={vi.fn()} />);
    fireEvent.change(screen.getByPlaceholderText(/Tell jcode/i), { target: { value: "go" } });
    fireEvent.click(screen.getByLabelText("Send"));

    // The bold word renders as its own element (markdown parsed) — the literal `**`
    // syntax is gone, proving we don't dump raw text.
    const strong = await screen.findByText("submit");
    expect(strong.tagName).toBe("STRONG");
    expect(screen.queryByText(/\*\*submit\*\*/)).not.toBeInTheDocument();
  });

  it("shows a live 'using a tool' status line while a turn runs", async () => {
    let release: () => void = () => {};
    const gate = new Promise<void>((r) => {
      release = r;
    });
    async function* held(): AsyncGenerator<JcodeEvent> {
      yield { type: "run", run_id: "run-7" };
      yield { type: "tool_use", tool: "Edit", data: { command: "edit src/app.ts" } };
      await gate; // hold the turn open with the Edit tool in flight
      yield { type: "tool_result", tool: "Edit", data: { ok: true } };
      yield { type: "done" };
    }
    vi.spyOn(api, "jcodeTurn").mockImplementation(() => held());
    render(<JcodeSessionScreen session={SESSION} onClose={vi.fn()} />);
    fireEvent.change(screen.getByPlaceholderText(/Tell jcode/i), { target: { value: "go" } });
    fireEvent.click(screen.getByLabelText("Send"));
    // While the Edit tool is in flight, the reused AgentStatusLine names what it's doing.
    expect(await screen.findByText(/Editing/)).toBeInTheDocument();
    release();
  });

  it("shows the model and work-branch in the composer context bar", async () => {
    render(<JcodeSessionScreen session={SESSION} onClose={vi.fn()} />);
    // The context bar names what the agent works against: the resolved model + the
    // sandbox work-branch.
    expect(await screen.findByText("qwen3-coder-next")).toBeInTheDocument();
    expect(screen.getByText("jcode/spike")).toBeInTheDocument();
  });

  it("shows the tool command in the Terminal tab", async () => {
    vi.spyOn(api, "jcodeTurn").mockImplementation(() => turn());
    render(<JcodeSessionScreen session={SESSION} onClose={vi.fn()} />);
    fireEvent.change(screen.getByPlaceholderText(/Tell jcode/i), { target: { value: "go" } });
    fireEvent.click(screen.getByLabelText("Send"));
    await screen.findByText(/Done\./);

    fireEvent.click(screen.getByLabelText("Terminal"));
    expect(screen.getByText(/\$ edit src\/app\.ts/)).toBeInTheDocument();
  });

  it("cancels the detached server turn when you leave mid-stream (B1)", async () => {
    let release: () => void = () => {};
    const gate = new Promise<void>((r) => {
      release = r;
    });
    async function* slow(): AsyncGenerator<JcodeEvent> {
      yield { type: "run", run_id: "run-9" };
      await gate; // hold the turn open until the test releases it
      yield { type: "done" };
    }
    vi.spyOn(api, "jcodeTurn").mockImplementation(() => slow());
    const cancel = vi.spyOn(api, "cancelJcodeRun").mockResolvedValue();
    const onClose = vi.fn();
    render(<JcodeSessionScreen session={SESSION} onClose={onClose} />);

    fireEvent.change(screen.getByPlaceholderText(/Tell jcode/i), { target: { value: "go" } });
    fireEvent.click(screen.getByLabelText("Send"));
    // The run event flows first (Stop replaces Send while busy), capturing the run id.
    await waitFor(() => expect(screen.getByLabelText("Stop")).toBeInTheDocument());

    fireEvent.click(screen.getByLabelText("Back to sessions"));
    expect(onClose).toHaveBeenCalled();
    await waitFor(() => expect(cancel).toHaveBeenCalledWith("run-9"));
    release();
  });

  it("opens a web preview tunnel from the Preview tab", async () => {
    vi.spyOn(api, "jcodePreviewStatus").mockResolvedValue({ enabled: true, url: null });
    vi.spyOn(api, "jcodePreviewOpen").mockResolvedValue({
      enabled: true,
      url: "https://demo-x.trycloudflare.com",
    });
    render(<JcodeSessionScreen session={SESSION} onClose={vi.fn()} />);

    fireEvent.click(screen.getByLabelText("Preview"));
    fireEvent.click(await screen.findByText("Open preview tunnel"));
    expect(await screen.findByText("https://demo-x.trycloudflare.com")).toBeInTheDocument();
  });

  it("shows a disabled state when preview is off", async () => {
    vi.spyOn(api, "jcodePreviewStatus").mockResolvedValue({ enabled: false, url: null });
    render(<JcodeSessionScreen session={SESSION} onClose={vi.fn()} />);
    fireEvent.click(screen.getByLabelText("Preview"));
    expect(await screen.findByText(/isn't enabled/i)).toBeInTheDocument();
  });

  it("arms a tap-again confirm before deleting", async () => {
    const del = vi.spyOn(api, "jcodeDeleteSession").mockResolvedValue();
    const onClose = vi.fn();
    render(<JcodeSessionScreen session={SESSION} onClose={onClose} />);

    fireEvent.click(screen.getByText("Delete"));
    expect(screen.getByText(/Tap again/)).toBeInTheDocument();
    expect(del).not.toHaveBeenCalled();
    fireEvent.click(screen.getByText(/Tap again/));
    await waitFor(() => expect(del).toHaveBeenCalledWith("j1"));
    expect(onClose).toHaveBeenCalled();
  });
});
