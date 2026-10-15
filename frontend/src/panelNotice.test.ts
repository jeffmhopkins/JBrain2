import { describe, expect, it } from "vitest";
import type { PanelNoticeOut } from "./api/client";
import {
  type Draft,
  clipBytes,
  dayLabel,
  draftProblem,
  emptyDraft,
  groupByDay,
  noticeLayout,
  noticePayload,
  outcome,
  photoPlacement,
  pictureSize,
  refusalCopy,
  shareFresh,
  textBox,
  workingSize,
  wrapWords,
} from "./panelNotice";

const PHOTO = { source: {} as CanvasImageSource, w: 4000, h: 3000 };

function draft(over: Partial<Draft> = {}): Draft {
  return { ...emptyDraft(), ...over };
}

function notice(over: Partial<PanelNoticeOut> = {}): PanelNoticeOut {
  return {
    id: "n-1",
    device_id: "panel-desk",
    sender: "owner",
    title: "Home soon!",
    body: "",
    choices: [],
    state: "queued",
    answer: null,
    created_at: "2026-10-14T14:02:10Z",
    delivered_at: null,
    shown_at: null,
    finished_at: null,
    hide_after_s: 20,
    image: null,
    audio_ms: null,
    expires_at: null,
    ttl_s: 900,
    chime: true,
    kind: "plain",
    ...over,
  };
}

/** A measure where every character is half the font size wide, like the mock's estimate. */
const half = (size: number) => (s: string) => s.length * size * 0.5;

describe("what the panel will show", () => {
  it("is a picture with a photo or words, a strip over a question, the card alone, or nothing", () => {
    expect(noticeLayout(draft())).toBe("empty");
    expect(noticeLayout(draft({ words: "  " }))).toBe("empty");
    expect(noticeLayout(draft({ words: "Lunch?" }))).toBe("full");
    expect(noticeLayout(draft({ photo: PHOTO }))).toBe("full");
    expect(noticeLayout(draft({ photo: PHOTO, ask: true }))).toBe("banner");
    expect(noticeLayout(draft({ ask: true }))).toBe("plain");
  });

  it("shrinks the strip for a question that takes two lines", () => {
    expect(pictureSize(draft({ words: "x" }))).toEqual({ w: 480, h: 480 });
    expect(
      pictureSize(draft({ words: "x", ask: true, question: "Did the package come?" })),
    ).toEqual({ w: 480, h: 240 });
    expect(
      pictureSize(
        draft({ words: "x", ask: true, question: "Did the plumber show up this morning?" }),
      ),
    ).toEqual({ w: 480, h: 200 });
  });

  it("says why Review cannot be reached yet", () => {
    expect(draftProblem(draft())).toMatch(/Add a photo/);
    expect(draftProblem(draft({ words: "hi" }))).toBeNull();
    expect(draftProblem(draft({ ask: true, answers: ["Yes"] }))).toBe("Write the question.");
    expect(draftProblem(draft({ ask: true, question: "Q?" }))).toBe("Add at least one answer.");
    expect(draftProblem(draft({ ask: true, question: "Q?", answers: ["Yes", " "] }))).toBe(
      "An answer is empty.",
    );
    expect(draftProblem(draft({ ask: true, question: "Q?", answers: ["Yes", "Yes "] }))).toBe(
      "Two answers read the same.",
    );
    expect(draftProblem(draft({ ask: true, question: "Q?", answers: ["Yes", "No"] }))).toBeNull();
  });
});

describe("the photo in its square", () => {
  it("covers the square, and zoom and pan are held so no edge shows", () => {
    const p = photoPlacement(PHOTO, { zoom: 1, panX: 0, panY: 0 }, 480, 480);
    expect(p.scale).toBeCloseTo(0.16);
    expect(p.y).toBe(0);
    expect(p.x).toBeCloseTo(-80);
    const far = photoPlacement(PHOTO, { zoom: 1, panX: 999, panY: 999 }, 480, 480);
    expect(far.panX).toBeCloseTo(80);
    expect(far.panY).toBe(0);
    expect(far.x).toBeCloseTo(0);
    const zoomed = photoPlacement(PHOTO, { zoom: 2, panX: 0, panY: -999 }, 480, 480);
    expect(zoomed.panY).toBeCloseTo(-240);
  });

  it("keeps a working copy no larger than zoom can use, and never enlarges one", () => {
    expect(workingSize(4000, 3000)).toEqual({ w: 1440, h: 1080 });
    expect(workingSize(300, 200)).toEqual({ w: 300, h: 200 });
  });
});

describe("words on the picture", () => {
  it("wraps to the width, at most four lines, breaking a word too wide for one", () => {
    expect(wrapWords("Home soon!", 440, half(64))).toEqual(["Home soon!"]);
    expect(wrapWords("one two three four five six seven", 160, half(40))).toEqual([
      "one two",
      "three",
      "four",
      "five six",
    ]);
    expect(wrapWords("Supercalifragilistic", 200, half(40))).toEqual(["Supercalif", "ragilistic"]);
    expect(wrapWords("   ", 440, half(64))).toEqual([]);
  });

  it("keeps the words' box inside the picture wherever they are dragged", () => {
    const m = half(64);
    const box = textBox(["Home soon!"], 64, { x: 0, y: 0 }, 480, 480, m);
    expect(box.x).toBeCloseTo(0);
    expect(box.y).toBeCloseTo(0);
    const right = textBox(["Home soon!"], 64, { x: 480, y: 480 }, 480, 480, m);
    expect(right.x + right.w).toBeCloseTo(480);
    expect(right.y + right.h).toBeCloseTo(480);
  });

  it("clips to the box's byte limit without splitting a character", () => {
    expect(clipBytes("héllo", 3)).toBe("hé");
    expect(clipBytes("👍👍", 5)).toBe("👍");
  });
});

