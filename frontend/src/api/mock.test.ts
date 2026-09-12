// Contract checks for the fixture backend so `npm run dev:mock` keeps
// working as screens evolve (mock states are part of a screen's definition
// of done — docs/reference/DESIGN.md "UI development process").

import { describe, expect, it } from "vitest";
import type {
  AppSettings,
  AttachmentExtract,
  EntityOut,
  LlmUsage,
  NoteAnalysis,
  NoteOut,
  ReviewItem,
  ReviewQueue,
  SdrRecording,
  SdrRecordingsPage,
  SearchOut,
  WikiArticleOut,
  WikiLandingOut,
  WikiTalkOut,
} from "./client";
import { mockFetch } from "./mock";

async function call(path: string, init?: RequestInit): Promise<Response> {
  return mockFetch(path, init);
}

function jsonInit(method: string, body: unknown): RequestInit {
  return { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
}

describe("mock API", () => {
  it("searches fixtures with literal <mark> snippets and match badges", async () => {
    const res = await call("/api/search?q=vitamin&limit=20");
    const out = (await res.json()) as SearchOut;
    expect(out.degraded).toBe(false);
    expect(out.results.length).toBeGreaterThan(0);
    expect(out.results[0]?.snippet).toContain("<mark>");
    expect(["semantic", "keyword", "both"]).toContain(out.results[0]?.match);
  });

  it('flips degraded keyword-only mode on a "degraded!" query', async () => {
    const res = await call("/api/search?q=degraded!%20vitamin");
    const out = (await res.json()) as SearchOut;
    expect(out.degraded).toBe(true);
    expect(out.results.every((r) => r.match === "keyword")).toBe(true);
  });

  it("filters search by domain", async () => {
    const res = await call("/api/search?q=vitamin&domain=finance");
    const out = (await res.json()) as SearchOut;
    expect(out.results.every((r) => r.domain === "finance")).toBe(true);
  });

  it("the search wiki leg returns a matching article ranked above note passages", async () => {
    // "denver" matches exactly one article's title/blurb; a wiki hit outscores
    // any note passage, so it heads the merged list.
    const res = await call("/api/search?q=denver");
    const out = (await res.json()) as SearchOut;
    const first = out.results[0];
    expect(first?.kind).toBe("wiki");
    if (first?.kind === "wiki") {
      expect(first.title).toBe("Denver");
      expect(first.article_id).toBe("denver");
    }
  });

  it("degraded mode drops the wiki leg (the semantic index is down)", async () => {
    const res = await call("/api/search?q=degraded!%20globex");
    const out = (await res.json()) as SearchOut;
    expect(out.results.some((r) => r.kind === "wiki")).toBe(false);
  });

  it("serves the wiki landing rails, and 'landing' is not swallowed by /api/wiki/:id", async () => {
    const res = await call("/api/wiki/landing");
    const landing = (await res.json()) as WikiLandingOut;
    expect(landing.recent.length).toBeGreaterThan(0);
    expect(landing.hubs.length).toBeGreaterThan(0);
    expect(landing.groups.length).toBeGreaterThan(0);
    // Every landing entry resolves to a real article (all rows are navigable).
    for (const entry of landing.groups.flatMap((g) => g.entries)) {
      const article = (await (await call(`/api/wiki/${entry.id}`)).json()) as WikiArticleOut;
      expect(article.id).toBe(entry.id);
    }
  });

  it("serves the Talk board and round-trips new-topic / reply / resolve / 409", async () => {
    const board = (await (await call("/api/wiki/priya-nair/talk")).json()) as WikiTalkOut;
    expect(board.title).toBe("Priya Nair");
    const log = board.topics.find((t) => t.kind === "build_log");
    expect(log?.posts.at(-1)?.author).toBe("builder");

    // New topic prepends; a reply appends; resolve flips status.
    const created = await call(
      "/api/wiki/priya-nair/talk/topics",
      jsonInit("POST", { title: "Wrong title", body: "fix it" }),
    );
    expect(created.status).toBe(201);
    const topic = (await created.json()) as { id: string };
    const reply = await call(
      `/api/wiki/priya-nair/talk/topics/${topic.id}/posts`,
      jsonInit("POST", { body: "please" }),
    );
    expect(reply.status).toBe(201);
    const patched = await call(
      `/api/wiki/priya-nair/talk/topics/${topic.id}`,
      jsonInit("PATCH", { status: "resolved" }),
    );
    expect(((await patched.json()) as { status: string }).status).toBe("resolved");

    // The Build log is machine-written: owner posts/resolves are refused.
    const refused = await call(
      `/api/wiki/priya-nair/talk/topics/${log?.id}/posts`,
      jsonInit("POST", { body: "nope" }),
    );
    expect(refused.status).toBe(409);

    // The Editor turn returns a reply post with an outcome chip; refused on the Build log.
    const editor = await call(
      `/api/wiki/priya-nair/talk/topics/${topic.id}/editor`,
      jsonInit("POST", { after_post_id: "p" }),
    );
    const edited = (await editor.json()) as { post: { author: string; outcome: string | null } };
    expect(edited.post.author).toBe("editor");
    expect(edited.post.outcome).toContain("correction filed");
    const editorRefused = await call(
      `/api/wiki/priya-nair/talk/topics/${log?.id}/editor`,
      jsonInit("POST", { after_post_id: "p" }),
    );
    expect(editorRefused.status).toBe(409);
  });

  it("accepts capture location on create and echoes it back", async () => {
    const res = await call(
      "/api/notes",
      jsonInit("POST", {
        client_id: "mock-test-loc",
        domain: "general",
        body: "located note",
        latitude: 47.6,
        longitude: -122.3,
        accuracy_m: 30,
      }),
    );
    expect(res.status).toBe(201);
    const note = (await res.json()) as NoteOut;
    expect(note.latitude).toBe(47.6);
    expect(note.ingest_state).toBe("pending");
  });

  it("PATCH mutates the fixture, resets ingest_state, and 400s unknown domains", async () => {
    const created = (await (
      await call(
        "/api/notes",
        jsonInit("POST", { client_id: "mock-test-patch", domain: "general", body: "before" }),
      )
    ).json()) as NoteOut;

    const bad = await call(`/api/notes/${created.id}`, jsonInit("PATCH", { domain: "bogus" }));
    expect(bad.status).toBe(400);

    const ok = await call(
      `/api/notes/${created.id}`,
      jsonInit("PATCH", { body: "after", domain: "health", destination: "Labs" }),
    );
    const updated = (await ok.json()) as NoteOut;
    expect(updated).toMatchObject({
      body: "after",
      domain: "health",
      destination: "Labs",
      ingest_state: "pending",
    });
  });

  it("DELETE removes the note; a second delete 404s", async () => {
    const created = (await (
      await call(
        "/api/notes",
        jsonInit("POST", { client_id: "mock-test-delete", domain: "general", body: "doomed" }),
      )
    ).json()) as NoteOut;

    expect((await call(`/api/notes/${created.id}`, { method: "DELETE" })).status).toBe(204);
    expect((await call(`/api/notes/${created.id}`, { method: "DELETE" })).status).toBe(404);
  });

  it("hide drops the note from the stream; unhide restores it", async () => {
    const created = (await (
      await call(
        "/api/notes",
        jsonInit("POST", { client_id: "mock-test-hide", domain: "general", body: "hide me" }),
      )
    ).json()) as NoteOut;
    const inStream = async () =>
      ((await (await call("/api/notes?limit=100")).json()) as { notes: NoteOut[] }).notes.some(
        (n) => n.id === created.id,
      );

    expect(await inStream()).toBe(true);
    expect((await call(`/api/notes/${created.id}/hide`, { method: "POST" })).status).toBe(204);
    expect(await inStream()).toBe(false);
    // Hidden but still directly fetchable — it lives on in Search.
    expect(((await (await call(`/api/notes/${created.id}`)).json()) as NoteOut).hidden).toBe(true);

    expect((await call(`/api/notes/${created.id}/unhide`, { method: "POST" })).status).toBe(204);
    expect(await inStream()).toBe(true);
    expect((await call(`/api/notes/${crypto.randomUUID()}/hide`, { method: "POST" })).status).toBe(
      404,
    );
  });

  it("serves a full analysis for the Dr. Patel note, empty analysis elsewhere", async () => {
    const page = (await (await call("/api/notes?limit=100")).json()) as { notes: NoteOut[] };
    const patel = page.notes.find((n) => n.body.includes("Saw Dr. Patel this morning"));
    if (!patel) throw new Error("patel fixture note missing");

    const analysis = (await (await call(`/api/notes/${patel.id}/analysis`)).json()) as NoteAnalysis;
    expect(analysis.analyzed_at).not.toBeNull();
    expect(analysis.tags.length).toBeGreaterThanOrEqual(3);
    expect(analysis.facts).toHaveLength(6);
    const kinds = new Set(analysis.facts.map((f) => f.kind));
    expect(kinds).toContain("measurement");
    expect(kinds).toContain("event");
    expect(analysis.facts.some((f) => f.status === "pending_review")).toBe(true);
    expect(analysis.facts.some((f) => f.pinned)).toBe(true);
    expect(analysis.temporal_tokens.some((t) => t.resolved_start?.startsWith("2026-09"))).toBe(
      true,
    );

    const other = page.notes.find((n) => n.id !== patel.id);
    if (!other) throw new Error("second fixture note missing");
    const empty = (await (await call(`/api/notes/${other.id}/analysis`)).json()) as NoteAnalysis;
    expect(empty.analyzed_at).toBeNull();
    expect(empty.facts).toHaveLength(0);
  });

  it("entity pages: Sarah carries the Denver→Austin address history, newest first", async () => {
    const sarah = (await (await call("/api/entities/ent-sarah")).json()) as EntityOut;
    const address = sarah.predicates.find((p) => p.predicate === "address");
    expect(address?.current?.value_json).toBe("Denver, CO");
    expect(address?.current?.status).toBe("pending_review");
    expect(address?.history.map((f) => f.value_json)).toEqual(["Denver, CO", "Austin, TX"]);
    expect(address?.history[1]?.status).toBe("superseded");
    expect((await call("/api/entities/ent-nobody")).status).toBe(404);
  });

  it("review: open queue covers every kind; resolve mutates fixture state", async () => {
    const queue = (await (await call("/api/review?status=open")).json()) as ReviewQueue;
    expect(queue.items.length).toBeGreaterThanOrEqual(6);
    const kinds = new Set(queue.items.map((i) => i.kind));
    for (const kind of [
      "fact_conflict",
      "attribute_collision",
      "merge_proposal",
      "ambiguous_mention",
      "domain_promotion",
      "low_confidence",
      "split_proposal",
    ]) {
      expect(kinds).toContain(kind);
    }

    const first = queue.items[0];
    if (!first) throw new Error("empty review fixture");
    // Collisions advertise accept_a/accept_b choices and no footer verbs;
    // an unadvertised action is rejected like the real backend rejects it.
    expect(first.kind).toBe("attribute_collision");
    expect(first.payload.outcomes).toBeUndefined();
    const choices = first.payload.choices as { action: string }[];
    expect(choices.map((c) => c.action)).toEqual(["accept_a", "accept_b"]);
    expect(
      (await call(`/api/review/${first.id}/resolve`, jsonInit("POST", { action: "accept" })))
        .status,
    ).toBe(400);

    const res = await call(
      `/api/review/${first.id}/resolve`,
      jsonInit("POST", { action: "accept_b", payload: {} }),
    );
    expect(res.status).toBe(200);
    const updated = (await res.json()) as ReviewItem;
    expect(updated.status).toBe("resolved");
    expect(updated.resolved_at).not.toBeNull();
    expect(updated.resolution?.action).toBe("accept_b");
    // Effects are recorded like the backend records them, so reopen can
    // round-trip in dev:mock.
    const actions = (updated.resolution?.effects ?? []).map((e) => e.action);
    expect(actions).toEqual(["pinned", "retracted"]);

    const after = (await (await call("/api/review?status=open")).json()) as ReviewQueue;
    expect(after.items.map((i) => i.id)).not.toContain(first.id);
    // Resolving twice conflicts rather than silently double-applying.
    expect(
      (
        await call(
          `/api/review/${first.id}/resolve`,
          jsonInit("POST", { action: "accept_a", payload: {} }),
        )
      ).status,
    ).toBe(409);
  });

  it("review: resolved log lists decisions newest-first with dismissals folded in", async () => {
    const log = (await (await call("/api/review?status=resolved")).json()) as ReviewQueue;
    expect(log.items.length).toBeGreaterThanOrEqual(4);
    const statuses = new Set(log.items.map((i) => i.status));
    expect(statuses).toContain("resolved");
    expect(statuses).toContain("dismissed");
    const decidedAt = (i: ReviewItem) => i.resolved_at ?? i.resolution?.reopened_at ?? i.created_at;
    const order = log.items.map(decidedAt);
    expect(order).toEqual([...order].sort((a, b) => b.localeCompare(a)));
    for (const item of log.items) {
      expect(item.resolution).not.toBeNull();
    }
  });

  it("review: reopen re-queues with a tombstone marker; double-reopen 409s", async () => {
    const reopened = await call("/api/review/rev-done-2/reopen", { method: "POST" });
    expect(reopened.status).toBe(200);
    const item = (await reopened.json()) as ReviewItem & { reopen_note: string | null };
    expect(item.status).toBe("open");
    expect(item.resolved_at).toBeNull();
    expect(item.resolution?.reopened_at).toBeDefined();
    expect(item.reopen_note).toBeNull();

    // Back in the open queue AND tombstoned in the resolved log.
    const open = (await (await call("/api/review?status=open")).json()) as ReviewQueue;
    expect(open.items.map((i) => i.id)).toContain("rev-done-2");
    const log = (await (await call("/api/review?status=resolved")).json()) as ReviewQueue;
    const tomb = log.items.find((i) => i.id === "rev-done-2");
    expect(tomb?.status).toBe("open");

    expect((await call("/api/review/rev-done-2/reopen", { method: "POST" })).status).toBe(409);
    expect((await call("/api/review/rev-nope/reopen", { method: "POST" })).status).toBe(404);
  });

  it("review: reopening a rejected merge keeps the permanent distinct_from edge", async () => {
    const reopened = await call("/api/review/rev-done-3/reopen", { method: "POST" });
    expect(reopened.status).toBe(200);
    const item = (await reopened.json()) as ReviewItem & { reopen_note: string | null };
    expect(item.status).toBe("open");
    expect(item.reopen_note).toContain("permanent");
  });

  it("llm-usage: totals exercise k/M formatting and a null-cost task", async () => {
    const usage = (await (await call("/api/ops/llm-usage")).json()) as LlmUsage;
    expect(usage.today.input_tokens).toBeGreaterThan(1000);
    expect(usage.month.input_tokens).toBeGreaterThan(1_000_000);
    expect(usage.by_task.some((t) => t.cost_usd === null)).toBe(true);
    expect(usage.days).toHaveLength(7);
  });

  it("serves the vision cache for the described image fixture", async () => {
    const page = (await (await call("/api/notes?limit=100")).json()) as { notes: NoteOut[] };
    const att = page.notes
      .flatMap((n) => n.attachments)
      .find((a) => a.filename === "roof-quote.jpg");
    if (!att) throw new Error("roof-quote.jpg fixture missing");
    expect(att.has_extracts).toBe(true);
    expect(att.has_description).toBe(true);

    const out = (await (await call(`/api/attachments/${att.id}/extracts`)).json()) as {
      extracts: AttachmentExtract[];
    };
    expect(out.extracts.map((e) => e.kind)).toEqual(["ocr", "caption"]);
    expect(out.extracts[0]?.text).toContain("[illegible]");
    expect(out.extracts[0]?.confidence).toBe(0.7);
    expect(out.extracts[1]?.confidence).toBe(0.6);

    expect((await call("/api/attachments/att-nope/extracts")).status).toBe(404);
  });

  it("serves generated-image bytes by id, and an edit's /source, else 404", async () => {
    // The result image (by id) and the edit's "before" source both round-trip.
    const result = await call("/api/images/generated/mock-genimg-lighthouse-stormy");
    expect(result.status).toBe(200);
    expect(result.headers.get("Content-Type")).toBe("image/png");
    const source = await call("/api/images/generated/mock-genimg-lighthouse/source");
    expect(source.status).toBe(200);
    expect(source.headers.get("Content-Type")).toBe("image/png");
    // An unknown id is a clean 404, never a blank image.
    expect((await call("/api/images/generated/img-nope")).status).toBe(404);
  });

  it("round-trips the image-analysis setting with strict validation", async () => {
    const before = (await (await call("/api/settings")).json()) as AppSettings;
    expect(before.image_analysis_mode).toBe("full"); // the decided default

    const put = await call("/api/settings", jsonInit("PUT", { image_analysis_mode: "ocr" }));
    expect(((await put.json()) as AppSettings).image_analysis_mode).toBe("ocr");
    const after = (await (await call("/api/settings")).json()) as AppSettings;
    expect(after.image_analysis_mode).toBe("ocr");

    expect(
      (await call("/api/settings", jsonInit("PUT", { image_analysis_mode: "everything" }))).status,
    ).toBe(422);
    expect((await call("/api/settings", jsonInit("PUT", { theme: "dark" }))).status).toBe(422);

    // Back to the default so later mock sessions start where they expect.
    await call("/api/settings", jsonInit("PUT", { image_analysis_mode: "full" }));
  });

  it("on-demand analyze 409s while in flight, then flips the fixture state", async () => {
    const page = (await (await call("/api/notes?limit=100")).json()) as { notes: NoteOut[] };
    const att = page.notes
      .flatMap((n) => n.attachments)
      .find((a) => a.filename === "whiteboard.jpg");
    if (!att) throw new Error("whiteboard.jpg fixture missing");
    expect(att.has_description).toBe(false);

    expect((await call("/api/attachments/att-nope/analyze", { method: "POST" })).status).toBe(404);
    expect((await call(`/api/attachments/${att.id}/analyze`, { method: "POST" })).status).toBe(202);
    expect((await call(`/api/attachments/${att.id}/analyze`, { method: "POST" })).status).toBe(409);

    // A tick later the worker "finished": extracts cached, chip flips.
    await new Promise((resolve) => setTimeout(resolve, 500));
    const refreshed = (await (await call("/api/notes?limit=100")).json()) as { notes: NoteOut[] };
    const flipped = refreshed.notes.flatMap((n) => n.attachments).find((a) => a.id === att.id);
    expect(flipped?.has_extracts).toBe(true);
    expect(flipped?.has_description).toBe(true);
    const out = (await (await call(`/api/attachments/${att.id}/extracts`)).json()) as {
      extracts: AttachmentExtract[];
    };
    expect(out.extracts.some((e) => e.kind === "caption" && e.text !== "")).toBe(true);
    // The gate: every image on the note now has extracts, so analysis lands
    // with it — the whiteboard fixture round-trips the gated sequence.
    const owner = refreshed.notes.find((n) => n.attachments.some((a) => a.id === att.id));
    expect(owner?.analyzed).toBe(true);
    const analysis = (await (
      await call(`/api/notes/${owner?.id}/analysis`)
    ).json()) as NoteAnalysis;
    expect(analysis.analyzed_at).not.toBeNull();
    // ...and a re-run is allowed again once the flight lands.
    expect((await call(`/api/attachments/${att.id}/analyze`, { method: "POST" })).status).toBe(202);
  });

  it("note analyze: 404 unknown, 202 then 409 while the run is in flight", async () => {
    expect(
      (await call(`/api/notes/${crypto.randomUUID()}/analyze`, { method: "POST" })).status,
    ).toBe(404);

    const page = (await (await call("/api/notes?limit=100")).json()) as { notes: NoteOut[] };
    const grocery = page.notes.find((n) => n.body.startsWith("Groceries:"));
    if (!grocery) throw new Error("grocery fixture note missing");

    expect((await call(`/api/notes/${grocery.id}/analyze`, { method: "POST" })).status).toBe(202);
    expect((await call(`/api/notes/${grocery.id}/analyze`, { method: "POST" })).status).toBe(409);

    // Once the flight lands, a note with no analysis fixture synthesizes a
    // minimal record and re-runs are allowed again.
    await new Promise((resolve) => setTimeout(resolve, 700));
    const settled = (await (await call(`/api/notes/${grocery.id}`)).json()) as NoteOut;
    expect(settled.analyzed).toBe(true);
    const minimal = (await (
      await call(`/api/notes/${grocery.id}/analysis`)
    ).json()) as NoteAnalysis;
    expect(minimal.analyzed_at).not.toBeNull();
    expect(minimal.facts).toHaveLength(0);
    expect((await call(`/api/notes/${grocery.id}/analyze`, { method: "POST" })).status).toBe(202);
    await new Promise((resolve) => setTimeout(resolve, 700));
  });

  it("note analyze: analyzed drops, image extracts gate, then analyzed_at bumps", async () => {
    const page = (await (await call("/api/notes?limit=100")).json()) as { notes: NoteOut[] };
    const patel = page.notes.find((n) => n.body.includes("Saw Dr. Patel this morning"));
    if (!patel) throw new Error("patel fixture note missing");
    const before = (await (await call(`/api/notes/${patel.id}/analysis`)).json()) as NoteAnalysis;
    expect(before.analyzed_at).not.toBeNull();

    expect((await call(`/api/notes/${patel.id}/analyze`, { method: "POST" })).status).toBe(202);
    // analyzed drops immediately — the lifecycle chip walks "analyzing…".
    const during = (await (await call(`/api/notes/${patel.id}`)).json()) as NoteOut;
    expect(during.analyzed).toBe(false);

    await new Promise((resolve) => setTimeout(resolve, 700));
    const after = (await (await call(`/api/notes/${patel.id}`)).json()) as NoteOut;
    expect(after.analyzed).toBe(true);
    const fresh = (await (await call(`/api/notes/${patel.id}/analysis`)).json()) as NoteAnalysis;
    expect(fresh.analyzed_at).not.toBeNull();
    expect(fresh.analyzed_at).not.toBe(before.analyzed_at);
    // The Patel fixture keeps its facts across re-runs.
    expect(fresh.facts).toHaveLength(6);
  });

  // Last on purpose: reset wipes the shared fixtures the tests above read.
  it("ops reset: zeroes content fixtures, keeps usage; status ticks running→exited", async () => {
    expect((await call("/api/ops/reset", { method: "POST" })).status).toBe(202);

    const page = (await (await call("/api/notes?limit=100")).json()) as { notes: NoteOut[] };
    expect(page.notes).toHaveLength(0);
    const open = (await (await call("/api/review?status=open")).json()) as ReviewQueue;
    expect(open.items).toHaveLength(0);
    expect((await call("/api/entities/ent-sarah")).status).toBe(404);

    // Spend telemetry survives resets, like app.llm_usage on the real stack.
    const usage = (await (await call("/api/ops/llm-usage")).json()) as LlmUsage;
    expect(usage.today.input_tokens).toBeGreaterThan(0);

    const first = (await (await call("/api/ops/reset/status")).json()) as { state: string };
    expect(first.state).toBe("running");
    await call("/api/ops/reset/status");
    const done = (await (await call("/api/ops/reset/status")).json()) as {
      state: string;
      exit_code: number | null;
      log_tail: string;
    };
    expect(done.state).toBe("exited");
    expect(done.exit_code).toBe(0);
    expect(done.log_tail).toContain("[reset] complete");
  });

  // --- the SDR recordings surface. DESIGN.md implementation rule 4 makes default /
  // empty / error / offline part of this screen's definition of done, and the two
  // failure states are NOT the same shape: one answers, the other never arrives.
  it("serves a recordings library whose sizes are the sidecar's real arithmetic", async () => {
    const page = (await (await call("/api/sdr/recordings")).json()) as SdrRecordingsPage;
    expect(page.recordings.length).toBeGreaterThan(0);
    // 64 kbps mono MP3 is 8 kB/s. Every row's size has to follow from its length, or the
    // trim sheet's "frees N" is arguing from a number nobody can check.
    for (const row of page.recordings) {
      // ...for a CLIP. A captions row has no file, so it has no size — null rather than
      // the bitrate applied to something that is not audio, which is what the box's own
      // CHECK holds and what the disk meter is filtered on.
      expect(row.bytes).toBe(row.kind === "captions" ? null : Math.round(row.duration_s * 8000));
      // The LIST carries no waveform, matching the real route: 400 floats a row would
      // dwarf the response. A fixture that handed it over here would make the by-id
      // fetch — the one path that has to work on a real box — the one path mock mode
      // never exercises.
      expect(row.peaks).toBeUndefined();
    }
    // Newest first — the only order the library is ever read in.
    const times = page.recordings.map((r) => new Date(r.started_at).getTime());
    expect([...times].sort((a, b) => b - a)).toEqual(times);
    // One fixture is already trimmed, so the header's "reclaimed" line has something to
    // report; it is derived from duration_s < captured_s, never from a stored flag.
    expect(page.recordings.some((r) => r.duration_s < r.captured_s)).toBe(true);
    expect(page.usage.reclaimed_bytes).toBeGreaterThan(0);
    // ...and one fixture is captions, because the library has to draw a row with no play
    // control, no scissors and no size — the shape only the long press produces.
    expect(page.recordings.some((r) => r.kind === "captions")).toBe(true);
    // The meter counts every recording but weighs only the clips, exactly as the api's
    // `FILTER (WHERE kind = 'audio')` does.
    expect(page.usage.count).toBe(page.recordings.length);
    expect(page.usage.bytes).toBe(page.recordings.reduce((total, r) => total + (r.bytes ?? 0), 0));
  });

  it("carries the waveform on the by-id route, which is the only place it lives", async () => {
    const page = (await (await call("/api/sdr/recordings")).json()) as SdrRecordingsPage;
    const first = page.recordings[0];
    expect(first).toBeDefined();

    const one = (await (await call(`/api/sdr/recordings/${first?.id}`)).json()) as SdrRecording;

    expect(one.id).toBe(first?.id);
    expect(one.peaks?.length ?? 0).toBeGreaterThan(0);
  });

  it("answers a by-id fetch for a recording that is gone with a 404", async () => {
    // The trim sheet can outlive its row: deleted in another tab, or trimmed away while
    // the sheet sat open. It has to see a status, not a body it will try to draw.
    expect((await call("/api/sdr/recordings/nope")).status).toBe(404);
  });

  it("offers the empty, error and offline states the screen has to draw", async () => {
    const empty = (await (await call("/api/sdr/recordings?state=empty")).json()) as {
      recordings: unknown[];
      usage: { reclaimed_bytes: number };
    };
    expect(empty.recordings).toHaveLength(0);
    expect(empty.usage.reclaimed_bytes).toBe(0);

    expect((await call("/api/sdr/recordings?state=error")).status).toBe(500);

    // Offline REJECTS rather than answering, because that is what fetch does with no
    // network — a fixture that returned 503 would let a screen pass that only handles
    // ApiError and hangs on a real outage.
    await expect(call("/api/sdr/recordings?state=offline")).rejects.toThrow(TypeError);
  });

  it("round-trips record on/off, and the stopped capture lands in the library", async () => {
    const before = ((await (await call("/api/sdr/recordings")).json()) as SdrRecordingsPage)
      .recordings.length;

    const on = (await (await call("/api/sdr/record?on=true", { method: "POST" })).json()) as {
      recording: { bytes: number } | null;
    };
    expect(on.recording).not.toBeNull();
    // The status is where the Record button reads its elapsed time and running size.
    const status = (await (await call("/api/sdr/status")).json()) as {
      recording: { bytes: number } | null;
    };
    expect(status.recording).not.toBeNull();

    const off = (await (await call("/api/sdr/record?on=false", { method: "POST" })).json()) as {
      recording: unknown;
      saved: { id: string } | null;
    };
    expect(off.recording).toBeNull();
    expect(off.saved).not.toBeNull();
    const after = (await (await call("/api/sdr/recordings")).json()) as SdrRecordingsPage;
    expect(after.recordings).toHaveLength(before + 1);

    // Idempotent both ways, like POST /sdr/aprs: stopping twice is not an error.
    const again = (await (await call("/api/sdr/record?on=false", { method: "POST" })).json()) as {
      recording: unknown;
    };
    expect(again.recording).toBeNull();
  });

  it("trims to a frame boundary and answers with what it actually cut", async () => {
    const page = (await (await call("/api/sdr/recordings")).json()) as SdrRecordingsPage;
    const target = page.recordings.find((r) => r.duration_s > 60);
    if (!target) throw new Error("fixture has no clip long enough to trim");

    const answer = (await (
      await call(
        `/api/sdr/recordings/${target.id}/trim`,
        jsonInit("POST", { start_s: 5, end_s: 25 }),
      )
    ).json()) as {
      recording: { duration_s: number; bytes: number; captured_s: number };
      cut: { start_s: number; end_s: number };
      usage: { reclaimed_bytes: number };
    };
    const cut = answer.recording;
    // 20 s is not a whole number of 72 ms frames, so the server answers with the one it
    // could actually make — the client's seconds were only ever a request.
    expect(cut.duration_s).toBeCloseTo(20, 1);
    const frames = cut.duration_s / 0.072;
    expect(Math.abs(frames - Math.round(frames))).toBeLessThan(1e-6);
    expect(cut.bytes).toBe(Math.round(cut.duration_s * 8000));
    // captured_s is untouched, which is what keeps the row marked "trimmed".
    expect(cut.captured_s).toBe(target.captured_s);

    // The meter travels with the answer, so the header moves with the list.
    expect(answer.usage.reclaimed_bytes).toBeGreaterThan(0);
    expect((await call(`/api/sdr/recordings/${target.id}`, { method: "DELETE" })).status).toBe(200);
    expect((await call(`/api/sdr/recordings/${target.id}`, { method: "DELETE" })).status).toBe(404);
  });

  it("acknowledges a chat-run cancel (the composer's Stop)", async () => {
    const res = await call("/api/chat/runs/run-1/cancel", { method: "POST" });
    expect(res.status).toBe(204);
  });
});
