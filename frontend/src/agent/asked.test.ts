import { describe, expect, it } from "vitest";
import {
  answerList,
  answeredCount,
  answersFromReply,
  askStep,
  askedQuestions,
  openQuestions,
  ownerTurnText,
  parseCandidates,
  sentAnswers,
  sentOutcomes,
  stripPairLabels,
} from "./asked";
import corpus from "./asked.corpus.json";
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
    { id: "qf2011e6f", question: "What's the medication called?", blocks: "medication.started" },
    {
      id: "q38035b59",
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
    expect(qs.map((q) => q.id)).toEqual(["qf2011e6f", "q38035b59"]);
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

describe("askStep", () => {
  it("reads the last RECORDED ask of the turn", () => {
    const m = assistant({
      tools: [
        askTool({ id: "a", args: { questions: [{ id: "q0ldb10c5", question: "Stale?" }] } }),
        askTool({ id: "b" }),
      ],
    });
    expect(askStep(m).questions.map((q) => q.id)).toEqual(["qf2011e6f", "q38035b59"]);
    expect(askStep(m).answerable).toBe(true);
  });

  it("ignores a failed ask — it asked nothing, and the reply pairs against the open set", () => {
    expect(askStep(assistant({ tools: [askTool({ ok: false })] })).questions).toEqual([]);
  });

  it("is empty on an ordinary turn", () => {
    expect(
      askStep(assistant({ tools: [{ id: "t", name: "search", ok: true }] })).questions,
    ).toEqual([]);
  });

  // R3f's fourth review, finding 1. The loop finishes the round it is in before it honours
  // a halt, so a model that emits two `ask_owner` calls in one message runs the second into
  // the already-waiting latch — a refusal, `ok: true` like every other returned string,
  // carrying the model's RAW second question and no minted id. Last-succeeded-wins put that
  // question on screen and hid the two the ledger actually held; a tap then posted `q1`,
  // `_pair` dropped it, and the note received nothing while the block drew it as sent.
  it("skips a refusal that kept the model's raw arguments, however late it ran", () => {
    const refusal = askTool({
      id: "b",
      args: { questions: [{ question: "Totally different question…" }] },
      summary: "This note is already waiting on Jeff for 2 questions…",
    });
    const m = assistant({ tools: [askTool({ id: "a" }), refusal] });
    expect(askStep(m).questions.map((q) => q.question)).toEqual([
      "What's the medication called?",
      "Which Dr. Chen?",
    ]);
  });

  // The same latch once it echoes the OPEN SET (`asktools._already_waiting`): the step is
  // then a record like any other and the block is right whichever call it is built from.
  it("takes the refusal when it echoes the set the ledger holds", () => {
    const m = assistant({
      tools: [
        askTool({ id: "a" }),
        askTool({ id: "b", args: { questions: [{ id: "qf2011e6f", question: "Which Sarah?" }] } }),
      ],
    });
    expect(askStep(m).questions.map((q) => q.question)).toEqual(["Which Sarah?"]);
    expect(askStep(m).answerable).toBe(true);
  });

  // Finding 3. An ask on a `settled`/`failed` thread rolls its ledger row back and returns
  // text; the tool echoes an EMPTY record, so there is no block to draw on a thread the
  // server is not waiting on. Without the record — a pre-echo refusal — the questions would
  // still render, but read-only, which is the other half of this contract.
  it("draws nothing for a refusal that recorded nothing", () => {
    const m = assistant({
      tools: [askTool({ id: "a", args: { questions: [] }, summary: "could not record" })],
    });
    expect(askStep(m).questions).toEqual([]);
  });

  // Finding 2. Every ask persisted before the id echo shipped has the model's raw args and
  // no ids, while its ledger row holds the real ones. Detected — not silently answered with
  // positional stand-ins `_pair` would drop.
  it("flags a step that predates the id echo instead of trusting its positions", () => {
    const m = assistant({
      tools: [askTool({ args: { questions: [{ question: "Which Sarah?" }] } })],
    });
    expect(askStep(m).questions.map((q) => q.id)).toEqual(["q1"]);
    expect(askStep(m).answerable).toBe(false);
  });

  it("trusts no blob where only some rows carry an id", () => {
    const m = assistant({
      tools: [
        askTool({
          args: {
            questions: [{ id: "qf2011e6f", question: "Which Sarah?" }, { question: "Dose?" }],
          },
        }),
      ],
    });
    expect(askStep(m).answerable).toBe(false);
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
      tools: [askTool({ args: { questions: [{ id: "q0d4c8a17", question: "And the dose?" }] } })],
    });
    const thread = [user("the note"), asked, user("Dr. Alice Chen"), again];
    expect(openQuestions(thread).map((q) => q.id)).toEqual(["q0d4c8a17"]);
  });

  it("waits for the turn to settle", () => {
    expect(openQuestions([assistant({ tools: [askTool()], streaming: true })])).toEqual([]);
  });

  // The deploy window, from the SEND's side (R3f's fourth review, finding 2): the block
  // still renders those questions — read-only — but nothing rides a send for them. Posting
  // positional ids is the drop `clarify._pair` makes silently, and the turn text then
  // degrades to bare prose the frozen block reads back as "answered in your reply".
  it("carries no answers for a step that predates the id echo", () => {
    const legacy = assistant({
      tools: [askTool({ args: { questions: [{ question: "Which Sarah?" }] } })],
    });
    expect(openQuestions([user("the note"), legacy])).toEqual([]);
  });
});