describe("what goes to the box", () => {
  it("sends a picture whole-glass, hiding after the chosen time", () => {
    expect(noticePayload(draft({ words: " Lunch? ", stayPlain: 60 }), "d", "AAAA")).toEqual({
      device_id: "d",
      title: "Lunch?",
      chime: true,
      image_b64: "AAAA",
      image_mode: "full",
      hide_after_s: 60,
    });
  });

  it("names a photo alone, and keeps one that waits for a tap a whole day", () => {
    expect(noticePayload(draft({ photo: PHOTO, stayPlain: 0 }), "d", "AAAA")).toMatchObject({
      title: "Photo",
      hide_after_s: 0,
      ttl_s: 86400,
    });
  });

  it("sends a question with its answers, a strip above it, waiting as long as chosen", () => {
    const d = draft({
      photo: PHOTO,
      ask: true,
      question: "Did the package come?",
      answers: ["Yes ", "No"],
      stayAsk: 3600,
      chime: false,
    });
    expect(noticePayload(d, "d", "AAAA")).toEqual({
      device_id: "d",
      title: "Did the package come?",
      chime: false,
      image_b64: "AAAA",
      image_mode: "banner",
      choices: ["Yes", "No"],
      hide_after_s: 0,
      ttl_s: 3600,
    });
  });

  it("sends a question alone with no picture", () => {
    const d = draft({ ask: true, question: "Oven off?", answers: ["Yes", "No"] });
    expect(noticePayload(d, "d", null)).not.toHaveProperty("image_b64");
  });
});

describe("how a sent notice is doing", () => {
  it.each([
    [notice(), "Sent · the panel picks it up within seconds", "live"],
    [notice({ state: "shown", choices: ["Yes"] }), "Waiting for an answer", "live"],
    [notice({ state: "answered", choices: ["Yes"], answer: "Yes" }), "Answered “Yes”", "ok"],
    [notice({ state: "dismissed", choices: ["Yes"] }), "Hidden without an answer", "warn"],
    [notice({ state: "dismissed" }), "Tapped away on the panel", "dim"],
    [notice({ state: "expired" }), "The panel never picked it up", "warn"],
    [
      notice({ state: "expired", delivered_at: "x", shown_at: "x" }),
      "Shown · hid itself after 20 s",
      "dim",
    ],
    [
      notice({ state: "expired", delivered_at: "x", choices: ["Yes"], ttl_s: 900 }),
      "No answer in 15 min",
      "warn",
    ],
    [notice({ state: "withdrawn" }), "Unsent before the panel had it", "dim"],
    [notice({ state: "withdrawn", delivered_at: "x" }), "Unsent · taken off the panel", "dim"],
  ])("%#: says %s", (n, text, tone) => {
    expect(outcome(n)).toEqual({ text, tone });
  });

  it("groups the week by day, newest first as the box lists them", () => {
    const now = new Date(2026, 9, 14, 15, 0);
    const at = (d: number, h: number) => new Date(2026, 9, d, h).toISOString();
    const groups = groupByDay(
      [
        notice({ id: "a", created_at: at(14, 14) }),
        notice({ id: "b", created_at: at(14, 9) }),
        notice({ id: "c", created_at: at(13, 19) }),
        notice({ id: "d", created_at: at(10, 8) }),
      ],
      now,
    );
    expect(
      groups.map((g) => [g.label === dayLabel(at(10, 8), now) ? "older" : g.label, g.rows.length]),
    ).toEqual([
      ["Today", 2],
      ["Yesterday", 1],
      ["older", 1],
    ]);
  });
});

describe("the box's refusal in words", () => {
  it("says the night hours and points at the switch", () => {
    const r = refusalCopy({ status: 409, reason: "quiet_hours", until: "06:30" });
    expect(r.title).toBe("it’s night hours on the panel until 06:30");
    expect(r.body).toMatch(/after 06:30/);
    expect(r.settings).toBe(true);
  });

  it("says how long an offline panel has been gone", () => {
    expect(refusalCopy({ status: 503, reason: "offline", last_seen_s: 7200 }).body).toMatch(
      /last heard from 2 h ago/,
    );
    expect(refusalCopy({ status: 503, reason: "offline", last_seen_s: null }).body).toMatch(
      /has not heard/,
    );
  });

  it.each([
    ["not_accepting", "notices are off on the panel", true],
    ["busy", "4 notices already waiting", false],
    ["no_notice_surface", "the panel’s firmware can’t show notices yet", false],
    ["image_too_complex", "the picture is too detailed for the panel", false],
    ["image_format", "the panel can’t take this picture", false],
    ["which_panel", "the box turned it away", false],
  ])("%s", (reason, title, settings) => {
    const r = refusalCopy({ status: 409, reason });
    expect(r.title).toBe(title);
    expect(r.settings).toBe(settings);
  });
});

describe("a shared photo waiting for the composer", () => {
  it("is offered only when it was left moments ago", () => {
    const now = 1_000_000;
    expect(shareFresh(String(now - 5_000), now)).toBe(true);
    expect(shareFresh(String(now - 3 * 60_000), now)).toBe(false);
    expect(shareFresh(String(now + 60_000), now)).toBe(false);
    expect(shareFresh(null, now)).toBe(false);
    expect(shareFresh("soon", now)).toBe(false);
  });
});
