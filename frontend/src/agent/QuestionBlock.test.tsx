import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { QuestionBlock } from "./QuestionBlock";
import { askedQuestions } from "./asked";

const QUESTIONS = askedQuestions({
  questions: [
    { id: "q1", question: "What's the medication called?", blocks: "medication.started" },
    {
      id: "q2",
      question: "Which Dr. Chen?",
      blocks: 'resolve_entity("Dr. Chen")',
      candidates: "Dr. Alice Chen (cardiology, 4 notes), Dr. Ray Chen (paediatrics, 2 notes)",
    },
  ],
});

function block(over: { answers?: Record<string, string>; frozen?: boolean } = {}) {
  const onAnswer = vi.fn();
  render(
    <QuestionBlock
      questions={QUESTIONS}
      answers={over.answers ?? {}}
      onAnswer={onAnswer}
      frozen={over.frozen ?? false}
    />,
  );
  return onAnswer;
}

describe("the question block", () => {
  it("puts the question in plain words, with what it blocks", () => {
    block();
    expect(screen.getByText("Which Dr. Chen?")).toBeInTheDocument();
    expect(screen.getByText('blocks · resolve_entity("Dr. Chen")')).toBeInTheDocument();
    expect(screen.getByText("2 questions · answers ride with your next send")).toBeInTheDocument();
  });

  // The candidate context is the point, not an ornament: "Dr. Alice Chen, cardiology, 4
  // notes" against "Dr. Ray Chen, paediatrics, 2 notes" is what makes a one-tap answer
  // possible at all.
  it("renders each candidate with the detail that tells it apart", () => {
    block();
    expect(screen.getByRole("button", { name: /Dr\. Alice Chen/ })).toHaveTextContent(
      "cardiology, 4 notes",
    );
    expect(screen.getByRole("button", { name: /Dr\. Ray Chen/ })).toHaveTextContent(
      "paediatrics, 2 notes",
    );
  });

  it("gives a question with no candidates a field to type in", () => {
    block();
    expect(screen.getByLabelText("What's the medication called?")).toBeInTheDocument();
  });

  it("reports a tap as local state and shows the pick", () => {
    const onAnswer = block();
    fireEvent.click(screen.getByRole("button", { name: /Dr\. Alice Chen/ }));
    expect(onAnswer).toHaveBeenCalledWith("q2", "Dr. Alice Chen");

    block({ answers: { q2: "Dr. Alice Chen" } });
    expect(screen.getAllByRole("button", { name: /Dr\. Alice Chen/ })[1]).toHaveAttribute(
      "aria-pressed",
      "true",
    );
  });

  // A stray tap must not commit anything, so it must be undoable.
  it("unpicks the chosen candidate on a second tap", () => {
    const onAnswer = block({ answers: { q2: "Dr. Ray Chen" } });
    fireEvent.click(screen.getByRole("button", { name: /Dr\. Ray Chen/ }));
    expect(onAnswer).toHaveBeenCalledWith("q2", "");
  });

  it("reports typing the same way", () => {
    const onAnswer = block();
    fireEvent.change(screen.getByLabelText("What's the medication called?"), {
      target: { value: "amlodipine" },
    });
    expect(onAnswer).toHaveBeenCalledWith("q1", "amlodipine");
  });

  it("says out loud that nothing here sends", () => {
    block();
    expect(screen.getByText(/Nothing here sends/)).toBeInTheDocument();
  });

  describe("once it is answered", () => {
    it("goes inert and says what was said", () => {
      block({ frozen: true, answers: { q1: "amlodipine", q2: "Dr. Ray Chen" } });
      expect(screen.getByText("2 questions · answered")).toBeInTheDocument();
      expect(screen.getByRole("button", { name: /Dr\. Ray Chen/ })).toBeDisabled();
      expect(screen.getByText("amlodipine")).toBeInTheDocument();
      expect(screen.queryByText(/Nothing here sends/)).not.toBeInTheDocument();
    });

    // A reply the owner TYPED carries no pairs; the row is honest rather than blank.
    it("puts no words in the owner's mouth when it cannot pair one", () => {
      block({ frozen: true, answers: { q1: "", q2: "" } });
      expect(screen.getByText("answered in your reply")).toBeInTheDocument();
    });
  });
});

// THE PROPERTY THE WHOLE DESIGN RESTS ON (§3b I6). The block is a departure from the
// app's other interactive-in-transcript component — `InlineProposal` posts its own
// outcome back as a follow-up turn — and the reason is arithmetic: if each answer posted,
// three taps would be three turns, three clarification blocks and three re-reads of the
// note, which is exactly the cost the batched ask exists to remove. So: filling the block
// reaches the network not at all.
describe("the block cannot start a turn", () => {
  const fetchSpy = vi.fn();
  beforeEach(() => {
    fetchSpy.mockClear();
    vi.stubGlobal("fetch", fetchSpy);
  });
  afterEach(() => vi.unstubAllGlobals());

  it("makes no request when a candidate is tapped, or a field typed in", () => {
    block();
    fireEvent.click(screen.getByRole("button", { name: /Dr\. Alice Chen/ }));
    fireEvent.click(screen.getByRole("button", { name: /Dr\. Ray Chen/ }));
    fireEvent.change(screen.getByLabelText("What's the medication called?"), {
      target: { value: "amlodipine" },
    });
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it("has no submit of its own — every control inside it is type=button", () => {
    const { container } = render(
      <QuestionBlock questions={QUESTIONS} answers={{}} onAnswer={vi.fn()} frozen={false} />,
    );
    expect(container.querySelector("form")).toBeNull();
    for (const b of container.querySelectorAll("button")) {
      expect(b).toHaveAttribute("type", "button");
    }
  });
});
