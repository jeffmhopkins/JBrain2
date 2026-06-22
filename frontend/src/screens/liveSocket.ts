// The live-location WebSocket client (JBrain360 M4d), isolated from React so the
// map screen stays testable (tests mock this module). The browser sends the
// session cookie automatically on the same-origin handshake — the device key is
// never in JS — and the server fans out only the fixes this viewer may see
// (own + family group), so the client just renders what arrives.

/** One live position off /api/locations/live (the server's `live_out` shape). */
export interface LiveFix {
  subject_id: string;
  lat: number;
  lon: number;
  accuracy_m: number | null;
  battery_pct: number | null;
  velocity_mps: number | null;
  captured_at: string;
}

export interface LiveHandle {
  close: () => void;
}

// Reconnect backoff: a dropped socket (a network blip while driving, a server
// restart, a long background) must recover on its own — otherwise the map silently
// freezes until a reload. Capped exponential, reset on a clean open.
const RECONNECT_BASE_MS = 1000;
const RECONNECT_MAX_MS = 15_000;

export function connectLive(onFix: (fix: LiveFix) => void): LiveHandle {
  const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
  const url = `${proto}//${window.location.host}/api/locations/live`;
  let ws: WebSocket | null = null;
  let closed = false; // the caller asked to stop — don't reconnect
  let attempt = 0;
  let timer: ReturnType<typeof setTimeout> | undefined;

  const open = () => {
    if (closed) return;
    const sock = new WebSocket(url);
    ws = sock;
    sock.onopen = () => {
      attempt = 0; // a good connection resets the backoff
    };
    sock.onmessage = (ev) => {
      try {
        onFix(JSON.parse(ev.data as string) as LiveFix);
      } catch {
        // Ignore a malformed frame rather than tear down the live stream.
      }
    };
    sock.onclose = () => {
      if (closed) return;
      const delay = Math.min(RECONNECT_BASE_MS * 2 ** attempt, RECONNECT_MAX_MS);
      attempt += 1;
      timer = setTimeout(open, delay);
    };
  };
  open();

  return {
    close: () => {
      closed = true;
      if (timer !== undefined) clearTimeout(timer);
      ws?.close();
    },
  };
}
