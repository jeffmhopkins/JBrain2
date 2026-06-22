import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { connectLive } from "./liveSocket";

// A minimal stand-in for the browser WebSocket: records instances and lets a test
// fire the lifecycle callbacks. connectLive uses global setTimeout, so fake timers
// drive the reconnect backoff.
class FakeWS {
  static instances: FakeWS[] = [];
  url: string;
  onopen: (() => void) | null = null;
  onmessage: ((ev: { data: string }) => void) | null = null;
  onclose: (() => void) | null = null;
  close = vi.fn();
  constructor(url: string) {
    this.url = url;
    FakeWS.instances.push(this);
  }
}

beforeEach(() => {
  FakeWS.instances = [];
  vi.stubGlobal("WebSocket", FakeWS as unknown as typeof WebSocket);
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

function last(): FakeWS {
  const w = FakeWS.instances[FakeWS.instances.length - 1];
  if (!w) throw new Error("no socket opened");
  return w;
}

describe("connectLive", () => {
  it("opens a socket and delivers parsed fixes", () => {
    const onFix = vi.fn();
    connectLive(onFix);
    expect(FakeWS.instances).toHaveLength(1);
    last().onmessage?.({ data: JSON.stringify({ subject_id: "s-1", lat: 1, lon: 2 }) });
    expect(onFix).toHaveBeenCalledWith(expect.objectContaining({ subject_id: "s-1" }));
  });

  it("ignores a malformed frame without tearing down", () => {
    const onFix = vi.fn();
    connectLive(onFix);
    expect(() => last().onmessage?.({ data: "not json" })).not.toThrow();
    expect(onFix).not.toHaveBeenCalled();
  });

  it("reconnects with backoff after an unexpected close", () => {
    connectLive(vi.fn());
    last().onclose?.(); // socket dropped (network blip / server restart)
    expect(FakeWS.instances).toHaveLength(1); // not yet — waiting on the backoff
    vi.advanceTimersByTime(1000); // first backoff is 1s
    expect(FakeWS.instances).toHaveLength(2); // reopened
    // A clean open resets the backoff; the next drop reconnects after 1s again.
    last().onopen?.();
    last().onclose?.();
    vi.advanceTimersByTime(1000);
    expect(FakeWS.instances).toHaveLength(3);
  });

  it("stops reconnecting once closed by the caller", () => {
    const handle = connectLive(vi.fn());
    handle.close();
    expect(last().close).toHaveBeenCalled();
    last().onclose?.(); // even if the socket then fires close
    vi.advanceTimersByTime(60_000);
    expect(FakeWS.instances).toHaveLength(1); // no reconnect
  });
});
