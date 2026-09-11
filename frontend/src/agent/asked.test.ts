import { describe, expect, it } from "vitest";
import {
  answerList,
  answeredCount,
  answersFromReply,
  askedQuestions,
  openQuestions,
  ownerTurnText,
  parseCandidates,
  sentAnswers,
  turnQuestions,
} from "./asked";
import type { ToolActivity, TranscriptMessage } from "./transcript";

function assistant(over: Partial<TranscriptMessage> = {}): TranscriptMessage {
  return {
    role: "assistant",
    text: "I got most of it.",
    tools: [],
    views: [],
    streaming: false,
    reasoning: "",
    thinking: false,
    ...over,
  };
}

function user(text: string): TranscriptMessage {
  return {
    role: "user",
    text,
    tools: [],
    views: [],
    streaming: false,
    reasoning: "",
    thinking: false,
  };
}

const ARGS = {
  questions: [
    { id: "q1", question: "What's the medication called?", blocks: "medication.started" },
    {
      id: "q2",
      question: "Which Dr. Chen?",
      blocks: 'resolve_entity("Dr. Chen")',
      candidates: "Dr. Alice Chen (cardiology, 4 notes), Dr. Ray Chen (paediatrics, 2 notes)",
    },
  ],
};

function askTool(over: Partial<ToolActivity> = {}): ToolActivity {
  return { id: "t1", name: "ask_owner", ok: true, args: ARGS, ...over };
}

describe("parseCandidates", () => {
  // The model writes the list as prose, and the detail's own punctuation is the same
  // character as the separator: a naive split turns two candidates into four and offers
  // "4 notes)" as an answer.
  it("splits on top-level commas only", () => {
    const out = parseCandidates(
      "Dr. Alice Chen (cardiology, 4 notes), Dr. Ray Chen (paediatrics, 2 notes)",
    );
    expect(out.map((c) => c.label)).toEqual(["Dr. Alice Chen", "Dr. Ray Chen"]);
    expect(out.map((c) => c.detail)).toEqual(["cardiology, 4 notes", "paediatrics, 2 notes"]);
  });

  it("answers with the name — the words the owner would have typed", () => {
    expect(parseCandidates("Sam Okonkwo (brother-in-law)")[0]?.value).toBe("Sam Okonkwo");
  });

  // If two candidates share a name, the name is exactly the thing that does not say which
  // was tapped, and a mispaired answer is a wrong sentence in the owner's own note.
  it("falls back to the whole candidate when two share a name", () => {
    const out = parseCandidates("Sam Chen (work, 3 notes), Sam Chen (climbing gym, 1 note)");
    expect(out[0]?.value).toBe("Sam Chen (work, 3 notes)");
    expect(out[1]?.value).toBe("Sam Chen (climbing gym, 1 note)");
  });

  it("takes a bare list and an empty one", () => {
    expect(parseCandidates("Alice, Bob").map((c) => c.value)).toEqual(["Alice", "Bob"]);
    expect(parseCandidates("")).toEqual([]);
  });

  // Measured against real model output (R3f's review, finding 4). The tool asks for
  // commas; the model writes what it writes, and a semicolon-joined list parsed as ONE
  // candidate — so the only tappable thing named both people, and a tap would have put
  // that whole string into the note as the owner's answer.
  it("splits on a top-level semicolon too", () => {
    const out = parseCandidates("Sarah Whitfield (sister); Sarah Chen (work)");
    expect(out.map((c) => c.label)).toEqual(["Sarah Whitfield", "Sarah Chen"]);
    expect(out.map((c) => c.detail)).toEqual(["sister", "work"]);
  });

  // A malformed list is left alone on purpose: every repair available here invents
  // candidates the model never wrote ("4 notes" offered as a person), and a candidate the
  // owner taps becomes a sentence in his own note. The typed escape is what makes the
  // unreachable candidate survivable — see QuestionBlock's "Something else".
  it("does not invent candidates out of an unbalanced list", () => {
    const out = parseCandidates(
      "Dr. Alice Chen (cardiology, 4 notes, Dr. Ray Chen (paediatrics, 2 notes)",
    );
    expect(out).toHaveLength(1);
  });
});

