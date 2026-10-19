import { afterEach, describe, expect, it, vi } from "vitest";
import { chatShareUrl } from "./agent/chatShare";
import { api } from "./api/client";
import { intakeShareUrl } from "./intake/share";
import { shareUrl } from "./jcode/share";
import { jlaunchShareUrl } from "./jlaunch/share";
import { mcShareUrl } from "./minecraftMapSource";
import { loadPublicOrigin, publicOrigin, setPublicOrigin } from "./publicOrigin";
import { researchShareUrl } from "./research/share";

// Links that leave the app must not carry the phone's private origin (OWNER_TUNNEL_PLAN §3.6).

function stubPublicUrl(response: Response | Error) {
  vi.stubGlobal(
    "fetch",
    vi.fn<typeof fetch>(async () => {
      if (response instanceof Error) throw response;
      return response;
    }),
  );
}

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
  setPublicOrigin(null);
});

describe("publicOrigin", () => {
  it("is the page's origin until a public address is loaded", () => {
    expect(publicOrigin()).toBe(window.location.origin);
  });

  it("uses the owner's address, else the box's setup one", async () => {
    stubPublicUrl(json({ url: "https://brain.example.com", env_url: "https://env.example.net" }));
    await loadPublicOrigin(api.publicUrl);
    expect(publicOrigin()).toBe("https://brain.example.com");
    stubPublicUrl(json({ url: "", env_url: "https://env.example.net/" }));
    await loadPublicOrigin(api.publicUrl);
    expect(publicOrigin()).toBe("https://env.example.net");
  });

  it("keeps the page's origin when the load fails or nothing is set", async () => {
    stubPublicUrl(json({ detail: "boom" }, 500));
    await loadPublicOrigin(api.publicUrl);
    expect(publicOrigin()).toBe(window.location.origin);
    stubPublicUrl(new TypeError("offline"));
    await loadPublicOrigin(api.publicUrl);
    expect(publicOrigin()).toBe(window.location.origin);
    stubPublicUrl(json({ url: "", env_url: "" }));
    await loadPublicOrigin(api.publicUrl);
    expect(publicOrigin()).toBe(window.location.origin);
  });

  it("is where every share link starts", () => {
    setPublicOrigin("https://brain.example.com");
    const base = "https://brain.example.com";
    expect(shareUrl("abc", "t0k")).toBe(`${base}/jcode/s/abc#t=t0k`);
    expect(researchShareUrl("r1")).toBe(`${base}/share/r1`);
    expect(chatShareUrl("c1")).toBe(`${base}/chat/s/c1`);
    expect(mcShareUrl("m1")).toBe(`${base}/mc/s/m1`);
    expect(intakeShareUrl("s1")).toBe(`${base}/intake#t=s1`);
    expect(jlaunchShareUrl("j1")).toBe(`${base}/results/j1`);
  });
});
