import { describe, expect, it, vi } from "vitest";
import { type SocketLike, type TermLike, attachTerminal, terminalWsUrl } from "./terminal";

describe("terminalWsUrl", () => {
  it("builds the owner's terminal endpoint from the page origin", () => {
    // ws: off the same-origin host (http page → ws, https → wss).
    expect(terminalWsUrl("abc123")).toBe(
      `ws://${window.location.host}/api/jcode/sessions/abc123/terminal`,
    );
  });
  it("encodes the session id", () => {
    expect(terminalWsUrl("a/b")).toContain("/sessions/a%2Fb/terminal");
  });
});

class FakeTerm implements TermLike {
  dataCb: ((d: string) => void) | null = null;
  resizeCb: ((s: { cols: number; rows: number }) => void) | null = null;
  written: (string | Uint8Array)[] = [];
  disposed = 0;
  onData(cb: (d: string) => void) {
    this.dataCb = cb;
    return { dispose: () => this.disposed++ };
  }
  onResize(cb: (s: { cols: number; rows: number }) => void) {
    this.resizeCb = cb;
    return { dispose: () => this.disposed++ };
  }
  write(data: string | Uint8Array) {
    this.written.push(data);
  }
}

function fakeSocket(): SocketLike & { sent: unknown[] } {
  return {
    binaryType: "blob",
    readyState: 1, // OPEN
    sent: [] as unknown[],
    send(data: unknown) {
      (this as { sent: unknown[] }).sent.push(data);
    },
    onmessage: null,
  };
}

// A minimal stand-in for the browser's MessageEvent (only `.data` is read).
const msg = (data: unknown) => ({ data }) as MessageEvent;

describe("attachTerminal", () => {
  it("sends keystrokes as raw bytes and resize as a JSON control", () => {
    const term = new FakeTerm();
    const ws = fakeSocket();
    attachTerminal(term, ws);
    expect(ws.binaryType).toBe("arraybuffer");

    term.dataCb?.("ls\n");
    expect(ws.sent[0]).toEqual(new TextEncoder().encode("ls\n"));

    term.resizeCb?.({ cols: 120, rows: 40 });
    expect(ws.sent[1]).toBe(JSON.stringify({ resize: { rows: 40, cols: 120 } }));
  });

  it("does not send when the socket is not open", () => {
    const term = new FakeTerm();
    const ws = fakeSocket();
    ws.readyState = 0; // CONNECTING
    attachTerminal(term, ws);
    term.dataCb?.("x");
    expect(ws.sent).toHaveLength(0);
  });

  it("writes binary frames as bytes and text frames as strings", () => {
    const term = new FakeTerm();
    const ws = fakeSocket();
    attachTerminal(term, ws);
    // A same-realm ArrayBuffer (as the browser delivers with binaryType="arraybuffer").
    const buf = new ArrayBuffer(4);
    new Uint8Array(buf).set([27, 91, 50, 74]);
    ws.onmessage?.(msg(buf));
    ws.onmessage?.(msg("hello"));
    expect(term.written[0]).toEqual(new Uint8Array([27, 91, 50, 74]));
    expect(term.written[1]).toBe("hello");
  });

  it("detaches its listeners on dispose", () => {
    const term = new FakeTerm();
    const dispose = attachTerminal(term, fakeSocket());
    dispose();
    expect(term.disposed).toBe(2); // onData + onResize
    vi.clearAllMocks();
  });
});
