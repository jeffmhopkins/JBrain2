import { describe, expect, it } from "vitest";
import { chunkStream, engineForVoice, reportToSpeech, speakable } from "./speakable.js";

describe("reportToSpeech", () => {
  const REPORT = [
    "**Bottom-line:** the day's news.",
    "",
    "### Good Morning",
    "Good morning, it's Sunday. Here's what's coming up.",
    "",
    "### Top National Stories",
    "The FAA proposed a new rule.",
    "",
    "---",
    "",
    "## Sources",
    "[^1]: Florida Space Report – https://spacereport.blogspot.com/",
  ].join("\n");

  it("drops heading lines so the greeting isn't doubled, and cuts the Sources section", () => {
    const out = reportToSpeech(REPORT);
    // Heading LINES gone (not just the '#'): no bare "Good Morning" / "Top National Stories".
    expect(out).not.toMatch(/^\s*#/m);
    expect(out).not.toMatch(/Good Morning\n/); // the header text, on its own line, is gone
    // The prose (including the spoken "Good morning" greeting) stays.
    expect(out).toContain("Good morning, it's Sunday.");
    expect(out).toContain("The FAA proposed a new rule.");
    // The Sources section (heading + URLs) is cut entirely — TTS never reads the URL.
    expect(out).not.toContain("spacereport.blogspot.com");
    expect(out).not.toMatch(/Sources/);
  });

  it("read aloud, the greeting is spoken once", () => {
    const spoken = speakable(reportToSpeech(REPORT));
    // "good morning" appears once (the greeting), not twice (header + greeting).
    expect(spoken.toLowerCase().match(/good morning/g)?.length).toBe(1);
    expect(spoken).not.toContain("blogspot");
  });
});

/** Feed `full` to chunkStream in `steps` growing prefixes (simulating streaming), advancing
 * a raw cursor, then flush — returning every clip emitted in order. */
function drive(full: string, steps: number): string[] {
  const all: string[] = [];
  let cursor = 0;
  for (let k = 1; k <= steps; k++) {
    const prefix = full.slice(0, Math.ceil((full.length * k) / steps));
    const { chunks, consumed } = chunkStream(prefix.slice(cursor), false);
    all.push(...chunks);
    cursor += consumed;
  }
  const { chunks } = chunkStream(full.slice(cursor), true);
  all.push(...chunks);
  return all;
}

describe("speakable", () => {
  it("authors pauses: each line/list item becomes its own sentence; a heading leads in with a colon", () => {
    // The core fix — whitespace was collapsed BEFORE newlines could become pauses, so a
    // list ran together. Every line now ends in terminal punctuation; a HEADING ends in ":"
    // (not "."), so it flows into the next item with lead-in intonation instead of a flat clip.
    expect(speakable("## Setup steps\n- Preheat oven\n- Add flour\n- Bake")).toBe(
      "Setup steps: Preheat oven. Add flour. Bake.",
    );
    expect(speakable("First line\nSecond line")).toBe("First line. Second line.");
  });

  it("ends a heading with a colon so it isn't an isolated flat clip", () => {
    // A heading read alone (its own clip ending in ".") sounds flat; ":" is a pause cue that
    // splitClips does NOT cut on, so the heading joins the following sentence in one clip.
    expect(speakable("# Background\nThe project began in 2019.")).toBe(
      "Background: The project began in two thousand nineteen.",
    );
    expect(chunkStream("# Background\nThe project began here.", true).chunks).toEqual([
      "Background: The project began here.",
    ]);
    // A heading already ending in terminal punctuation is left as-is (no doubled mark).
    expect(speakable("## Ready?\nYes.")).toBe("Ready? Yes.");
  });

  it("keeps an existing terminal mark rather than doubling it", () => {
    expect(speakable("Done already.\nNext one!")).toBe("Done already. Next one!");
  });

  it("linearizes a pipe table into one sentence per row", () => {
    const md = "| Name | Age |\n|------|-----|\n| Alice | 30 |\n| Bob | 25 |";
    expect(speakable(md)).toBe(
      "Name, Age. Row one: Name, Alice; Age, thirty. Row two: Name, Bob; Age, twenty five.",
    );
  });

  it("does not read a ragged table row's extra cells as nothing, and ignores a bare rule", () => {
    // A body row wider than the header keeps the extra cell (no silent drop).
    expect(speakable("| A | B |\n|---|---|\n| 1 | 2 | 3 |")).toBe(
      "A, B. Row one: A, one; B, two; three.",
    );
    // A prose line with a pipe followed by a horizontal rule is NOT a table.
    expect(speakable("apples | oranges\n-----\nDone.")).toBe("apples oranges. Done.");
  });

  it("announces fenced code blocks instead of reading them", () => {
    expect(speakable("Try this:\n```py\nprint(1)\n```\nDone.")).toBe("Try this: code block. Done.");
  });

  it("keeps inline code and link text, drops the URL and images", () => {
    expect(speakable("Run `npm ci` then see [the docs](https://x.io/a/b).")).toBe(
      "Run npm ci then see the docs.",
    );
    expect(speakable("![a chart](c.png) shows growth.")).toBe("shows growth.");
  });

  it("reads a bare URL as its domain, not the slug", () => {
    expect(speakable("Visit https://github.com/OHF-Voice/piper1-gpl for more")).toBe(
      "Visit github dot com for more.",
    );
    expect(speakable("see www.example.co.uk/path here")).toBe("see example dot co dot uk here.");
  });

  it("verbalizes currency, percent, ranges and decimals", () => {
    expect(speakable("It cost $1,250.50 today.")).toBe(
      "It cost one thousand two hundred fifty dollars and fifty cents today.",
    );
    // Magnitude words and non-cents decimals put the unit AFTER the amount (a regression: the old
    // regex matched only "$16" out of "$16.8 billion" and orphaned ".8 billion").
    expect(speakable("Revenue hit $16.8 billion.")).toBe(
      "Revenue hit sixteen point eight billion dollars.",
    );
    expect(speakable("a $5 million round")).toBe("a five million dollars round.");
    expect(speakable("worth $1.2 trillion")).toBe("worth one point two trillion dollars.");
    expect(speakable("about $16.8 here")).toBe("about sixteen point eight dollars here.");
    expect(speakable("Up 50% this week.")).toBe("Up fifty percent this week.");
    expect(speakable("Pick 3-5 items.")).toBe("Pick three to five items.");
    expect(speakable("Version 3.14 shipped.")).toBe("Version three point one four shipped.");
    expect(speakable("There are 1,024 files.")).toBe("There are one thousand twenty four files.");
  });

  it("speaks dates as ordinal day + speech-style year (before the number pass mangles them)", () => {
    expect(speakable("Tomorrow is July 10, 2026.")).toBe(
      "Tomorrow is July tenth, twenty twenty six.",
    );
    expect(speakable("due April 21 2010")).toBe("due April twenty first, twenty ten.");
    expect(speakable("born May 1, 2000")).toBe("born May first, two thousand.");
    expect(speakable("the July 10th deadline")).toBe("the July tenth deadline.");
  });

  it("speaks temperature units and the mi distance unit in full", () => {
    expect(speakable("a high near 94°F today")).toBe(
      "a high near ninety four degrees Fahrenheit today.",
    );
    expect(speakable("40 mi south of here")).toBe("forty miles south of here.");
    // A bare degree with no unit still reads "degrees" (the existing behaviour, unchanged).
    expect(speakable("tilt it 45° back")).toBe("tilt it forty five degrees back.");
  });

  it("verbalizes the allow-list emoji and drops the rest", () => {
    expect(speakable("Done ✅ and shipped 🚀")).toBe("Done check and shipped.");
    expect(speakable("Careful ⚠️ here")).toBe("Careful warning here.");
  });

  it("maps stray symbols to words", () => {
    expect(speakable("A & B need it")).toBe("A and B need it.");
    expect(speakable("go from a → b")).toBe("go from a to b.");
    expect(speakable("it is 20° outside")).toBe("it is twenty degrees outside.");
  });

  it("brackets a parenthetical with commas so piper pauses around it", () => {
    expect(speakable("defence spending (target 5% by 2035) and reaffirm")).toBe(
      "defence spending, target five percent by two thousand thirty five, and reaffirm.",
    );
    // A parenthetical at a sentence end folds into the period — no ",." / ".," stutter.
    expect(speakable("raise it (a lot).")).toBe("raise it, a lot.");
    expect(speakable("He agreed (reluctantly.)")).toBe("He agreed, reluctantly.");
  });

  it("expands e.g. / i.e. to spoken words with a pause", () => {
    expect(speakable("warm voices, e.g., Ashley, work best")).toBe(
      "warm voices, for example, Ashley, work best.",
    );
    // Period-space form (no comma) still gets the pause.
    expect(speakable("tune it e.g. slower")).toBe("tune it for example, slower.");
    // Inside a parenthetical, both rules compose cleanly.
    expect(speakable("Use the box voice (i.e. piper) here")).toBe(
      "Use the box voice, that is, piper, here.",
    );
  });

  it("pauses on a dash used as a clause break, but not a compound hyphen", () => {
    expect(speakable("you send me one of yours—let's see who wins")).toBe(
      "you send me one of yours, let's see who wins.",
    );
    expect(speakable("Play a track and guess it — great fun")).toBe(
      "Play a track and guess it, great fun.",
    );
    // Spaced ASCII hyphen used as a dash also gets the beat.
    expect(speakable("quick round - then swap")).toBe("quick round, then swap.");
    // Numeric ranges still read as "to", not a comma.
    expect(speakable("pick 3-5 of them")).toBe("pick three to five of them.");
    // A closed-up en-dash relation is a compound, not a clause break: "U.S.–Iran" must not read
    // "U S, Iran" (the reported awkward pause) — it reads as a space between the two clean words.
    expect(speakable("the U.S.–Iran conflict escalated")).toBe("the U S Iran conflict escalated.");
    expect(speakable("a New York–London flight")).toBe("a New York London flight.");
    // A SPACED en-dash is still a clause break → comma beat.
    expect(speakable("hold on – almost there")).toBe("hold on, almost there.");
  });

  it("speaks inequalities and relations instead of dropping the glyph (which inverts meaning)", () => {
    expect(speakable("5 < 10 and 10 > 5")).toBe("five less than ten and ten greater than five.");
    expect(speakable("x <= 3, y >= 7, z != 0")).toBe(
      "x less than or equal to three, y greater than or equal to seven, z not equal to zero.",
    );
    expect(speakable("a ≤ b ≥ c ≠ d ± e")).toBe(
      "a less than or equal to b greater than or equal to c not equal to d plus or minus e.",
    );
  });

  it("turns a snake_case underscore into a space instead of eating it", () => {
    expect(speakable("set the user_id field")).toBe("set the user id field.");
    expect(speakable("the read_aloud_engine value")).toBe("the read aloud engine value.");
  });

  it("reads clock times as words, not a mangled colon", () => {
    expect(speakable("meet at 3:30pm")).toBe("meet at three thirty P M.");
    expect(speakable("the 9:05 train")).toBe("the nine oh five train.");
    expect(speakable("at 3:00")).toBe("at three o'clock.");
    expect(speakable("depart 14:00 sharp")).toBe("depart fourteen hundred sharp.");
  });

  it("reads a phone number digit-by-digit, not as arithmetic ranges", () => {
    expect(speakable("call 555-123-4567 now")).toBe(
      "call five five five, one two three, four five six seven now.",
    );
  });

  it("reads a multi-part version number without garbling", () => {
    expect(speakable("upgrade to v2.3.1 today")).toBe(
      "upgrade to version two point three point one today.",
    );
    // A plain decimal (one dot) is untouched by the version rule.
    expect(speakable("it took 2.5 hours")).toBe("it took two point five hours.");
  });

  it("reads an ISO date as a spoken date, not a hyphen range", () => {
    expect(speakable("due 2026-08-10 for review")).toBe(
      "due August tenth, twenty twenty six for review.",
    );
  });

  it("splits a compound hyphen into a space so espeak doesn't mash the words", () => {
    // ASCII "large-scale" reads as "largescale" in espeak; a space gives two clean words. The
    // Unicode/non-breaking hyphens ("Bob‑verse", "most‑play‑again") are handled the same way.
    expect(speakable("a large-scale AI matrix")).toBe("a large scale AI matrix.");
    expect(speakable("the most‑play‑again award is well-known")).toBe(
      "the most play again award is well known.",
    );
    expect(speakable("a self‑replicating Bob‑verse probe")).toBe(
      "a self replicating Bob verse probe.",
    );
    // Letter/digit compounds too (model numbers, callsigns).
    expect(speakable("Aurora‑One near Kestrel‑7")).toBe("Aurora One near Kestrel seven.");
  });

  it("expands titles and abbreviations espeak reads wrong or splits on", () => {
    // Titles: espeak says "Mister" but breaks the sentence before the name — expanding + dropping
    // the period keeps the clause together.
    expect(speakable("Dr. Lee met Mr. Ng")).toBe("Doctor Lee met Mister Ng.");
    expect(speakable("Mrs. Ng and Ms. Poe")).toBe("Missus Ng and Miz Poe.");
    expect(speakable("Prof. Ada spoke")).toBe("Professor Ada spoke.");
    // vs/approx: espeak reads "V S" / "approx" — say the words.
    expect(speakable("cats vs. dogs")).toBe("cats versus dogs.");
    expect(speakable("approx. 5 items")).toBe("approximately five items.");
  });

  it("drops a name initial's period so it isn't read as a sentence end / split into a clip", () => {
    // The reported case: "E." made espeak pause AND the clip splitter cut "Taylor" onto a
    // separate render. Dropping the period fixes both — and the whole title stays ONE clip.
    expect(speakable("The Bobiverse (by Dennis E. Taylor)")).toBe(
      "The Bobiverse, by Dennis E Taylor.",
    );
    expect(chunkStream("The Bobiverse (by Dennis E. Taylor)", true).chunks).toEqual([
      "The Bobiverse, by Dennis E Taylor.",
    ]);
    // Chained initials all lose their periods; a real sentence end (lowercase-led next word) does not.
    expect(speakable("J. R. R. Tolkien wrote it")).toBe("J R R Tolkien wrote it.");
    expect(speakable("Grade A. then rest")).toBe("Grade A. then rest.");
    // A dotted initialism collapses to spaced letters ("U.S." → "U S") — no interior period to
    // read as a sentence end, so the name stays one clause.
    expect(speakable("the U.S. Grant memorial")).toBe("the U S Grant memorial.");
  });

  it("collapses dotted initialisms so they don't pause or split mid-sentence", () => {
    // The reported bug: "U.S." mid-sentence read with an obnoxious pause because its trailing "."
    // looked like a sentence end (espeak pause + a clip split). Collapsed to "U S", one clause.
    expect(speakable("The U.S. economy grew last year")).toBe("The U S economy grew last year.");
    expect(speakable("They met in Washington, D.C. today")).toBe(
      "They met in Washington, D C today.",
    );
    expect(speakable("a.m. and p.m. and a Ph.D.")).toBe("A M and P M and a P H D.");
    // The whole sentence stays ONE clip (no split at "U.S."), streaming and flushed alike.
    expect(chunkStream("The U.S. economy grew.", true).chunks).toEqual(["The U S economy grew."]);
    // Mid-stream (not flushed) the committer holds the sentence rather than cutting at "U.S. ".
    expect(chunkStream("The U.S. economy grew last year and", false).chunks).toEqual([]);
  });

  it("makes a line-ending ellipsis a real pause before the next paragraph", () => {
    // Reported: "diagnostic…\nThe console flickers" ran together — the ellipsis normalizer ate the
    // newline and the splitter never cuts on "…". Now the two lines are separate clips.
    expect(chunkStream("Running the diagnostic…\nThe console flickers.", true).chunks).toEqual([
      "Running the diagnostic….",
      "The console flickers.",
    ]);
    // A MID-line ellipsis is still a soft beat kept inside the clause (not split).
    expect(chunkStream("wait… okay then. Done.", true).chunks).toEqual([
      "wait… okay then.",
      "Done.",
    ]);
  });

  it("keeps an ellipsis as a dramatic beat (not a comma, not 'dot dot dot')", () => {
    // "..." and "…" both normalize to one ellipsis char — a ~300 ms pause espeak renders without
    // saying the dots, and which the chunker won't split on (it's not . ! ?).
    expect(speakable("wait... okay then")).toBe("wait… okay then.");
    expect(speakable("hmm… maybe")).toBe("hmm… maybe.");
    // A trailing ellipsis is itself terminal — no extra period appended.
    expect(speakable("that's it...")).toBe("that's it…");
  });

  it("speaks simple proper fractions but leaves ratios and dates as slashes", () => {
    expect(speakable("add 3/4 cup")).toBe("add three quarters cup.");
    expect(speakable("1/2 of it")).toBe("one half of it.");
    expect(speakable("2/3 done")).toBe("two thirds done.");
    // Improper ratio and a date must NOT become fractions (they fall through to the slash map).
    expect(speakable("24/7 support")).toBe("twenty four slash seven support.");
    expect(speakable("on 07/04 today")).toBe("on seven slash four today.");
  });

  it("speaks a numbered list's number but drops bare bullets", () => {
    // The number carries the enumeration, so it's read; a plain bullet does not.
    expect(speakable("4. Bring up a shared memory")).toBe("four. Bring up a shared memory.");
    expect(speakable("1. First\n2. Second\n10. Tenth")).toBe(
      "one. First. two. Second. ten. Tenth.",
    );
    expect(speakable("- just a bullet")).toBe("just a bullet.");
  });

  it("strips emphasis, headings, blockquotes and horizontal rules", () => {
    expect(speakable("**Bold** and _italic_ and ~~struck~~.")).toBe("Bold and italic and struck.");
    expect(speakable("> quoted note\n\n---\n\nafter rule")).toBe("quoted note. after rule.");
  });

  it("is a no-op-ish on plain prose and handles empty input", () => {
    expect(speakable("Just a normal sentence.")).toBe("Just a normal sentence.");
    expect(speakable("")).toBe("");
    // @ts-expect-error — defends against a null/undefined answer body
    expect(speakable(null)).toBe("");
  });
});

describe("chunkStream", () => {
  it("emits one clip per complete sentence and holds a partial tail", () => {
    expect(chunkStream("One sentence. Two sentence. Thr", false)).toEqual({
      chunks: ["One sentence.", "Two sentence."],
      consumed: "One sentence. Two sentence. ".length,
    });
  });

  it("holds an open code fence until it closes", () => {
    const open = "Here:\n```py\nprint(1)\n";
    expect(chunkStream(open, false).chunks).toEqual(["Here:"]); // fence body held
    const closed = `${open}print(2)\n\`\`\`\nDone.`;
    expect(chunkStream(closed, true).chunks).toEqual(["Here: code block.", "Done."]);
  });

  it("holds a streaming table until the block is complete, then linearizes it whole", () => {
    const partial = "Scores:\n| Name | Pts |\n|------|-----|\n| Al | 3 |\n";
    // The table body is still open (a next row could arrive), so only the lead-in commits.
    expect(chunkStream(partial, false).chunks).toEqual(["Scores:"]);
    const full = `${partial}| Bo | 2 |\n\nAfter.`;
    const clips = chunkStream(full, true).chunks;
    expect(clips[0]).toContain("Scores:"); // label + column lead-in merge (label ends in ":")
    expect(clips).toContain("Row one: Name, Al; Pts, three.");
    expect(clips).toContain("Row two: Name, Bo; Pts, two.");
    expect(clips.at(-1)).toBe("After.");
  });

  it("does not cut a clip after an abbreviation or ellipsis mid-stream", () => {
    // Buffer ends right after "Dr." with no real sentence terminator yet — must hold, not emit
    // "Doctor" as its own clip (which would drop a pause before the name).
    expect(chunkStream("Dr. Smith is here", false).chunks).toEqual([]);
    // Once the sentence closes it commits as one clip, with the title expanded.
    expect(chunkStream("Dr. Smith is here. ", false).chunks).toEqual(["Doctor Smith is here."]);
    // An ellipsis mid-thought is likewise held, not treated as a sentence boundary.
    expect(chunkStream("wait... nearly", false).chunks).toEqual([]);
  });

  it("streaming in any number of steps yields the same clips as one shot (no loss/dup)", () => {
    const doc =
      "# Report\n\nWe shipped 3 things today.\n\n- Fixed login\n- Added 2 charts\n\n" +
      "| Item | Qty |\n|------|-----|\n| Milk | 2 |\n| Eggs | 12 |\n\n" +
      "```js\nconst x = 1;\n```\n\nSee https://example.com/docs for 50% more. Done ✅";
    const oneShot = chunkStream(doc, true).chunks;
    for (const steps of [1, 2, 3, 7, 20, doc.length]) {
      expect(drive(doc, steps)).toEqual(oneShot);
    }
  });
});

describe("engineForVoice", () => {
  it("always resolves to the kokoro profile (the only on-box engine)", () => {
    expect(engineForVoice("kokoro-af_heart")).toBe("kokoro");
    expect(engineForVoice("kokoro-bm_george")).toBe("kokoro");
    expect(engineForVoice("")).toBe("kokoro");
    expect(engineForVoice(undefined as unknown as string)).toBe("kokoro");
  });
});
