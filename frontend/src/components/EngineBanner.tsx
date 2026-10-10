// The global engine banner: a full-width status strip under the top bar, on every screen
// that has one (DESIGN.md "Status banner"; binding mock
// docs/mocks/engine-switch/a-segmented-toggle.html).
//
// It exists because a switch or a failure changes what every local answer runs on, and the
// owner should never have to open Ops to learn that. Problems and transitions only: amber
// while something holds the engine (a switch, a fallback, a debug/perplexity job), rose after
// a rollback until dismissed. Flash-Next serving normally shows nothing — the owner removed
// that steel strip (2026-10-03); Ops → Engine says which engine serves. It reads the shared store
// only — the shell polls — so rendering a top bar never costs a request.

import {
  ENGINE_LABEL,
  type EngineSnapshot,
  dismissEngineSwitch,
  noEngineUp,
  openEngineCard,
  switchInFlight,
  switchSteps,
  useEngineSnapshot,
} from "../engineState";
import { XIcon } from "./icons";

export interface EngineBannerItem {
  key: string;
  tone: "steel" | "amber" | "rose";
  live?: boolean;
  title: string;
  detail: string;
  action: { label: string; run: () => void };
  dismiss?: () => void;
}

export function engineBanners(snap: EngineSnapshot): EngineBannerItem[] {
  const s = snap.state;
  if (s === null) return [];
  const out: EngineBannerItem[] = [];
  const sw = s.switch;
  const details = { label: "Details", run: () => openEngineCard() };
  if (switchInFlight(s) && sw !== null) {
    const step = switchSteps(sw.previous, sw.target, sw.model).find((x) => x.stage === sw.stage);
    out.push({
      key: "switching",
      tone: "amber",
      live: true,
      title: `Switching to ${ENGINE_LABEL[sw.target]}`,
      detail: `${step ? step.label.toLowerCase() : sw.stage} · local AI paused`,
      action: details,
    });
  } else if (noEngineUp(s)) {
    // Not dismissable: it is not news about a past switch but the box's present state, and
    // it clears itself the moment an engine is up again.
    out.push({
      key: "no-engine",
      tone: "rose",
      title: "No local engine is up",
      detail: "local models are unavailable — start it again from Ops → Engine",
      action: details,
    });
  } else if (
    sw !== null &&
    (sw.stage === "rolled_back" || sw.stage === "failed") &&
    !snap.dismissed.has(sw.id)
  ) {
    out.push({
      key: "rollback",
      tone: "rose",
      title:
        sw.stage === "rolled_back"
          ? `Switch to ${ENGINE_LABEL[sw.target]} failed`
          : "Engine switch failed",
      detail:
        sw.stage === "rolled_back"
          ? `rolled back to ${ENGINE_LABEL[s.effective]}`
          : `${ENGINE_LABEL[s.effective]} recorded as serving — check Ops`,
      action: details,
      dismiss: () => dismissEngineSwitch(sw.id),
    });
  } else if (s.desired !== s.effective) {
    out.push({
      key: "fallback",
      tone: "amber",
      title: `${ENGINE_LABEL[s.desired]} selected`,
      detail: `${ENGINE_LABEL[s.effective]} serving (fallback)`,
      action: details,
    });
  }
  if (s.perplexity_running || s.oneshot === "perplexity") {
    out.push({
      key: "perplexity",
      tone: "amber",
      live: true,
      title: "Perplexity test running",
      detail: "debug one-shot · engine busy",
      action: details,
    });
  } else if (s.admission.closed && !switchInFlight(s)) {
    // Admission closed with no switch of ours running: a debug job holds the engine.
    out.push({
      key: "admission",
      tone: "amber",
      live: true,
      title: "Local AI paused",
      detail: s.admission.reason ?? "a debug job holds the engine",
      action: details,
    });
  }
  // A strip read from a state the api can no longer confirm must say so, or a finished
  // switch keeps reading "Switching…" while the api restarts beneath it.
  const first = out[0];
  if (snap.error !== null && first) first.detail += " · can't reach the engine";
  return out;
}

export function EngineBanner() {
  const snap = useEngineSnapshot();
  const items = engineBanners(snap);
  if (items.length === 0) return null;
  return (
    <div className="engine-banners">
      {items.map((b) => (
        <div key={b.key} className={`engine-banner ${b.tone}`}>
          <span className={`engine-banner-dot${b.live ? " live" : ""}`} aria-hidden="true" />
          {/* The text is the live region, not the strip: a region that also holds its own
              controls is announced on every poll and is unusable with a screen reader. */}
          <output className="engine-banner-text">
            <b>{b.title}</b> <span>· {b.detail}</span>
          </output>
          <button type="button" className="engine-banner-act" onClick={b.action.run}>
            {b.action.label}
          </button>
          {b.dismiss && (
            <button
              type="button"
              className="engine-banner-x"
              aria-label="Dismiss"
              onClick={b.dismiss}
            >
              <XIcon size={16} />
            </button>
          )}
        </div>
      ))}
    </div>
  );
}
