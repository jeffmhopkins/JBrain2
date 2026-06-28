import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { api } from "../api/client";
import type { ExternalSession } from "../jcode/types";
import { ExternalSessionScreen } from "./ExternalSessionScreen";

const SESSION: ExternalSession = {
  id: "ext-1",
  label: "Remote box",
  enabled: true,
  created_at: "2026-06-27T00:00:00Z",
  expires_at: null,
  last_used_at: null,
  in_tokens: 1200,
  out_tokens: 3400,
  requests: 9,
};

describe("ExternalSessionScreen", () => {
  it("shows usage stats and the endpoint URL", () => {
    render(
      <ExternalSessionScreen
        session={SESSION}
        url="https://box.example/api/ext/llm/ext-1"
        onClose={vi.fn()}
        onChanged={vi.fn()}
      />,
    );
    expect(screen.getByText("1,200")).toBeInTheDocument();
    expect(screen.getByText("3,400")).toBeInTheDocument();
    expect(screen.getByText("9")).toBeInTheDocument();
    expect(screen.getByDisplayValue("https://box.example/api/ext/llm/ext-1")).toBeInTheDocument();
  });

  it("offers Claude (Anthropic) and grok (OpenAI) wire-up recipes", () => {
    render(
      <ExternalSessionScreen
        session={SESSION}
        secret="sk-secret"
        url="https://box.example/api/ext/llm/ext-1"
        onClose={vi.fn()}
        onChanged={vi.fn()}
      />,
    );
    expect(screen.getByText("Spin up Claude")).toBeInTheDocument();
    expect(screen.getByText("grok build (OpenAI)")).toBeInTheDocument();
    // The Anthropic recipe uses the bare base URL; the OpenAI one appends /v1.
    expect(
      screen.getByText(/ANTHROPIC_BASE_URL=https:\/\/box\.example\/api\/ext\/llm\/ext-1/),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/OPENAI_BASE_URL=https:\/\/box\.example\/api\/ext\/llm\/ext-1\/v1/),
    ).toBeInTheDocument();
    // The Windows recipe is a one-liner that pulls the hosted setup script from the
    // same host as the endpoint (no token in the URL — the script prompts for it).
    expect(
      screen.getByText("irm https://box.example/api/install/grok.ps1 | iex"),
    ).toBeInTheDocument();
    // The freshly-minted secret is baked into the copyable command.
    expect(screen.getAllByText(/sk-secret/).length).toBeGreaterThan(0);
  });

  it("shows the secret only when freshly minted", () => {
    const { rerender } = render(
      <ExternalSessionScreen session={SESSION} url="u" onClose={vi.fn()} onChanged={vi.fn()} />,
    );
    expect(screen.queryByLabelText("Access token")).not.toBeInTheDocument();
    rerender(
      <ExternalSessionScreen
        session={SESSION}
        secret="sk-secret"
        url="u"
        onClose={vi.fn()}
        onChanged={vi.fn()}
      />,
    );
    expect(screen.getByDisplayValue("sk-secret")).toBeInTheDocument();
  });

  it("flips the on/off toggle through the api", async () => {
    const setEnabled = vi.spyOn(api, "externalSetEnabled").mockResolvedValue();
    const onChanged = vi.fn();
    render(
      <ExternalSessionScreen session={SESSION} url="u" onClose={vi.fn()} onChanged={onChanged} />,
    );
    fireEvent.click(screen.getByRole("switch"));
    await waitFor(() => expect(setEnabled).toHaveBeenCalledWith("ext-1", false));
    expect(onChanged).toHaveBeenCalled();
  });

  it("deletes only after a confirm tap", async () => {
    const revoke = vi.spyOn(api, "externalRevoke").mockResolvedValue();
    const onClose = vi.fn();
    render(
      <ExternalSessionScreen session={SESSION} url="u" onClose={onClose} onChanged={vi.fn()} />,
    );
    const del = screen.getByText("Delete endpoint");
    fireEvent.click(del); // arms confirm, doesn't delete
    expect(revoke).not.toHaveBeenCalled();
    fireEvent.click(screen.getByText(/Tap again/));
    await waitFor(() => expect(revoke).toHaveBeenCalledWith("ext-1"));
    expect(onClose).toHaveBeenCalled();
  });
});
