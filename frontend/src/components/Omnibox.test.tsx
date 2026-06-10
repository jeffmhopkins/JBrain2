import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { Omnibox } from "./Omnibox";

function setup() {
  const onSend = vi.fn();
  const onConversation = vi.fn();
  render(<Omnibox onSend={onSend} onConversation={onConversation} onOpenLauncher={vi.fn()} />);
  return { onSend, onConversation };
}

describe("Omnibox", () => {
  it("morphs the segment row when active Entry is tapped, and back", () => {
    setup();
    expect(screen.getByRole("tab", { name: "Research" })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("tab", { name: "Entry" }));
    expect(screen.getByRole("tab", { name: "Medical" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Financial" })).toBeInTheDocument();
    expect(screen.queryByRole("tab", { name: "Research" })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("tab", { name: "Entry" }));
    expect(screen.getByRole("tab", { name: "Full Brain" })).toBeInTheDocument();
  });

  it("shows the destination row only for Medical/Financial and sends the domain", () => {
    const { onSend } = setup();
    expect(screen.queryByLabelText("Destination")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("tab", { name: "Entry" }));
    fireEvent.click(screen.getByRole("tab", { name: "Medical" }));
    expect(screen.getByText("notes/medical/")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Destination"), { target: { value: "Labs" } });

    fireEvent.change(screen.getByLabelText("Composer"), { target: { value: "BP 118/76" } });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    expect(onSend).toHaveBeenCalledWith({
      domain: "health",
      destination: "Labs",
      body: "BP 118/76",
      files: [],
    });
  });

  it("hands Research sends to the conversation toast instead of saving", () => {
    const { onSend, onConversation } = setup();
    fireEvent.click(screen.getByRole("tab", { name: "Research" }));
    fireEvent.change(screen.getByLabelText("Composer"), { target: { value: "what did I note?" } });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    expect(onConversation).toHaveBeenCalled();
    expect(onSend).not.toHaveBeenCalled();
  });
});