describe("stripPairLabels", () => {
  // R3f's fourth review, finding 7 — the two inputs of twenty-three on which the PWA's
  // sanitiser and the backend's diverged, because `/m` counts a lone CR and U+2028 as line
  // starts and `re.MULTILINE` does not. The same strings, and the same expected output, are
  // asserted in `test_the_two_sanitisers_agree_on_the_inputs_that_diverged`.
  it("cuts a label only at a NEWLINE, as the backend does", () => {
    expect(stripPairLabels("Q: a\nA: b\rA: c")).toBe("a\nb\rA: c");
    expect(stripPairLabels("Q: a\nA: b\u2028A: c")).toBe("a\nb\u2028A: c");
  });

  it("still cuts the pair it is for, and leaves a chunk that is not one", () => {
    expect(stripPairLabels("Q: a\nA: b")).toBe("a\nb");
    expect(stripPairLabels("Two options:\nA: the cardiologist\nB: the paediatrician")).toBe(
      "Two options:\nA: the cardiologist\nB: the paediatrician",
    );
  });

  // R3f's fifth review, finding 1 — the gate the two above could not be. The regexes were
  // byte-identical and the sanitisers still disagreed, because the difference lived in the
  // TRIM in front of the pattern: `String.trim()` cuts U+FEFF, `str.strip()` cuts U+0085
  // and U+001C-U+001F, neither cuts the other's. So a BOM-prefixed pair passed this gate,
  // failed the backend's, and reached the persisted turn and the NOTE verbatim — where
  // `answersFromReply`, trimming the BOM, read it straight back as that row's answer.
  //
  // `asked.corpus.json` is one file both suites run: this asserts the PWA maps every `in`
  // to its `out`, `test_both_sanitisers_agree_over_the_whitespace_corpus` asserts Python
  // maps the same `in` to the same `out`. Running both is the whole point — a gate that
  // compares pattern text cannot see a difference that lives around the pattern.
  describe("the shared whitespace corpus", () => {
    it("maps every case exactly as the backend does", () => {
      // Vacuous if the corpus were emptied: the union is 30 characters, wrapped four ways.
      expect(corpus.cases.length).toBeGreaterThan(120);
      for (const c of corpus.cases) {
        expect({ in: c.in, out: stripPairLabels(c.in) }).toEqual({ in: c.in, out: c.out });
      }
    });

    // The property the corpus is FOR, on the side that owns the reader: whatever either
    // sanitiser leaves behind, nothing in it reads back as a pair. That is what makes the
    // sanitiser's definition ("neutralise what the reader accepts") true by measurement
    // rather than by inspection of two regexes.
    it("leaves nothing the reader will take as an answer", () => {
      for (const c of corpus.cases) {
        expect({ in: c.in, pairs: answersFromReply(c.out) }).toEqual({ in: c.in, pairs: [] });
      }
    });
  });
});

