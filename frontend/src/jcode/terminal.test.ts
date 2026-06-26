import { describe, expect, it, vi } from "vitest";
import {
  KEY_SEQ,
  type SocketLike,
  type TermLike,
  applyModifier,
  attachTerminal,
  terminalWsUrl,
} from "./terminal";

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
    const { detach } = attachTerminal(term, fakeSocket());
    detach();
    expect(term.disposed).toBe(2); // onData + onResize
    vi.clearAllMocks();
  });

  it("sends a control sequence verbatim via sendKey", () => {
    const term = new FakeTerm();
    const ws = fakeSocket();
    const { sendKey } = attachTerminal(term, ws);
    sendKey(KEY_SEQ.up);
    expect(ws.sent[0]).toEqual(new TextEncoder().encode("\x1b[A"));
  });

  it("folds the next keystroke under an armed Ctrl, then auto-clears", () => {
    const term = new FakeTerm();
    const ws = fakeSocket();
    const changes: (string | null)[] = [];
    const { setModifier } = attachTerminal(term, ws, (m) => changes.push(m));

    setModifier("ctrl");
    term.dataCb?.("c"); // Ctrl+C → ETX
    expect(ws.sent[0]).toEqual(new TextEncoder().encode("\x03"));

    term.dataCb?.("c"); // modifier consumed — next key is literal
    expect(ws.sent[1]).toEqual(new TextEncoder().encode("c"));
    // Reported armed, then auto-cleared once the key was folded.
    expect(changes).toEqual(["ctrl", null]);
  });

  it("prefixes the next keystroke with ESC under an armed Alt", () => {
    const term = new FakeTerm();
    const ws = fakeSocket();
    const { setModifier } = attachTerminal(term, ws);
    setModifier("alt");
    term.dataCb?.("b"); // Alt+B → ESC b (word-back in readline)
    expect(ws.sent[0]).toEqual(new TextEncoder().encode("\x1bb"));
  });
});

describe("applyModifier", () => {
  it("folds letters to their control code", () => {
    expect(applyModifier("ctrl", "a")).toBe("\x01");
    expect(applyModifier("ctrl", "z")).toBe("\x1a");
  });
  it("folds Ctrl+Space to NUL and leaves digits untouched", () => {
    expect(applyModifier("ctrl", " ")).toBe("\x00");
    expect(applyModifier("ctrl", "5")).toBe("5");
  });
  it("prefixes ESC for Alt", () => {
    expect(applyModifier("alt", "x")).toBe("\x1bx");
  });
});
