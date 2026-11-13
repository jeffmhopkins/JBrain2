// The creature opened in place: a full-screen layer over the GrowBot screen, so "on this phone"
// stays inside the app instead of leaving for a browser tab. Climbed by its back bar or the
// platform Back gesture (a back layer, like a Sheet); closing it ends the creature's session
// on this phone the way closing its tab did.

import type { TouchEvent } from "react";
import { useBackLayer } from "../backLayers";
import { ChevronLeftIcon, ExternalLinkIcon } from "./icons";

// What the creature asks for: camera and microphone to see and hear, wake lock so the screen
// stays on mid-walk, the motion sensors its policies steer by, and sound without a tap first.
// The creature's own Permissions-Policy grants these to itself; a frame gets them only when
// its parent delegates them here too.
const ALLOW =
  "camera; microphone; screen-wake-lock; fullscreen; autoplay; accelerometer; gyroscope; magnetometer";

// No top navigation: vendored code must not be able to send the app elsewhere (the new tab's
// opener was cut by hand for the same reason). allow-same-origin with allow-scripts is safe
// only because the creature is always another origin than the app.
const SANDBOX =
  "allow-scripts allow-same-origin allow-forms allow-modals allow-popups " +
  "allow-popups-to-escape-sandbox allow-downloads allow-pointer-lock";

export function GrowbotFrame({
  name,
  url,
  onClose,
  onBrowser,
}: {
  name: string;
  /** The one-time link; null while it is being minted. */
  url: string | null;
  onClose: () => void;
  /** Leave the app for a browser tab instead (the old way, kept for a page that needs it). */
  onBrowser: () => void;
}) {
  useBackLayer(onClose);
  // The card screen's own swipe-down would close the whole card under the creature.
  const keep = (e: TouchEvent) => e.stopPropagation();
  return (
    <section
      className="gb-frame"
      aria-label={`GrowBot — ${name}`}
      onTouchStart={keep}
      onTouchMove={keep}
    >
      <header className="top-bar gb-frame-bar">
        <button type="button" className="back-btn" onClick={onClose} aria-label="Close GrowBot">
          <ChevronLeftIcon size={22} />
          <span className="screen-title">{name}</span>
        </button>
        <button
          type="button"
          className="gb-frame-out"
          onClick={onBrowser}
          aria-label="Open in a browser tab"
          title="Open in a browser tab"
        >
          <ExternalLinkIcon size={18} />
        </button>
      </header>
      {url ? (
        <iframe
          className="gb-frame-view"
          title={`GrowBot — ${name}`}
          src={url}
          allow={ALLOW}
          allowFullScreen
          sandbox={SANDBOX}
          // The creature's /open takes a framed link only from the app's own origin, which
          // it reads here: an explicit policy, so a page-wide default can't hide it.
          referrerPolicy="origin"
        />
      ) : (
        <p className="gb-frame-wait">Opening {name}…</p>
      )}
    </section>
  );
}
