import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { type ClarificationOut, api } from "../api/client";
import { Clarifications } from "./Clarifications";

vi.mock("../api/client", () => ({
  api: {
    listClarifications: vi.fn(),
    deleteClarification: vi.fn(),
  },
}));
const listClarifications = vi.mocked(api.listClarifications);
const deleteClarification = vi.mocked(api.deleteClarification);

afterEach(() => vi.clearAllMocks());

const block = (over: Partial<ClarificationOut> = {}): ClarificationOut => ({
  id: "b1",
  seq: 1,
  kind: "answer",
  question: "Which Sarah?",
  answer: "my sister — the door code is 4417",
  created_at: "2026-09-01T10:00:00Z",
  ...over,
});

describe("the D6 eraser", () => {
  it("erases one block and hands back the note as it now reads", async () => {
    // W2's obligation, in its own words: "the wave that ships the writer ships the
    // eraser", because on a box with no terminal an unredactable field is not a limit
    // the owner can work around. W3 shipped the API half only — the routes exist and
    // are tested, and nothing in the PWA could issue the DELETE — so a password typed
    // into an answer was removable only by deleting the whole note, body and graph
    // with it.
    listClarifications.mockResolvedValue([block()]);
    deleteClarification.mockResolvedValue({ body: "Ran the 10k." } as never);
    const onErased = vi.fn();
    render(<Clarifications noteId="n1" onErased={onErased} />);

    fireEvent.click(await screen.findByRole("button", { name: /What you've added/ }));
    expect(screen.getByText("my sister — the door code is 4417")).toBeInTheDocument();

    // Destructive, so it takes the app's tap-again confirm — and the armed label spells
    // out the consequence, because unlike a note delete this one is not even soft.
    fireEvent.click(screen.getByRole("button", { name: "erase" }));
    expect(deleteClarification).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: /tap again/ }));

    await waitFor(() => expect(deleteClarification).toHaveBeenCalledWith("n1", "b1"));
    // Gone from the panel, and the body above updates without a second fetch.
    await waitFor(() => expect(screen.queryByText("my sister — the door code is 4417")).toBeNull());
    expect(onErased).toHaveBeenCalledWith("Ran the 10k.");
  });

  it("labels an unprompted addition instead of printing a null question", async () => {
    // Backend 0203: an `addition` has no question, because nobody asked. A row that
    // rendered `block.question` unconditionally would print "null" into a list of the
    // owner's own sentences — and, worse, an addition would read as the answer to
    // whatever question sits above it.
    listClarifications.mockResolvedValue([
      block(),
      block({
        id: "b2",
        seq: 2,
        kind: "addition",
        question: null,
        answer: "actually the dentist is Dr. Ashcote",
      }),
    ]);
    render(<Clarifications noteId="n1" onErased={vi.fn()} />);

    fireEvent.click(await screen.findByRole("button", { name: /What you've added/ }));
    expect(screen.getByText("actually the dentist is Dr. Ashcote")).toBeInTheDocument();
    expect(screen.getByText("you added")).toBeInTheDocument();
    expect(screen.queryByText("null")).toBeNull();
    // Both shapes are erasable — an addition is the owner's own words on the note just
    // as an answer is, and a secret can be typed into either.
    expect(screen.getAllByRole("button", { name: "erase" })).toHaveLength(2);
  });

  it("renders nothing for a note that was never asked about", async () => {
    // Which is nearly every note. D6 says the note screen does not change, so a panel
    // that appeared on every note would be the change it rules out.
    listClarifications.mockResolvedValue([]);
    const { container } = render(<Clarifications noteId="n1" onErased={vi.fn()} />);
    await waitFor(() => expect(listClarifications).toHaveBeenCalled());
    expect(container.firstChild).toBeNull();
  });

  it("stays silent when the listing fails, rather than showing an error on every note", async () => {
    listClarifications.mockRejectedValue(new Error("offline"));
    const { container } = render(<Clarifications noteId="n1" onErased={vi.fn()} />);
    await waitFor(() => expect(listClarifications).toHaveBeenCalled());
    expect(container.firstChild).toBeNull();
  });

  it("asks for nothing on an unsynced note", () => {
    render(<Clarifications noteId={null} onErased={vi.fn()} />);
    expect(listClarifications).not.toHaveBeenCalled();
  });
});
