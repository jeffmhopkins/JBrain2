import { describe, expect, it, vi } from "vitest";
import {
  type SocketLike,
  type TermLike,
  applyModifier,
  attachTerminal,
  guardMobileInput,
  isReplacementInput,
  keySequence,
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

  it("sends a special key's base sequence via sendKey", () => {
    const term = new FakeTerm();
    const ws = fakeSocket();
    const { sendKey } = attachTerminal(term, ws);
    sendKey("up");
    expect(ws.sent[0]).toEqual(new TextEncoder().encode("\x1b[A"));
  });

  it("folds an armed modifier into the next special key, then auto-clears", () => {
    const term = new FakeTerm();
    const ws = fakeSocket();
    const changes: (string | null)[] = [];
    const { sendKey, setModifier } = attachTerminal(term, ws, (m) => changes.push(m));

    setModifier("shift");
    sendKey("tab"); // Shift+Tab → back-tab (CBT)
    expect(ws.sent[0]).toEqual(new TextEncoder().encode("\x1b[Z"));

    sendKey("tab"); // modifier consumed — next Tab is plain
    expect(ws.sent[1]).toEqual(new TextEncoder().encode("\t"));
    expect(changes).toEqual(["shift", null]);
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
  it("upper-cases under Shift (the soft keyboard sends the unshifted char)", () => {
    expect(applyModifier("shift", "a")).toBe("A");
    expect(applyModifier("shift", "5")).toBe("5");
  });
});

describe("keySequence", () => {
  it("returns the base sequence with no modifier", () => {
    expect(keySequence("tab", null)).toBe("\t");
    expect(keySequence("up", null)).toBe("\x1b[A");
  });
  it("maps Shift+Tab to the back-tab (CBT)", () => {
    expect(keySequence("tab", "shift")).toBe("\x1b[Z");
  });
  it("encodes modified cursor keys with the xterm modifier parameter", () => {
    expect(keySequence("up", "shift")).toBe("\x1b[1;2A");
    expect(keySequence("left", "alt")).toBe("\x1b[1;3D");
    expect(keySequence("right", "ctrl")).toBe("\x1b[1;5C");
  });
  it("falls back to base for combinations with no standard sequence", () => {
    expect(keySequence("tab", "ctrl")).toBe("\t");
    expect(keySequence("esc", "shift")).toBe("\x1b");
  });
});

describe("isReplacementInput", () => {
  it("flags OS text-substitution edits and passes normal input through", () => {
    // The double-space→". " shortcut, autocorrect, and smart quotes all fire as replacements.
    expect(isReplacementInput("insertReplacementText")).toBe(true);
    // Plain typing, composition, and deletion must not be cancelled.
    expect(isReplacementInput("insertText")).toBe(false);
    expect(isReplacementInput("insertCompositionText")).toBe(false);
    expect(isReplacementInput("deleteContentBackward")).toBe(false);
  });
});

describe("guardMobileInput", () => {
  // A minimal EventTarget stand-in that records addEventListener/removeEventListener and can
  // dispatch a beforeinput carrying an inputType (jsdom's InputEvent doesn't surface it here).
  function fakeTextarea() {
    let handler: ((e: Event) => void) | null = null;
    return {
      el: {
        addEventListener: (type: string, cb: (e: Event) => void) => {
          if (type === "beforeinput") handler = cb;
        },
        removeEventListener: () => {
          handler = null;
        },
      } as unknown as HTMLTextAreaElement,
      fire(inputType: string) {
        let prevented = false;
        const preventDefault = () => {
          prevented = true;
        };
        handler?.({ inputType, preventDefault } as unknown as Event);
        return prevented;
      },
      get attached() {
        return handler !== null;
      },
    };
  }

  it("cancels replacement edits but lets plain typing through, and detaches", () => {
    const ta = fakeTextarea();
    const dispose = guardMobileInput(ta.el);
    expect(ta.fire("insertReplacementText")).toBe(true); // the duplicating substitution
    expect(ta.fire("insertText")).toBe(false); // a real keystroke
    dispose();
    expect(ta.attached).toBe(false);
  });
});