describe("askedQuestions", () => {
  it("reads the set the ask recorded, in order", () => {
    const qs = askedQuestions(ARGS);
    expect(qs.map((q) => q.id)).toEqual(["q1", "q2"]);
    expect(qs[1]?.blocks).toBe('resolve_entity("Dr. Chen")');
    expect(qs[1]?.candidates).toHaveLength(2);
    expect(qs[0]?.candidates).toEqual([]);
  });

  it("numbers a question the model gave no id", () => {
    expect(askedQuestions({ questions: [{ question: "Which Sam?" }] })[0]?.id).toBe("q1");
  });

  it("drops a malformed entry rather than rendering a blank row", () => {
    const qs = askedQuestions({ questions: [{ question: "" }, "nope", { question: "Real?" }] });
    expect(qs.map((q) => q.question)).toEqual(["Real?"]);
  });

  // A thread can be sitting in waiting_on_owner with a pre-batch ledger row the moment
  // this ships; without the fallback that owner sees an empty block.
  it("falls back to a pre-batch single question", () => {
    expect(askedQuestions({ question: "Which Sarah?" })).toEqual([
      { id: "q1", question: "Which Sarah?", blocks: "", candidates: [] },
    ]);
  });

  it("is empty for a call with nothing in it", () => {
    expect(askedQuestions(undefined)).toEqual([]);
    expect(askedQuestions({})).toEqual([]);
  });
});

describe("turnQuestions", () => {
  it("reads the last SUCCEEDED ask of the turn", () => {
    const m = assistant({
      tools: [
        askTool({ id: "a", args: { questions: [{ id: "old", question: "Stale?" }] } }),
        askTool({ id: "b" }),
      ],
    });
    expect(turnQuestions(m).map((q) => q.id)).toEqual(["q1", "q2"]);
  });

  it("ignores a failed ask — it asked nothing, and the reply pairs against the open set", () => {
    expect(turnQuestions(assistant({ tools: [askTool({ ok: false })] }))).toEqual([]);
  });

  it("is empty on an ordinary turn", () => {
    expect(turnQuestions(assistant({ tools: [{ id: "t", name: "search", ok: true }] }))).toEqual(
      [],
    );
  });
});

describe("openQuestions", () => {
  const asked = assistant({ tools: [askTool()] });

  it("is the last turn's set while the owner has not replied", () => {
    expect(openQuestions([user("the note"), asked])).toHaveLength(2);
  });

  // A persisted turn replays no stop reason, so "is it last" is the test — and it holds
  // live and on reopen alike.
  it("closes the moment a reply lands after it", () => {
    expect(openQuestions([user("the note"), asked, user("Dr. Alice Chen")])).toEqual([]);
  });

  it("freezes the older block and arms the newer when a thread asks twice", () => {
    const again = assistant({
      tools: [askTool({ args: { questions: [{ id: "q9", question: "And the dose?" }] } })],
    });
    const thread = [user("the note"), asked, user("Dr. Alice Chen"), again];
    expect(openQuestions(thread).map((q) => q.id)).toEqual(["q9"]);
  });

  it("waits for the turn to settle", () => {
    expect(openQuestions([assistant({ tools: [askTool()], streaming: true })])).toEqual([]);
  });
});

describe("the draft and what rides the send", () => {
  const qs = askedQuestions(ARGS);

  it("counts only answers with something in them", () => {
    expect(answeredCount(qs, {})).toBe(0);
    expect(answeredCount(qs, { q1: "  ", q2: "Dr. Alice Chen" })).toBe(1);
  });

  it("sends structured pairs, in the order asked, narrowed to the open set", () => {
    expect(answerList(qs, { q2: "Dr. Alice Chen", q1: "amlodipine", stale: "gone" })).toEqual([
      { question_id: "q1", answer: "amlodipine" },
      { question_id: "q2", answer: "Dr. Alice Chen" },
    ]);
  });

  it("sends nothing from an untouched block — an empty list behaves exactly as before", () => {
    expect(answerList(qs, {})).toEqual([]);
  });
});

