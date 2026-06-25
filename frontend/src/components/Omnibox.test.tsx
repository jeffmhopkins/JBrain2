import { fireEvent, render, screen, waitFor } from "@testing-library/react";
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
  it("seeds the composer from a draft handoff and consumes it", () => {
    const onConsumeDraft = vi.fn();
    render(
      <Omnibox
        seg={{ row: "main", mode: "fullbrain" }}
        onSegChange={vi.fn()}
        onSend={vi.fn()}
        onConversation={vi.fn()}
        onOpenLauncher={vi.fn()}
        draft="reschedule my dentist to "
        onConsumeDraft={onConsumeDraft}
      />,
    );
    expect(screen.getByDisplayValue(/reschedule my dentist to/)).toBeInTheDocument();
    expect(onConsumeDraft).toHaveBeenCalled();
  });

  it("shows the appointment pill and clears it on tap", () => {
    const onClearApptRef = vi.fn();
    render(
      <Omnibox
        seg={{ row: "main", mode: "fullbrain" }}
        onSegChange={vi.fn()}
        onSend={vi.fn()}
        onConversation={vi.fn()}
        onOpenLauncher={vi.fn()}
        apptRef={{ id: "A1", title: "Dentist" }}
        onClearApptRef={onClearApptRef}
      />,
    );
    const pill = screen.getByRole("button", { name: "Remove appointment Dentist" });
    expect(pill).toHaveTextContent("Dentist");
    fireEvent.click(pill);
    expect(onClearApptRef).toHaveBeenCalled();
  });

  it("morphs the segment row when active Entry is tapped, and back", () => {
    render(<Harness />);
    expect(screen.getByRole("tab", { name: "Research" })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("tab", { name: "Entry" }));
    expect(screen.getByRole("tab", { name: "Medical" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Financial" })).toBeInTheDocument();
    expect(screen.queryByRole("tab", { name: "Research" })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("tab", { name: "Entry" }));
    expect(screen.getByRole("tab", { name: "Brain" })).toBeInTheDocument();
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

  it("reports a committed horizontal swipe so the home screen can shuttle panels", () => {
    const onLateralSwipe = vi.fn();
    render(
      <Omnibox
        seg={{ row: "main", mode: "fullbrain" }}
        onSegChange={vi.fn()}
        onSend={vi.fn()}
        onConversation={vi.fn()}
        onOpenLauncher={vi.fn()}
        onLateralSwipe={onLateralSwipe}
      />,
    );
    // Gestures initiate from the segment row only, not the whole box.
    const box = document.querySelector(".omnibox .seg-row") as Element;

    // A rightward drag past the commit threshold reports a positive dx.
    fireEvent.touchStart(box, { touches: [{ clientX: 30, clientY: 40 }] });
    fireEvent.touchEnd(box, { changedTouches: [{ clientX: 130, clientY: 44 }] });
    expect(onLateralSwipe).toHaveBeenCalledTimes(1);
    expect(onLateralSwipe.mock.calls[0]?.[0]).toBeGreaterThan(0);

    // A leftward drag reports a negative dx.
    fireEvent.touchStart(box, { touches: [{ clientX: 200, clientY: 40 }] });
    fireEvent.touchEnd(box, { changedTouches: [{ clientX: 90, clientY: 44 }] });
    expect(onLateralSwipe).toHaveBeenCalledTimes(2);
    expect(onLateralSwipe.mock.calls[1]?.[0]).toBeLessThan(0);

    // A short travel (a tap) leaves the panels be.
    fireEvent.touchStart(box, { touches: [{ clientX: 100, clientY: 40 }] });
    fireEvent.touchEnd(box, { changedTouches: [{ clientX: 110, clientY: 41 }] });
    expect(onLateralSwipe).toHaveBeenCalledTimes(2);
  });

  it("hands conversational sends off with the typed body instead of saving", () => {
    const onSend = vi.fn();
    const onConversation = vi.fn();
    render(<Harness onSend={onSend} onConversation={onConversation} />);
    fireEvent.click(screen.getByRole("tab", { name: "Research" }));
    fireEvent.change(screen.getByLabelText("Composer"), { target: { value: "what did I note?" } });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    expect(onConversation).toHaveBeenCalledWith("what did I note?", []);
    expect(onSend).not.toHaveBeenCalled();
  });

  it("hides the chat paperclip when attach is off (no stand-in hint)", () => {
    render(
      <Omnibox
        seg={{ row: "main", mode: "fullbrain" }}
        onSegChange={vi.fn()}
        onSend={vi.fn()}
        onConversation={vi.fn()}
        onOpenLauncher={vi.fn()}
        attachEnabled={false}
      />,
    );
    expect(screen.queryByRole("button", { name: "Attach files" })).not.toBeInTheDocument();
  });

  it("always shows the paperclip for capture modes (note attachments, not chat)", () => {
    render(
      <Omnibox
        seg={{ row: "main", mode: "entry" }}
        onSegChange={vi.fn()}
        onSend={vi.fn()}
        onConversation={vi.fn()}
        onOpenLauncher={vi.fn()}
        // Even with attach off, a capture mode keeps its paperclip — HomeScreen
        // only turns it off for a vision-less conversation mode.
        attachEnabled
      />,
    );
    expect(screen.getByRole("button", { name: "Attach files" })).toBeInTheDocument();
  });

  it("forwards staged files on a conversational send, then clears them", async () => {
    const onConversation = vi.fn(() => Promise.resolve(true));
    render(
      <Omnibox
        seg={{ row: "main", mode: "fullbrain" }}
        onSegChange={vi.fn()}
        onSend={vi.fn()}
        onConversation={onConversation}
        onOpenLauncher={vi.fn()}
      />,
    );
    const file = new File(["hi"], "scan.png", { type: "image/png" });
    const input = document.querySelector('input[type="file"]') as HTMLInputElement;
    fireEvent.change(input, { target: { files: [file] } });
    // The staged chip shows before the send.
    expect(screen.getByRole("button", { name: "Remove scan.png" })).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("Composer"), { target: { value: "read this" } });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));

    expect(onConversation).toHaveBeenCalledWith("read this", [file]);
    // A confirmed send clears the staged chip.
    await waitFor(() =>
      expect(screen.queryByRole("button", { name: "Remove scan.png" })).not.toBeInTheDocument(),
    );
  });

  it("keeps staged files when the conversational send reports a failure", async () => {
    const onConversation = vi.fn(() => Promise.resolve(false));
    render(
      <Omnibox
        seg={{ row: "main", mode: "fullbrain" }}
        onSegChange={vi.fn()}
        onSend={vi.fn()}
        onConversation={onConversation}
        onOpenLauncher={vi.fn()}
      />,
    );
    const file = new File(["hi"], "doc.pdf", { type: "application/pdf" });
    const input = document.querySelector('input[type="file"]') as HTMLInputElement;
    fireEvent.change(input, { target: { files: [file] } });
    fireEvent.change(screen.getByLabelText("Composer"), { target: { value: "read this" } });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));

    // An upload failure keeps BOTH the file staged AND the typed text so the
    // owner can retry without re-typing.
    await waitFor(() => expect(onConversation).toHaveBeenCalled());
    expect(screen.getByRole("button", { name: "Remove doc.pdf" })).toBeInTheDocument();
    await waitFor(() => expect(screen.getByLabelText("Composer")).toHaveValue("read this"));
  });

  it("turns the send button into Stop while a turn streams, and aborts on tap", () => {
    const onStop = vi.fn();
    render(
      <Omnibox
        seg={{ row: "main", mode: "fullbrain" }}
        onSegChange={vi.fn()}
        onSend={vi.fn()}
        onConversation={vi.fn()}
        onOpenLauncher={vi.fn()}
        busy
        onStop={onStop}
      />,
    );
    // While busy the Send button is gone and a Stop button stands in its place.
    expect(screen.queryByRole("button", { name: "Send" })).not.toBeInTheDocument();
    const stop = screen.getByRole("button", { name: "Stop generating" });
    expect(stop).not.toBeDisabled();
    fireEvent.click(stop);
    expect(onStop).toHaveBeenCalledTimes(1);
  });

  it("renders the live context-usage meter from the usage prop", () => {
    render(
      <Omnibox
        seg={{ row: "main", mode: "fullbrain" }}
        onSegChange={vi.fn()}
        onSend={vi.fn()}
        onConversation={vi.fn()}
        onOpenLauncher={vi.fn()}
        contextUsage={{ used: 8192, base: 4096, window: 32768 }}
      />,
    );
    // 8192 / 32768 = 25%; the bar fills to match and the label reads compactly.
    const meter = screen.getByRole("status", {
      name: /Context used: 8192 of 32768 tokens \(25%\) — 4096 carried, 4096 this turn/,
    });
    expect(meter).toHaveTextContent("8.2k/33k · 25%");
    // Two-tone: the peak reaches 25%, the solid carried floor (4096) to 13%.
    expect(meter.querySelector(".ctx-fill-peak")).toHaveStyle({ width: "25%" });
    expect(meter.querySelector(".ctx-fill-base")).toHaveStyle({ width: "13%" });
  });

  it("warms the context meter toward warning as the window fills", () => {
    const { rerender } = render(
      <Omnibox
        seg={{ row: "main", mode: "fullbrain" }}
        onSegChange={vi.fn()}
        onSend={vi.fn()}
        onConversation={vi.fn()}
        onOpenLauncher={vi.fn()}
        contextUsage={{ used: 30000, base: 1000, window: 32768 }}
      />,
    );
    // ~92% → the high/over-budget register.
    expect(document.querySelector(".ctx-meter.ctx-high")).toBeInTheDocument();

    rerender(
      <Omnibox
        seg={{ row: "main", mode: "fullbrain" }}
        onSegChange={vi.fn()}
        onSend={vi.fn()}
        onConversation={vi.fn()}
        onOpenLauncher={vi.fn()}
        contextUsage={{ used: 25000, base: 1000, window: 32768 }}
      />,
    );
    // ~76% → the mid register, not yet high.
    expect(document.querySelector(".ctx-meter.ctx-mid")).toBeInTheDocument();
    expect(document.querySelector(".ctx-meter.ctx-high")).not.toBeInTheDocument();
  });

  it("allows a files-only conversational send (caption optional)", () => {
    const onConversation = vi.fn(() => Promise.resolve(true));
    render(
      <Omnibox
        seg={{ row: "main", mode: "fullbrain" }}
        onSegChange={vi.fn()}
        onSend={vi.fn()}
        onConversation={onConversation}
        onOpenLauncher={vi.fn()}
      />,
    );
    const file = new File(["hi"], "scan.png", { type: "image/png" });
    const input = document.querySelector('input[type="file"]') as HTMLInputElement;
    fireEvent.change(input, { target: { files: [file] } });
    // No caption typed — Send is enabled because a file is staged.
    const sendBtn = screen.getByRole("button", { name: "Send" });
    expect(sendBtn).not.toBeDisabled();
    fireEvent.click(sendBtn);
    expect(onConversation).toHaveBeenCalledWith("", [file]);
  });
});
