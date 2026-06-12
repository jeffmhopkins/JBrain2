import { fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";
import type { SegState } from "../notes/modes";
import { Omnibox } from "./Omnibox";

interface HarnessProps {
  onSend?: ReturnType<typeof vi.fn>;
  onConversation?: ReturnType<typeof vi.fn>;
}

function Harness({ onSend, onConversation }: HarnessProps) {
  const [seg, setSeg] = useState<SegState>({ row: "main", mode: "entry" });
  return (
    <Omnibox
      seg={seg}
      onSegChange={setSeg}
      onSend={onSend ?? vi.fn()}
      onConversation={onConversation ?? vi.fn()}
      onOpenLauncher={vi.fn()}
    />
  );
}

describe("Omnibox", () => {
  it("morphs the segment row when active Entry is tapped, and back", () => {
    render(<Harness />);
    expect(screen.getByRole("tab", { name: "Research" })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("tab", { name: "Entry" }));
    expect(screen.getByRole("tab", { name: "Medical" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Financial" })).toBeInTheDocument();
    expect(screen.queryByRole("tab", { name: "Research" })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("tab", { name: "Entry" }));
    expect(screen.getByRole("tab", { name: "Full Brain" })).toBeInTheDocument();
  });

  it("shows the destination row only for Medical/Financial and sends the domain", () => {
    const onSend = vi.fn();
    render(<Harness onSend={onSend} />);
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

  it("hands conversational sends off with the typed body instead of saving", () => {
    const onSend = vi.fn();
    const onConversation = vi.fn();
    render(<Harness onSend={onSend} onConversation={onConversation} />);
    fireEvent.click(screen.getByRole("tab", { name: "Research" }));
    fireEvent.change(screen.getByLabelText("Composer"), { target: { value: "what did I note?" } });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    expect(onConversation).toHaveBeenCalledWith("what did I note?");
    expect(onSend).not.toHaveBeenCalled();
  });
});
