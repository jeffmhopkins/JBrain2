// castSend tells the box's own refusals (an answer the owner reads) from a box that can't reach
// its TVs (a failure the screen words for itself): review of #1760.
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, api } from "./client";

function serve(status: number, body: unknown) {
  vi.stubGlobal(
    "fetch",
    vi.fn(() => Promise.resolve(new Response(JSON.stringify(body), { status }))),
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("castSend", () => {
  it("returns a refusal with its reason", async () => {
    serve(415, { detail: { reason: "bad_picture" } });
    expect(await api.castSend({ tv_ids: ["a"], title: "x" })).toEqual({
      refused: { status: 415, reason: "bad_picture" },
    });
  });

  it.each([503, 502])("throws for a box that can't reach its TVs (%i)", async (status) => {
    serve(status, { detail: "cast sidecar unreachable (ConnectError) — is it running?" });
    const err = await api.castSend({ tv_ids: ["a"], title: "x" }).catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).status).toBe(status);
  });

  it("throws for a validation error with no reason", async () => {
    serve(422, { detail: [{ loc: ["body", "tv_ids"], msg: "too long" }] });
    await expect(api.castSend({ tv_ids: ["a"], title: "x" })).rejects.toBeInstanceOf(ApiError);
  });
});
