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

/** A one-shot modifier the mobile key row arms for the next keystroke. A physical keyboard
 * sends Ctrl/Alt/Shift combinations directly; a soft keyboard can't, so these are applied
 * here to the next character or special key instead. */
export type Modifier = "ctrl" | "alt" | "shift";

/** The named special keys the mobile row can send (a soft keyboard can't produce these). */
export type SpecialKey = "esc" | "tab" | "up" | "down" | "left" | "right";

/** Base control sequences for each special key (no modifier applied). */
export const KEY_SEQ: Record<SpecialKey, string> = {
  esc: "\x1b",
  tab: "\t",
  up: "\x1b[A",
  down: "\x1b[B",
  right: "\x1b[C",
  left: "\x1b[D",
};

// The xterm modifier parameter is 1 + a bitmask (shift=1, alt=2, ctrl=4), encoded into the
// CSI sequence for cursor keys as "\x1b[1;{param}{final}" (e.g. Shift+Up → \x1b[1;2A).
const MOD_BIT: Record<Modifier, number> = { shift: 1, alt: 2, ctrl: 4 };
const ARROW_FINAL: Partial<Record<SpecialKey, string>> = {
  up: "A",
  down: "B",
  right: "C",
  left: "D",
};

/** Resolve a special key to the bytes to send, folding in an armed modifier. Shift+Tab is the
 * back-tab (CBT, \x1b[Z); Shift/Ctrl/Alt + an arrow use xterm's modified cursor-key encoding.
 * Combinations with no standard sequence (e.g. Ctrl+Tab, a modified Esc) fall back to base. */
export function keySequence(key: SpecialKey, mod: Modifier | null): string {
  if (!mod) return KEY_SEQ[key];
  if (key === "tab") return mod === "shift" ? "\x1b[Z" : KEY_SEQ.tab;
  const final = ARROW_FINAL[key];
  if (final) return `\x1b[1;${1 + MOD_BIT[mod]}${final}`;
  return KEY_SEQ[key];
}

/** Apply a soft-keyboard modifier to a typed chunk. Ctrl folds letters to their control
 * code (a→\x01 … z→\x1a, plus the usual @[\]^_ and space→NUL); Alt (Meta) prefixes ESC,
 * the xterm convention; Shift upper-cases (the soft keyboard sends the unshifted char).
 * Anything Ctrl can't fold passes through untouched. */
export function applyModifier(mod: Modifier, data: string): string {
  if (mod === "alt") return `\x1b${data}`;
  if (mod === "shift") return data.toUpperCase();
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

/** True for `beforeinput` inputTypes where the OS rewrites characters it believes are still
 * in the field — iOS autocorrect and smart punctuation (the double-space→". " shortcut, smart
 * quotes/dashes) all arrive as `insertReplacementText`. Plain typing (`insertText`), IME
 * composition, and deletion are NOT replacements and must pass through untouched. */
export function isReplacementInput(inputType: string): boolean {
  return inputType === "insertReplacementText";
}

/** Stop the mobile OS from editing already-committed bytes through xterm's helper textarea.
 * xterm forwards each keystroke to the shell and clears the textarea, so when iOS later fires a
 * replacement edit (autocorrect, or the double-space→". " shortcut) it reconstructs and re-emits
 * the whole line and the shell sees the input duplicated. A terminal never wants the OS to
 * rewrite committed input, so cancel those replacement edits at the source. Returns a disposer. */
export function guardMobileInput(textarea: HTMLTextAreaElement): () => void {
  const onBeforeInput = (e: Event) => {
    if (isReplacementInput((e as InputEvent).inputType)) e.preventDefault();
  };
  textarea.addEventListener("beforeinput", onBeforeInput);
  return () => textarea.removeEventListener("beforeinput", onBeforeInput);
}

/** The live terminal bridge the screen drives: a disposer plus the hooks the mobile key
 * row needs — sending a special key, and arming a one-shot Ctrl/Alt/Shift modifier. */
export interface TerminalHandle {
  /** Detach the listeners (the caller still closes the socket + disposes term). */
  detach(): void;
  /** Send a special key (an arrow, Esc, Tab), folding in any armed modifier (then clearing
   * it) — so an armed Shift + Tab sends the back-tab, an armed Ctrl + arrow the modified key. */
  sendKey(key: SpecialKey): void;
  /** Arm a modifier for the next keystroke, or clear it (null). */
  setModifier(mod: Modifier | null): void;
}

/** Bridge an xterm terminal to a session's shell socket: keystrokes/paste out as raw
 * bytes, the terminal's size out as a JSON resize control, and shell bytes in. When a
 * modifier is armed (the mobile Ctrl/Alt/Shift keys), the next typed chunk is transformed
 * and the modifier auto-clears. Returns a handle to detach and to drive the mobile key row. */
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
    sendKey(key) {
      send(keySequence(key, modifier));
      if (modifier) setModifier(null);
    },
    setModifier,
  };
}
