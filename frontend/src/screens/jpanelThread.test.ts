import { describe, expect, it } from "vitest";
import type { JpanelMessage, JpanelPetChat, JpanelPetTurn, JpanelThread } from "../api/client";
import { atLiveEnd, threadItems, threadPanels } from "./jpanelThread";

function msg(id: string, at: string, extra: Partial<JpanelMessage> = {}): JpanelMessage {
  return {
    id,
    from_name: "Lydian",
    to_name: "Dad",
    direction: "in",
    transcript: id,
    composed: "voice",
    duration_ms: 2000,
    created_at: at,
    played_at: null,
    ...extra,
  };
}

function turn(id: string, answeredAt: string, totalMs: number): JpanelPetTurn {
  return {
    id,
    heard: `${id} heard`,
    reply: `${id} reply`,
    stt_ms: 0,
    llm_ms: 0,
    tts_ms: 0,
    total_ms: totalMs,
    created_at: answeredAt,
  };
}

function thread(messages: JpanelMessage[]): JpanelThread {
  return { device_id: "dev-lydian", name: "Lydian", unplayed: 0, messages };
}

function chat(turns: JpanelPetTurn[]): JpanelPetChat {
  return { device_id: "dev-lydian", label: "Lydian", turns };
}

describe("who gets a chip on the picker", () => {
  it("lists a panel that has only ever talked to its pet", () => {
    // SHE WOULD NOT EXIST ON THIS SCREEN otherwise. A child who has never recorded a message
    // for her father — but asks the thing on her wall about fish every afternoon — has no
    // message thread at all, and a picker built from message threads would simply omit her.
    const panels = threadPanels(
      [{ device_id: "dev-lydian", name: "Lydian", unplayed: 2, messages: [] }],
      [
        { device_id: "dev-lydian", label: "Lydian", turns: [] },
        { device_id: "dev-elora", label: "Elora", turns: [turn("t1", "2026-09-29T10:00:00Z", 0)] },
      ],
    );
    expect(panels.map((p) => p.name)).toEqual(["Lydian", "Elora"]);
    expect(panels.map((p) => p.device_id)).toEqual(["dev-lydian", "dev-elora"]);
  });

  it("does not list a panel twice when it has both", () => {
    const panels = threadPanels(
      [{ device_id: "dev-lydian", name: "Lydian", unplayed: 3, messages: [] }],
      [{ device_id: "dev-lydian", label: "Lydian", turns: [] }],
    );
    expect(panels).toEqual([{ device_id: "dev-lydian", name: "Lydian", unplayed: 3 }]);
  });

  it("carries the unplayed count, which is what the chip is read for", () => {
    const panels = threadPanels(
      [{ device_id: "dev-lydian", name: "Lydian", unplayed: 4, messages: [] }],
      [],
    );
    expect(panels).toEqual([{ device_id: "dev-lydian", name: "Lydian", unplayed: 4 }]);
  });
});

describe("the merged conversation", () => {
  it("reads downwards, oldest first", () => {
    // Both routes serve newest first, which is right for an inbox and backwards for a chat.
    const items = threadItems(
      thread([msg("late", "2026-09-29T12:00:00Z"), msg("early", "2026-09-29T09:00:00Z")]),
      undefined,
    );
    expect(items.map((i) => i.key)).toEqual(["m:early", "m:late"]);
  });

  it("puts a message that arrived mid-turn between the question and the answer", () => {
    // THE WHOLE POINT OF MERGING. `pet_turn` is written when the reply is MADE, so its
    // timestamp is the answer's — her question happened `total_ms` earlier. A message that
    // landed while she was waiting belongs between them, which is exactly where it happened
    // and exactly what two separate tabs could never show.
    const items = threadItems(
      thread([msg("mid", "2026-09-29T10:00:03Z")]),
      chat([turn("t1", "2026-09-29T10:00:06Z", 6000)]),
    );
    expect(items.map((i) => i.key)).toEqual(["q:t1", "m:mid", "a:t1"]);
  });

  it("keeps a question before its own answer when the turn reports no elapsed time", () => {
    // A turn with `total_ms: 0` gives both halves the same instant, and an unstable or
    // naive sort would be free to print the pet answering before she spoke.
    const items = threadItems(undefined, chat([turn("t1", "2026-09-29T10:00:00Z", 0)]));
    expect(items.map((i) => i.key)).toEqual(["q:t1", "a:t1"]);
  });

  it("refuses to let a negative duration invert a turn", () => {
    const items = threadItems(undefined, chat([turn("t1", "2026-09-29T10:00:00Z", -5000)]));
    expect(items.map((i) => i.key)).toEqual(["q:t1", "a:t1"]);
  });

  it("interleaves several turns and messages by when they happened", () => {
    const items = threadItems(
      thread([msg("m2", "2026-09-29T11:00:00Z"), msg("m1", "2026-09-29T09:00:00Z")]),
      chat([turn("t2", "2026-09-29T12:00:02Z", 2000), turn("t1", "2026-09-29T10:00:01Z", 1000)]),
    );
    expect(items.map((i) => i.key)).toEqual(["m:m1", "q:t1", "a:t1", "m:m2", "q:t2", "a:t2"]);
  });

  it("survives a panel with nothing on either side", () => {
    expect(threadItems(undefined, undefined)).toEqual([]);
    expect(threadItems(thread([]), chat([]))).toEqual([]);
  });

  it("sorts an unreadable timestamp to the top rather than the live end", () => {
    // A row with no usable time is old news by definition. Sorting it to the bottom would
    // park it under the composer — the one place the eye goes first.
    const items = threadItems(
      thread([msg("good", "2026-09-29T10:00:00Z"), msg("bad", "not a date")]),
      undefined,
    );
    expect(items.map((i) => i.key)).toEqual(["m:bad", "m:good"]);
  });
});

describe("whether to follow the conversation down", () => {
  it("follows when the reader is already at the live end", () => {
    expect(atLiveEnd({ scrollTop: 900, scrollHeight: 1000, clientHeight: 100 })).toBe(true);
  });

  it("leaves someone reading back through this morning where they are", () => {
    // A chat that polls and always scrolls to the bottom takes the page away from its reader
    // every twenty seconds.
    expect(atLiveEnd({ scrollTop: 100, scrollHeight: 2000, clientHeight: 400 })).toBe(false);
  });

  it("allows a thumb's worth of imprecision", () => {
    expect(atLiveEnd({ scrollTop: 880, scrollHeight: 1000, clientHeight: 100 })).toBe(true);
  });
});