describe("ownerTurnText", () => {
  const qs = askedQuestions(ARGS);

  // The mirror of clarify.owner_turn_text, so the optimistic bubble reads exactly as the
  // persisted turn does on reload. The Q:/A: labels are a safety boundary, not styling:
  // only the A: half is the owner's, and the Q: half is a string a model wrote while
  // reading a note body that may carry someone else's text.
  it("renders an answers-only send as labelled pairs", () => {
    expect(ownerTurnText("", qs, { q1: "amlodipine", q2: "Dr. Alice Chen" })).toBe(
      "Q: What's the medication called?\nA: amlodipine\n\nQ: Which Dr. Chen?\nA: Dr. Alice Chen",
    );
  });

  it("lets typed text stand alone when the block answered nothing", () => {
    expect(ownerTurnText("it was Alice", qs, {})).toBe("it was Alice");
  });

  // R3f's review, finding 1. Typed text used to win OUTRIGHT and throw the pairs away,
  // so the designed mixed send persisted as the sentence alone — and the frozen block,
  // which reads its answers back out of this very text, then drew "2 questions ·
  // answered" with neither answer shown and the tapped candidate not picked.
  it("carries BOTH halves of a mixed send, pairs first", () => {
    expect(ownerTurnText("also the dinner is cancelled", qs, { q2: "Dr. Ray Chen" })).toBe(
      "Q: Which Dr. Chen?\nA: Dr. Ray Chen\n\nalso the dinner is cancelled",
    );
  });
});

describe("a frozen block's answers", () => {
  const qs = askedQuestions(ARGS);

  it("pairs back out of the reply's own text, by question and never by position", () => {
    const reply = ownerTurnText("", qs, { q1: "amlodipine", q2: "Dr. Ray Chen" });
    expect(answersFromReply(reply)).toContainEqual({
      question: "Which Dr. Chen?",
      answer: "Dr. Ray Chen",
    });
    expect(sentAnswers(qs, reply)).toEqual({ q1: "amlodipine", q2: "Dr. Ray Chen" });
  });

  // The other half of finding 1: a mixed send's typed words are not a Q/A pair, so they
  // are not read back as an answer — and the pairs beside them still are.
  it("reads a mixed send's pairs back and leaves its typed words out of them", () => {
    const reply = ownerTurnText("also the dinner is cancelled", qs, { q2: "Dr. Ray Chen" });
    expect(sentAnswers(qs, reply)).toEqual({ q1: "", q2: "Dr. Ray Chen" });
  });

  // R3f's review, finding 6. Keyed by question STRING, two rows worded the same both
  // replayed the second answer; `ask_owner` does not dedupe its set, and the plan asserts
  // the pairing as a property rather than a rendering convenience.
  it("gives two identically worded questions their own answers", () => {
    const twins = askedQuestions({
      questions: [
        { id: "q1", question: "Which Sam?" },
        { id: "q2", question: "Which Sam?" },
      ],
    });
    const reply = ownerTurnText("", twins, { q1: "Sam Okonkwo", q2: "Sam Reyes" });
    expect(sentAnswers(twins, reply)).toEqual({ q1: "Sam Okonkwo", q2: "Sam Reyes" });
  });

  it("leaves a partially answered set honest about which rows have words", () => {
    expect(sentAnswers(qs, ownerTurnText("", qs, { q2: "Dr. Ray Chen" }))).toEqual({
      q1: "",
      q2: "Dr. Ray Chen",
    });
  });

  it("puts no words in the owner's mouth when the reply was free prose", () => {
    expect(sentAnswers(qs, "it was Alice, and 5mg")).toEqual({ q1: "", q2: "" });
  });
});
