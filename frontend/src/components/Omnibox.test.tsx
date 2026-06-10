import { fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";
import type { SegState } from "../notes/modes";
import type { EditingNote } from "../notes/useNoteActions";
import { Omnibox } from "./Omnibox";

interface HarnessProps {
  onSend?: ReturnType<typeof vi.fn>;
  onConversation?: ReturnType<typeof vi.fn>;
  onSubmitEdit?: ReturnType<typeof vi.fn>;
  onCancelEdit?: ReturnType<typeof vi.fn>;
  editing?: EditingNote | null;
}

function Harness({ onSend, onConversation, onSubmitEdit, onCancelEdit, editing }: HarnessProps) {
  const [seg, setSeg] = useState<SegState>({ row: "main", mode: "entry" });
  return (
    <Omnibox
      seg={seg}
      onSegChange={setSeg}
      editing={editing ?? null}
      onCancelEdit={onCancelEdit ?? vi.fn()}
      onSubmitEdit={onSubmitEdit ?? vi.fn()}
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

  it("hands Research sends to the conversation toast instead of saving", () => {
    const onSend = vi.fn();
    const onConversation = vi.fn();
    render(<Harness onSend={onSend} onConversation={onConversation} />);
    fireEvent.click(screen.getByRole("tab", { name: "Research" }));
    fireEvent.change(screen.getByLabelText("Composer"), { target: { value: "what did I note?" } });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    expect(onConversation).toHaveBeenCalled();
    expect(onSend).not.toHaveBeenCalled();
  });

  it("loads the note body in edit mode and sends it as an edit, not a capture", () => {
    const onSend = vi.fn();
    const onSubmitEdit = vi.fn();
    render(
      <Harness
        onSend={onSend}
        onSubmitEdit={onSubmitEdit}
        editing={{ id: "n1", body: "original body" }}
      />,
    );

    expect(screen.getByText("editing note")).toBeInTheDocument();
    const composer = screen.getByLabelText("Composer");
    expect(composer).toHaveValue("original body");

    fireEvent.change(composer, { target: { value: "corrected body" } });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    expect(onSubmitEdit).toHaveBeenCalledWith("corrected body");
    expect(onSend).not.toHaveBeenCalled();
  });

  it("cancel leaves edit mode and clears the loaded body", () => {
    const onCancelEdit = vi.fn();
    render(<Harness onCancelEdit={onCancelEdit} editing={{ id: "n1", body: "original body" }} />);

    fireEvent.click(screen.getByRole("button", { name: "cancel" }));
    expect(onCancelEdit).toHaveBeenCalled();
    expect(screen.getByLabelText("Composer")).toHaveValue("");
  });
});
