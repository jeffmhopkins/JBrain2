import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { BRAIN_LOG_NAMES_KEY, splitNames } from "../growbot";
import { GrowbotBrainLogSheet } from "./GrowbotBrainLogSheet";

// The brain log sheet (DESIGN.md "GrowBot — the brain log"): pick a window, hash or keep
// speech, list names to hide, and download the box's zip under the box's name.

function zip(name: string): Response {
  return new Response(new Blob(["PK"]), {
    headers: { "Content-Disposition": `attachment; filename="${name}"` },
  });
}

describe("GrowbotBrainLogSheet", () => {
  beforeEach(() => {
    localStorage.clear();
    URL.createObjectURL = vi.fn(() => "blob:x");
    URL.revokeObjectURL = vi.fn();
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("downloads the window picked, hashed by default, with the names to hide", async () => {
    const fetch = vi.fn(async () => zip("growbot-brain-log-20261112-0300-3h.zip"));
    vi.stubGlobal("fetch", fetch);
    const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
    const say = vi.fn();
    const onClose = vi.fn();
    render(<GrowbotBrainLogSheet say={say} onClose={onClose} />);
    expect(screen.getByRole("button", { name: "1 hour" })).toHaveAttribute("aria-pressed", "true");
    fireEvent.click(screen.getByRole("button", { name: "3 hours" }));
    fireEvent.change(screen.getByLabelText("Names to hide"), {
      target: { value: "Quorra, Tamsin" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Download brain log (.zip)" }));
    await waitFor(() => expect(onClose).toHaveBeenCalled());
    const [url, init] = fetch.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/growbot/brain-log");
    expect(init.method).toBe("POST");
    expect(JSON.parse(String(init.body))).toEqual({
      hours: 3,
      redact: true,
      names: ["Quorra", "Tamsin"],
    });
    expect(click).toHaveBeenCalled();
    expect(say).toHaveBeenCalledWith(
      "Saved growbot-brain-log-20261112-0300-3h.zip to Downloads. Send it to GrowBot's developer as it is.",
    );
    expect(localStorage.getItem(BRAIN_LOG_NAMES_KEY)).toBe("Quorra, Tamsin");
  });

  it("keeps speech when asked, and remembers the names on this phone", async () => {
    localStorage.setItem(BRAIN_LOG_NAMES_KEY, "Quorra");
    const fetch = vi.fn(async () => zip("x.zip"));
    vi.stubGlobal("fetch", fetch);
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
    render(<GrowbotBrainLogSheet say={vi.fn()} onClose={vi.fn()} />);
    expect(screen.getByLabelText("Names to hide")).toHaveValue("Quorra");
    fireEvent.click(screen.getByLabelText(/Keep what people said/));
    fireEvent.click(screen.getByRole("button", { name: "Download brain log (.zip)" }));
    await waitFor(() => expect(fetch).toHaveBeenCalled());
    const [, init] = fetch.mock.calls[0] as unknown as [string, RequestInit];
    expect(JSON.parse(String(init.body))).toMatchObject({ hours: 1, redact: false });
  });

  it("says why when the box refuses, and stays open", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response(JSON.stringify({ detail: "db down" }), { status: 503 })),
    );
    const onClose = vi.fn();
    render(<GrowbotBrainLogSheet say={vi.fn()} onClose={onClose} />);
    fireEvent.click(screen.getByRole("button", { name: "Download brain log (.zip)" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("The brain log didn't download");
    expect(onClose).not.toHaveBeenCalled();
  });

  it("splits names on commas and lines and drops blanks", () => {
    expect(splitNames(" Quorra ,\nTamsin,, x ,")).toEqual(["Quorra", "Tamsin"]);
  });
});
