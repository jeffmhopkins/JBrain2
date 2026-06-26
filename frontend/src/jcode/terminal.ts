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

/** Bridge an xterm terminal to a session's shell socket: keystrokes/paste out as raw
 * bytes, the terminal's size out as a JSON resize control, and shell bytes in. Returns
 * a disposer that detaches the listeners (the caller closes the socket + disposes term). */
export function attachTerminal(term: TermLike, ws: SocketLike): () => void {
  ws.binaryType = "arraybuffer";
  const encoder = new TextEncoder();
  const onData = term.onData((data) => {
    if (ws.readyState === WS_OPEN) ws.send(encoder.encode(data));
  });
  const onResize = term.onResize(({ cols, rows }) => {
    if (ws.readyState === WS_OPEN) ws.send(JSON.stringify({ resize: { rows, cols } }));
  });
  ws.onmessage = (ev) => {
    const payload = ev.data;
    if (payload instanceof ArrayBuffer) term.write(new Uint8Array(payload));
    else if (typeof payload === "string") term.write(payload);
  };
  return () => {
    onData.dispose();
    onResize.dispose();
  };
}
