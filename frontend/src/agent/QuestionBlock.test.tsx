import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { QuestionBlock } from "./QuestionBlock";
import { askedQuestions, ownerTurnText, sentAnswers, sentOutcomes } from "./asked";

/** Ids as the TOOL mints them (`q` + 8 hex), never `q1`/`q2`: those are byte-identical to
 * `askedQuestions`' deploy-window fallback, so a fixture written that way asserts nothing
 * about the ids the block actually renders (R3f's fourth review, finding 8). */
const QUESTIONS = askedQuestions({
  questions: [
    { id: "qf2011e6f", question: "What's the medication called?", blocks: "medication.started" },
    {
      id: "q38035b59",
      question: "Which Dr. Chen?",
      blocks: 'resolve_entity("Dr. Chen")',
      candidates: "Dr. Alice Chen (cardiology, 4 notes), Dr. Ray Chen (paediatrics, 2 notes)",
    },
  ],
});

// The set the review's own walk-through uses: two typed rows around one candidate row,
// which is what makes a PARTIAL send (tap one, leave two) renderable at all.
const THREE = askedQuestions({
  questions: [
    { id: "qf2011e6f", question: "What's the medication called?", blocks: "medication.started" },
    {
      id: "q38035b59",
      question: "Which Dr. Chen?",
      blocks: 'resolve_entity("Dr. Chen")',
      candidates: "Dr. Alice Chen (cardiology, 4 notes), Dr. Ray Chen (paediatrics, 2 notes)",
    },
    { id: "q7c1a904d", question: "What dose?", blocks: "medication.dose" },
  ],
});

const STILL_OPEN = "still open — not answered in your reply";

/** A LIVE block, or — given `reply` — one frozen against that reply turn's own text.
 *
 * Frozen through `sentOutcomes` rather than a hand-built freeze on purpose: what a
 * settled row may claim is derived from the wire, and a test that hands the component a
 * shape the wire cannot produce pins nothing about what the owner sees. */
