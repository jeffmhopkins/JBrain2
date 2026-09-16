import { afterEach, describe, expect, it, vi } from "vitest";
import { RESUME_THROTTLE_MS, armUpdateChecks } from "./swUpdate";

function reg() {
  return { update: vi.fn(), calls: 0 };
}

/** Put the document into a foreground/background state the helper will read back. */
function setVisibility(state: "visible" | "hidden") {
  Object.defineProperty(document, "visibilityState", { value: state, configurable: true });
}

afterEach(() => setVisibility("visible"));

describe("when the PWA asks for a new build", () => {
  it("asks the moment the app comes back, which is when the owner is about to look", () => {
    // The defect this exists for: the only trigger was an hourly timer, so for up to an
    // hour after a deploy the owner saw the previous bundle with nothing saying so — and
    // he reported a fix that had already shipped. A resumed iOS PWA is not a fresh load,
    // so `autoUpdate`'s own reload never gets its moment either.
    const r = reg();
    const off = armUpdateChecks(r, { now: () => 0 });
    expect(r.update).toHaveBeenCalledTimes(0);
    window.dispatchEvent(new Event("pageshow"));
    expect(r.update).toHaveBeenCalledTimes(1);
    off();
  });

  it("listens on every resume signal, not just visibilitychange", () => {
    // iOS delivers `pageshow` with no visibility event at all, and a resume on a
    // different network arrives as `online`. `visibility.RESUME_EVENTS` enumerates them
    // for the pollers for exactly this reason; the update check rides the same list.
    for (const event of ["pageshow", "focus", "online"]) {
      const r = reg();
      let t = 0;
      const off = armUpdateChecks(r, { now: () => t });
      t += RESUME_THROTTLE_MS + 1;
      window.dispatchEvent(new Event(event));
      expect(r.update, event).toHaveBeenCalledTimes(1);
      off();
    }
  });

  it("collapses the burst that one resume fires", () => {
    // `pageshow` and `focus` together is routine; the request is cheap but four of them
    // are still four.
    const r = reg();
    const off = armUpdateChecks(r, { now: () => 1_000 });
    window.dispatchEvent(new Event("pageshow"));
    window.dispatchEvent(new Event("focus"));
    window.dispatchEvent(new Event("online"));
    expect(r.update).toHaveBeenCalledTimes(1);
    off();
  });

  it("asks again once the throttle has passed", () => {
    const r = reg();
    let t = 0;
    const off = armUpdateChecks(r, { now: () => t });
    window.dispatchEvent(new Event("pageshow"));
    t += RESUME_THROTTLE_MS + 1;
    window.dispatchEvent(new Event("pageshow"));
    expect(r.update).toHaveBeenCalledTimes(2);
    off();
  });

  it("stays quiet while genuinely backgrounded", () => {
    // `focus` and `online` both fire while hidden, so the state is re-read rather than
    // inferred from the event — a hidden app has no business holding the server busy.
    const r = reg();
    setVisibility("hidden");
    const off = armUpdateChecks(r, { now: () => 0 });
    window.dispatchEvent(new Event("focus"));
    window.dispatchEvent(new Event("online"));
    expect(r.update).toHaveBeenCalledTimes(0);
    off();
  });

  it("keeps the hourly timer as the backstop for an app never backgrounded", () => {
    const r = reg();
    // Captured in an array rather than a `let`: TS cannot see the assignment inside the
    // injected callback, so it narrows the variable to `never` at the call below.
    const ticks: (() => void)[] = [];
    const off = armUpdateChecks(r, {
      now: () => 0,
      setInterval: ((fn: () => void) => {
        ticks.push(fn);
        return 1 as unknown as ReturnType<typeof setInterval>;
      }) as unknown as typeof setInterval,
    });
    expect(ticks).toHaveLength(1);
    ticks[0]?.();
    expect(r.update).toHaveBeenCalledTimes(1);
    off();
  });

  it("tears down its listeners, so a remount cannot stack them", () => {
    const r = reg();
    const off = armUpdateChecks(r, { now: () => 0 });
    off();
    window.dispatchEvent(new Event("pageshow"));
    expect(r.update).toHaveBeenCalledTimes(0);
  });
});
