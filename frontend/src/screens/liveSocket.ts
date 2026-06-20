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
  captured_at: string;
}

export interface LiveHandle {
  close: () => void;
}

export function connectLive(onFix: (fix: LiveFix) => void): LiveHandle {
  const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
  const ws = new WebSocket(`${proto}//${window.location.host}/api/locations/live`);
  ws.onmessage = (ev) => {
    try {
      onFix(JSON.parse(ev.data as string) as LiveFix);
    } catch {
      // Ignore a malformed frame rather than tear down the live stream.
    }
  };
  return { close: () => ws.close() };
}