function block(
  over: {
    answers?: Record<string, string>;
    reply?: string;
    questions?: typeof QUESTIONS;
    readOnly?: boolean;
  } = {},
) {
  const onAnswer = vi.fn();
  const qs = over.questions ?? QUESTIONS;
  render(
    <QuestionBlock
      questions={qs}
      answers={over.reply === undefined ? (over.answers ?? {}) : sentAnswers(qs, over.reply)}
      onAnswer={onAnswer}
      sent={over.reply === undefined ? null : sentOutcomes(qs, over.reply)}
      readOnly={over.readOnly ?? false}
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
    expect(onAnswer).toHaveBeenCalledWith("q38035b59", "Dr. Alice Chen");

    block({ answers: { q38035b59: "Dr. Alice Chen" } });
    expect(screen.getAllByRole("button", { name: /Dr\. Alice Chen/ })[1]).toHaveAttribute(
      "aria-pressed",
      "true",
    );
  });

  // A stray tap must not commit anything, so it must be undoable.
  it("unpicks the chosen candidate on a second tap", () => {
    const onAnswer = block({ answers: { q38035b59: "Dr. Ray Chen" } });
    fireEvent.click(screen.getByRole("button", { name: /Dr\. Ray Chen/ }));
    expect(onAnswer).toHaveBeenCalledWith("q38035b59", "");
  });

  it("reports typing the same way", () => {
    const onAnswer = block();
    fireEvent.change(screen.getByLabelText("What's the medication called?"), {
      target: { value: "amlodipine" },
    });
    expect(onAnswer).toHaveBeenCalledWith("qf2011e6f", "amlodipine");
  });

  // R3f's review, finding 4. A candidate row rendered candidates OR a field, never both,
  // so a candidate the model's prose lost — one unclosed paren is enough — was
  // unanswerable. The escape is the robust fix; hardening the parser is not, because
  // every repair invents candidates the model never wrote.
  describe("the typed escape", () => {
    it("reveals the same field a candidate row otherwise never gets", () => {
      const onAnswer = block();
      expect(screen.queryByLabelText("Which Dr. Chen?")).not.toBeInTheDocument();
      fireEvent.click(screen.getByRole("button", { name: "Something else" }));
      fireEvent.change(screen.getByLabelText("Which Dr. Chen?"), {
        target: { value: "Dr. Ray Chen's locum" },
      });
      expect(onAnswer).toHaveBeenCalledWith("q38035b59", "Dr. Ray Chen's locum");
    });

    it("drops the pick it replaces, and is undoable like every other tap", () => {
      const onAnswer = block({ answers: { q38035b59: "Dr. Alice Chen" } });
      fireEvent.click(screen.getByRole("button", { name: "Something else" }));
      expect(onAnswer).toHaveBeenCalledWith("q38035b59", "");
      fireEvent.click(screen.getByRole("button", { name: "Something else" }));
      expect(screen.queryByLabelText("Which Dr. Chen?")).not.toBeInTheDocument();
    });

    // A draft restored after a failed send has to come back visible, not stranded
    // behind a tap the owner has no reason to make twice.
    it("comes back open on words that are not one of the candidates", () => {
      block({ answers: { q38035b59: "the locum" } });
      expect(screen.getByLabelText("Which Dr. Chen?")).toHaveValue("the locum");
    });

    it("is not offered on a frozen block", () => {
      block({
        reply: ownerTurnText("", QUESTIONS, { qf2011e6f: "amlodipine", q38035b59: "the locum" }),
      });
      expect(screen.queryByRole("button", { name: "Something else" })).not.toBeInTheDocument();
      expect(screen.getByText("the locum")).toBeInTheDocument();
    });
  });

  it("says out loud that nothing here sends", () => {
    block();
    expect(screen.getByText(/Nothing here sends/)).toBeInTheDocument();
  });

  describe("once it is answered", () => {
    it("goes inert and says what was said", () => {
      block({
        reply: ownerTurnText("", QUESTIONS, { qf2011e6f: "amlodipine", q38035b59: "Dr. Ray Chen" }),
      });
      expect(screen.getByText("2 questions · answered")).toBeInTheDocument();
      expect(screen.getByRole("button", { name: /Dr\. Ray Chen/ })).toBeDisabled();
      expect(screen.getByText("amlodipine")).toBeInTheDocument();
      expect(screen.queryByText(/Nothing here sends/)).not.toBeInTheDocument();
    });

    // R3f's SECOND review, finding 1. The block is the one thing on screen reporting the
    // outcome of a send, and on the send §3b I7 actually designs — one candidate tapped,
    // the other rows left blank, an aside typed — it said the rows it had left OPEN were
    // "answered in your reply". The aside reached no note (F5), the questions stayed open,
    // `owner_reply_notice` told the agent so, and the agent's next turn re-asked exactly
    // the rows the block had just called answered.
    it("says a question the send left open is still open, not answered elsewhere", () => {
      block({
        questions: THREE,
        reply: ownerTurnText("also the dinner is cancelled", THREE, { q38035b59: "Dr. Ray Chen" }),
      });
      expect(screen.getByText("3 questions · 1 answered, 2 still open")).toBeInTheDocument();
      expect(screen.getAllByText(STILL_OPEN)).toHaveLength(2);
      expect(screen.queryByText("answered in your reply")).not.toBeInTheDocument();
      expect(screen.getByRole("button", { name: /Dr\. Ray Chen/ })).toHaveAttribute(
        "aria-pressed",
        "true",
      );
    });

    // Prose ALONE has exactly one thing it could be answering, and `clarify._pair` gives
    // it the OLDEST open question. So one row is genuinely answered in the reply — and
    // the rest are not, which is what the whole set used to claim.
    it("names only the one question a prose-only reply actually answered", () => {
      block({ questions: THREE, reply: "it was Alice, and 5mg" });
      expect(screen.getByText("3 questions · 1 answered, 2 still open")).toBeInTheDocument();
      expect(screen.getByText("answered in your reply")).toBeInTheDocument();
      expect(screen.getAllByText(STILL_OPEN)).toHaveLength(2);
    });

    // Every structured answer named a question that is not open (a stale block replayed
    // off a reopened thread): the reply paired nothing, and the header says so.
    it("claims nothing at all when the reply paired nothing", () => {
      block({ questions: THREE, reply: "" });
      expect(screen.getByText("3 questions · still open")).toBeInTheDocument();
      expect(screen.getAllByText(STILL_OPEN)).toHaveLength(3);
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
    // The escape is one more piece of local state, not a second submit.
    fireEvent.click(screen.getByRole("button", { name: "Something else" }));
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it("has no submit of its own — every control inside it is type=button", () => {
    const { container } = render(
      <QuestionBlock questions={QUESTIONS} answers={{}} onAnswer={vi.fn()} sent={null} />,
    );
    expect(container.querySelector("form")).toBeNull();
    for (const b of container.querySelectorAll("button")) {
      expect(b).toHaveAttribute("type", "button");
    }
  });
});

// R3f's fourth review, finding 2 — the deploy window. The questions are real and their ids
// are not: an answer posted against a positional stand-in names no open question, `_pair`
// drops it, and the set is consumed anyway. So the block shows and offers nothing.
describe("a block whose ids the ledger never held", () => {
  it("shows every question and no way to answer it here", () => {
    block({ questions: THREE, readOnly: true });
    expect(screen.getByText("What's the medication called?")).toBeInTheDocument();
    expect(screen.getByText("Which Dr. Chen?")).toBeInTheDocument();
    expect(screen.getByText("What dose?")).toBeInTheDocument();
    // Nothing to tap and nothing to type — not disabled controls, which would invite a tap
    // that cannot work.
    expect(screen.queryAllByRole("button")).toHaveLength(0);
    expect(screen.queryAllByRole("textbox")).toHaveLength(0);
  });

  it("says where the answer goes, and the header does not promise a send", () => {
    block({ questions: THREE, readOnly: true });
    expect(screen.getByText("3 questions · answer in your reply")).toBeInTheDocument();
    expect(screen.getByText(/answer the first question above/)).toBeInTheDocument();
    expect(screen.queryByText(/Nothing here sends/)).not.toBeInTheDocument();
  });

  it("is a LIVE state only — a frozen block reads its outcomes as usual", () => {
    const reply = ownerTurnText("amlodipine", THREE, {});
    block({ questions: THREE, reply, readOnly: true });
    expect(screen.getByText("3 questions · 1 answered, 2 still open")).toBeInTheDocument();
    expect(screen.getAllByText(STILL_OPEN)).toHaveLength(2);
  });
});
