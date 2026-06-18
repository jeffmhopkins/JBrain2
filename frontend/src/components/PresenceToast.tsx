// L7b — the app-open presence toast (binding mock: docs/mocks/location-l7/
// option-c.html, "corner presence toast").
//
// A self-dismissing corner toast that rises on app/chat open showing the owner's
// OWN latest place + freshness. Teal (--location) "currently at" when fresh; amber
// (--warn) "last known" when stale — FRESHNESS-HONEST, never "here now" for an old
// fix. A single "open" action; auto-dismisses after a few seconds. NAMES + TIMES
// ONLY — no coordinate. Absent entirely when there is no usable fix.

import { useEffect, useState } from "react";
import { type LocationPresence, api } from "../api/client";

const AUTO_DISMISS_MS = 4200;

export interface PresenceToastDeps {
  loadPresence: () => Promise<LocationPresence>;
}

export function PresenceToast({
  deps,
  onOpen,
}: {
  deps?: PresenceToastDeps | undefined;
  onOpen?: (() => void) | undefined;
}) {
  const load = deps?.loadPresence ?? api.locationPresence;
  const [presence, setPresence] = useState<LocationPresence | null>(null);
  const [dismissed, setDismissed] = useState(false);

  useEffect(() => {
    let stale = false;
    load()
      .then((p) => {
        // Only a usable, present fix raises the toast — never an empty/absent one.
        if (!stale && p.present) setPresence(p);
      })
      .catch(() => {
        // Presence is low-stakes — a failed read simply shows no toast.
      });
    return () => {
      stale = true;
    };
  }, [load]);

  useEffect(() => {
    if (!presence || dismissed) return;
    const t = setTimeout(() => setDismissed(true), AUTO_DISMISS_MS);
    return () => clearTimeout(t);
  }, [presence, dismissed]);

  if (!presence || dismissed) return null;

  const stale = presence.stale;
  // An <output> is the semantic live-status region (implicit role="status"), so no
  // role override is needed — and the toast reads to assistive tech as it appears.
  return (
    <output className={`presence-toast${stale ? " stale" : " fresh"}`} aria-live="polite">
      <span className="presence-toast-dot" aria-hidden="true" />
      <span className="presence-toast-text">
        <b>{headline(presence)}</b>
        <span className="presence-toast-fresh">{freshness(presence)}</span>
      </span>
      <button
        type="button"
        className="presence-toast-action"
        onClick={() => {
          setDismissed(true);
          onOpen?.();
        }}
      >
        open
      </button>
    </output>
  );
}

/** Fresh → "Currently at <place>"; stale → "Last known: <place>" (never "here
 * now"). A fix outside every fence reports the state without a place name. */
function headline(p: LocationPresence): string {
  if (p.stale) {
    return p.place_name ? `Last known: ${p.place_name}` : "Last known position";
  }
  return p.place_name ? `Currently at: ${p.place_name}` : "Recent fix";
}

function freshness(p: LocationPresence): string {
  const ago = p.age_seconds === null ? "" : agoText(p.age_seconds);
  if (p.stale) return ago ? `${ago} · may have moved` : "may have moved";
  return ago ? `fix ${ago}` : "fix recent";
}

function agoText(seconds: number): string {
  const mins = Math.round(seconds / 60);
  if (mins < 1) return "just now";
  if (mins < 90) return `${mins} min ago`;
  return `${Math.round(seconds / 3600)} h ago`;
}

export { headline, freshness };