describe("the draft and what rides the send", () => {
  const qs = askedQuestions(ARGS);

  it("counts only answers with something in them", () => {
    expect(answeredCount(qs, {})).toBe(0);
    expect(answeredCount(qs, { qf2011e6f: "  ", q38035b59: "Dr. Alice Chen" })).toBe(1);
  });

  it("sends structured pairs, in the order asked, narrowed to the open set", () => {
    expect(
      answerList(qs, { q38035b59: "Dr. Alice Chen", qf2011e6f: "amlodipine", stale: "gone" }),
    ).toEqual([
      { question_id: "qf2011e6f", answer: "amlodipine" },
      { question_id: "q38035b59", answer: "Dr. Alice Chen" },
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
    expect(ownerTurnText("", qs, { qf2011e6f: "amlodipine", q38035b59: "Dr. Alice Chen" })).toBe(
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
    expect(ownerTurnText("also the dinner is cancelled", qs, { q38035b59: "Dr. Ray Chen" })).toBe(
      "Q: Which Dr. Chen?\nA: Dr. Ray Chen\n\nalso the dinner is cancelled",
    );
  });
});

describe("a frozen block's answers", () => {
  const qs = askedQuestions(ARGS);

  it("pairs back out of the reply's own text, by question and never by position", () => {
    const reply = ownerTurnText("", qs, { qf2011e6f: "amlodipine", q38035b59: "Dr. Ray Chen" });
    expect(answersFromReply(reply)).toContainEqual({
      question: "Which Dr. Chen?",
      answer: "Dr. Ray Chen",
    });
    expect(sentAnswers(qs, reply)).toEqual({ qf2011e6f: "amlodipine", q38035b59: "Dr. Ray Chen" });
  });

  // The other half of finding 1: a mixed send's typed words are not a Q/A pair, so they
  // are not read back as an answer — and the pairs beside them still are.
  it("reads a mixed send's pairs back and leaves its typed words out of them", () => {
    const reply = ownerTurnText("also the dinner is cancelled", qs, { q38035b59: "Dr. Ray Chen" });
    expect(sentAnswers(qs, reply)).toEqual({ qf2011e6f: "", q38035b59: "Dr. Ray Chen" });
  });

  // R3f's review, finding 6. Keyed by question STRING, two rows worded the same both
  // replayed the second answer; `ask_owner` does not dedupe its set, and the plan asserts
  // the pairing as a property rather than a rendering convenience.
  it("gives two identically worded questions their own answers", () => {
    const twins = askedQuestions({
      questions: [
        { id: "qf2011e6f", question: "Which Sam?" },
        { id: "q38035b59", question: "Which Sam?" },
      ],
    });
    const reply = ownerTurnText("", twins, { qf2011e6f: "Sam Okonkwo", q38035b59: "Sam Reyes" });
    expect(sentAnswers(twins, reply)).toEqual({ qf2011e6f: "Sam Okonkwo", q38035b59: "Sam Reyes" });
  });

  it("leaves a partially answered set honest about which rows have words", () => {
    expect(sentAnswers(qs, ownerTurnText("", qs, { q38035b59: "Dr. Ray Chen" }))).toEqual({
      qf2011e6f: "",
      q38035b59: "Dr. Ray Chen",
    });
  });

  it("puts no words in the owner's mouth when the reply was free prose", () => {
    expect(sentAnswers(qs, "it was Alice, and 5mg")).toEqual({ qf2011e6f: "", q38035b59: "" });
  });
});

// R3f's SECOND review, finding 1. `sentAnswers` returns "" both for "the reply carried no
// words for this row" and for "the reply did not answer this row at all", and the block
// read that "" as *answered in your reply* — telling the owner, live and on every reopen,
// that questions it had left OPEN were answered somewhere. The agent was told the truth
// (`owner_reply_notice`) and re-asked them on its next turn.
describe("what a settled reply DID to each question", () => {
  const qs = askedQuestions(ARGS);

  it("marks an unpaired row OPEN when the reply carried pairs beside its prose", () => {
    const reply = ownerTurnText("also the dinner is cancelled", qs, { q38035b59: "Dr. Ray Chen" });
    expect(sentOutcomes(qs, reply)).toEqual({
      qf2011e6f: { kind: "open" },
      q38035b59: { kind: "paired", answer: "Dr. Ray Chen" },
    });
  });

  // Prose ALONE is the one reply where "answered in your reply" is true, and it is true
  // of exactly one row: `clarify._pair` gives free text the OLDEST open question.
  it("names the one row a prose-only reply answered, and opens the rest", () => {
    expect(sentOutcomes(qs, "it was Alice, and 5mg")).toEqual({
      qf2011e6f: { kind: "in-reply" },
      q38035b59: { kind: "open" },
    });
  });

  it("claims nothing for a reply that is not there at all", () => {
    expect(sentOutcomes(qs, "")).toEqual({
      qf2011e6f: { kind: "open" },
      q38035b59: { kind: "open" },
    });
  });

  it("reads a fully answered set as fully answered", () => {
    const reply = ownerTurnText("", qs, { qf2011e6f: "amlodipine", q38035b59: "Dr. Ray Chen" });
    expect(sentOutcomes(qs, reply)).toEqual({
      qf2011e6f: { kind: "paired", answer: "amlodipine" },
      q38035b59: { kind: "paired", answer: "Dr. Ray Chen" },
    });
  });
});

// R3f's second review, finding 3(b). "The typed half carries no labels" was an assumption
// about what the owner types, not a property of the code: the composer is a bare
// `<textarea>`, Enter inserts a newline, and the questions sit on screen directly above
// it. Quoting one back forged a pair — the block showed a question answered in words the
// backend had dropped and told the agent were still open.
describe("the typed half cannot forge a Q/A pair", () => {
  const qs = askedQuestions(ARGS);

  it("strips the labels a quoted question would carry, beside a tapped answer", () => {
    const reply = ownerTurnText("Q: Which Dr. Chen?\nA: nobody at all", qs, {
      qf2011e6f: "amlodipine",
    });
    expect(reply).toBe(
      "Q: What's the medication called?\nA: amlodipine\n\nWhich Dr. Chen?\nnobody at all",
    );
    expect(sentOutcomes(qs, reply)).toEqual({
      qf2011e6f: { kind: "paired", answer: "amlodipine" },
      q38035b59: { kind: "open" },
    });
  });

  // The prose-only send is the same hole from the other side: there the typed words ARE
  // the whole turn text, so nothing else has to go wrong for the forgery to be read back.
  it("strips them on a prose-only send too", () => {
    const reply = ownerTurnText("Q: Which Dr. Chen?\nA: nobody at all", qs, {});
    expect(reply).toBe("Which Dr. Chen?\nnobody at all");
    expect(answersFromReply(reply)).toEqual([]);
  });

  // ⟲ R3f's third review, finding 3(b). Stripping every labelled LINE deleted words that
  // were the owner's — `A: the cardiologist` under `Two options:` lost its label while the
  // `B:` below it kept one, an enumerated reply mangled into nonsense. The cut is made
  // only on a chunk `answersFromReply` would actually read back as a pair, so what comes
  // off is always the channel's own label.
  it("leaves every word the owner wrote, and ordinary prose untouched", () => {
    expect(ownerTurnText("also the dinner is cancelled", qs, {})).toBe(
      "also the dinner is cancelled",
    );
    const enumerated = "Two options:\nA: the cardiologist\nB: the paediatrician";
    expect(ownerTurnText(enumerated, qs, {})).toBe(enumerated);
    expect(ownerTurnText("  A: 5mg, twice", qs, {})).toBe("  A: 5mg, twice");
    // A `Q:` with no `A:` under it is not a pair, and neither is one split by the blank
    // line that ends a chunk — the reader would find neither, so nothing is neutralised.
    expect(ownerTurnText("Q: which one?", qs, {})).toBe("Q: which one?");
    expect(ownerTurnText("Q: which one?\n\nA: that one", qs, {})).toBe(
      "Q: which one?\n\nA: that one",
    );
    for (const text of [enumerated, "  A: 5mg, twice", "Q: which one?\n\nA: that one"]) {
      expect(answersFromReply(ownerTurnText(text, qs, {}))).toEqual([]);
    }
  });

  it("still breaks a real pair wherever in the message it sits", () => {
    const reply = ownerTurnText("an aside\n\nQ: Which Dr. Chen?\nA: nobody", qs, {});
    expect(reply).toBe("an aside\n\nWhich Dr. Chen?\nnobody");
    expect(answersFromReply(reply)).toEqual([]);
  });
});
