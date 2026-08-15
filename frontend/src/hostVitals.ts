// The box's live GPU reading, behind the top bar's vitals trace.
//
// Two-step by necessity. EventSource cannot see a status code — a rejected stream
// surfaces only as an error, and it RETRIES on its own forever — so a family
// member (whom the owner-only ops router rejects) would otherwise sit in a silent
// reconnect loop for the whole session. So: probe once with a normal fetch, and
// open the stream only if the probe says this principal may read host telemetry.
//
// The stream is torn down whenever the app is backgrounded. A phone in a pocket has
// no business holding a socket open to read a gauge nobody is looking at, and the
// PWA already treats visibility as the signal for exactly this (see visibility.ts).

import { useEffect, useRef, useState } from "react";

import { ApiError, api } from "./api/client";
import { useForeground } from "./visibility";

/** How long to wait before re-probing after a probe that failed for a reason that
 *  might pass — a server mid-restart, a dropped network. A rejection (the principal
 *  may not read host telemetry) is never retried; see `allowed` below. */
const REPROBE_MS = 30_000;

type Access = "unknown" | "allowed" | "denied";

/** The host's GPU busy percent, refreshed once a second, or null when there is no
 *  reading to show — no gauge on the box, the stream not up, or the app in the
 *  background. Callers render an absent reading rather than a zero. */
export function useGpuBusy(): number | null {
  const foreground = useForeground();
  const [gpu, setGpu] = useState<number | null>(null);
  // A REF, not state: this outlives foreground flips (so returning to the app doesn't
  // re-probe a principal already answered) but must not re-run the effect when it
  // changes — learning "allowed" mid-probe would otherwise tear down the stream the
  // probe had just opened and wipe the reading it had just delivered.
  const access = useRef<Access>("unknown");

  useEffect(() => {
    if (!foreground || access.current === "denied") return;

    let cancelled = false;
    let source: EventSource | null = null;
    let reprobe: ReturnType<typeof setTimeout> | null = null;

    const open = (): void => {
      if (cancelled) return;
      source = api.opsVitalsStream();
      source.onmessage = (event: MessageEvent<string>) => {
        try {
          const frame = JSON.parse(event.data) as { gpu_busy_percent?: unknown };
          setGpu(reading(frame.gpu_busy_percent));
        } catch {
          // A malformed frame must not kill the stream — the next tick is a second away.
        }
      };
      // EventSource reconnects on its own; drop the stale reading so the trace shows a
      // gap rather than holding a number that stopped being true.
      source.onerror = () => setGpu(null);
    };

    const probe = async (): Promise<void> => {
      try {
        const vitals = await api.opsVitals();
        if (cancelled) return;
        access.current = "allowed";
        setGpu(reading(vitals.gpu_busy_percent));
        open();
      } catch (error) {
        if (cancelled) return;
        // 401/403 is this principal's standing answer — stop asking. Anything else
        // (offline, server restarting) may pass later, so try again on a slow timer.
        if (isRejection(error)) {
          access.current = "denied";
          return;
        }
        reprobe = setTimeout(() => void probe(), REPROBE_MS);
      }
    };

    if (access.current === "allowed") open();
    else void probe();

    return () => {
      cancelled = true;
      source?.close();
      if (reprobe !== null) clearTimeout(reprobe);
      setGpu(null);
    };
  }, [foreground]);

  return foreground ? gpu : null;
}

/** Coerce a reading to the `number | null` this hook promises. A missing or
 *  non-finite field must land as null, not sail through as `undefined`: callers
 *  test for null, and `undefined` passes that test — which rendered a literal NaN
 *  in the top bar the first time a response came back without the field. */
function reading(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

/** True when the failure means "not for you" rather than "not right now". Anything
 *  that isn't an outright rejection is treated as retryable, so a transport quirk can
 *  never permanently blind the meter. */
function isRejection(error: unknown): boolean {
  return error instanceof ApiError && (error.status === 401 || error.status === 403);
}
