// The interactive-terminal client glue, isolated from React + xterm so it's unit
// testable (the screen mounts the real xterm.js; this just bridges it to the socket).
// The browser sends the session cookie automatically on the same-origin handshake —
// the api authenticates the owner and proxies to the sandbox shell.

/** The owner's terminal WebSocket for a session (proxied to the jcode PTY). */
export function terminalWsUrl(sid: string): string {
  const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${window.location.host}/api/jcode/sessions/${encodeURIComponent(sid)}/terminal`;
}

/** The slice of xterm's Terminal we use — typed structurally so tests pass a fake. */
export interface TermLike {
  onData(cb: (data: string) => void): { dispose: () => void };
  onResize(cb: (size: { cols: number; rows: number }) => void): { dispose: () => void };
  write(data: string | Uint8Array): void;
}

/** The slice of WebSocket we use — kept assignable from a real WebSocket. */
export interface SocketLike {
  binaryType: BinaryType;
  readyState: number;
  send(data: string | ArrayBufferLike | ArrayBufferView): void;
  onmessage: ((ev: MessageEvent) => void) | null;
}

const WS_OPEN = 1; // WebSocket.OPEN

/** A one-shot modifier the mobile key row arms for the next typed character. A physical
 * keyboard sends Ctrl/Alt combinations directly; a soft keyboard can't, so these are
 * applied here to the next keystroke instead. */
export type Modifier = "ctrl" | "alt";

/** Control sequences the mobile key row sends verbatim (no character to modify). */
export const KEY_SEQ = {
  esc: "\x1b",
  tab: "\t",
  up: "\x1b[A",
  down: "\x1b[B",
  right: "\x1b[C",
  left: "\x1b[D",
} as const;

/** Apply a soft-keyboard modifier to a typed chunk. Ctrl folds letters to their control
 * code (a→\x01 … z→\x1a, plus the usual @[\]^_ and space→NUL); Alt (Meta) prefixes ESC,
 * the xterm convention. Anything Ctrl can't fold passes through untouched. */
export function applyModifier(mod: Modifier, data: string): string {
  if (mod === "alt") return `\x1b${data}`;
  let out = "";
  for (const ch of data) {
    const code = ch.charCodeAt(0);
    if (code >= 97 && code <= 122)
      out += String.fromCharCode(code - 96); // a-z → \x01-\x1a
    else if (code >= 64 && code <= 95)
      out += String.fromCharCode(code - 64); // @A-Z[\]^_ → \x00-\x1f
    else if (ch === " ")
      out += "\x00"; // Ctrl+Space → NUL
    else out += ch;
  }
  return out;
}

/** The live terminal bridge the screen drives: a disposer plus the hooks the mobile key
 * row needs — sending a raw control sequence, and arming a one-shot Ctrl/Alt modifier. */
export interface TerminalHandle {
  /** Detach the listeners (the caller still closes the socket + disposes term). */
  detach(): void;
  /** Send a control sequence (an arrow, Esc, Tab) straight to the shell. */
  sendKey(seq: string): void;
  /** Arm a modifier for the next typed character, or clear it (null). */
  setModifier(mod: Modifier | null): void;
}

/** Bridge an xterm terminal to a session's shell socket: keystrokes/paste out as raw
 * bytes, the terminal's size out as a JSON resize control, and shell bytes in. When a
 * modifier is armed (the mobile Ctrl/Alt keys), the next typed chunk is transformed and
 * the modifier auto-clears. Returns a handle to detach and to drive the mobile key row. */
export function attachTerminal(
  term: TermLike,
  ws: SocketLike,
  onModifierChange?: (mod: Modifier | null) => void,
): TerminalHandle {
  ws.binaryType = "arraybuffer";
  const encoder = new TextEncoder();
  let modifier: Modifier | null = null;
  const send = (data: string) => {
    if (ws.readyState === WS_OPEN) ws.send(encoder.encode(data));
  };
  const setModifier = (mod: Modifier | null) => {
    modifier = mod;
    onModifierChange?.(mod);
  };
  const onData = term.onData((data) => {
    if (modifier) {
      send(applyModifier(modifier, data));
      setModifier(null);
    } else {
      send(data);
    }
  });
  const onResize = term.onResize(({ cols, rows }) => {
    if (ws.readyState === WS_OPEN) ws.send(JSON.stringify({ resize: { rows, cols } }));
  });
  ws.onmessage = (ev) => {
    const payload = ev.data;
    if (payload instanceof ArrayBuffer) term.write(new Uint8Array(payload));
    else if (typeof payload === "string") term.write(payload);
  };
  return {
    detach() {
      onData.dispose();
      onResize.dispose();
    },
    sendKey: send,
    setModifier,
  };
}
