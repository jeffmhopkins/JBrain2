import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { api } from "../api/client";
import type { JcodeEvent, JcodeSession } from "../jcode/types";
import { JcodeSessionScreen } from "./JcodeSessionScreen";

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
