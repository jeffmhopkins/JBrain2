import { useEffect, useState } from "react";
import type { FeedConfig, ImageAnalysisMode } from "../api/client";
import { api } from "../api/client";
import { FONT_SCALES, type FontScale, getFontScale, setFontScale } from "../fontScale";
import { isLocationCaptureEnabled, setLocationCaptureEnabled } from "../location";
import { type ThemePref, getThemePref, setThemePref } from "../theme";
import { TOKEN_RATES, type TokenRate, getTokenRate, setTokenRate } from "../tokenRate";

const THEME_OPTIONS: { value: ThemePref; label: string }[] = [
  { value: "system", label: "System" },
  { value: "dark", label: "Dark" },
  { value: "light", label: "Light" },
];

const IMAGE_ANALYSIS_OPTIONS: { value: ImageAnalysisMode; label: string }[] = [
  { value: "ocr", label: "ocr only" },
  { value: "full", label: "full analysis" },
];

interface SettingsScreenProps {
  deviceLabel: string;
  onLogout: () => void;
}

export function SettingsScreen({ deviceLabel, onLogout }: SettingsScreenProps) {
  const [theme, setTheme] = useState<ThemePref>(getThemePref);
  const [fontScale, setScale] = useState<FontScale>(getFontScale);
  const [tokenRate, setRate] = useState<TokenRate>(getTokenRate);
  const [locationOn, setLocationOn] = useState<boolean>(isLocationCaptureEnabled);
  // Inline confirm per DESIGN.md — no window.confirm for destructive acts.
  const [confirmingLogout, setConfirmingLogout] = useState(false);
  // Image analysis is the FIRST server-synced setting (GET/PUT /api/settings
  // over app.settings): the worker reads it, so it must follow the account.
  // Theme and text size deliberately stay device-local for now.
  const [imageMode, setImageMode] = useState<ImageAnalysisMode | null>(null);
  // The owner's display timezone — synced from this device's zone on app load
  // (App.tsx); shown read-only so the owner knows which zone their times render
  // in. Falls back to the browser's detected zone before the server answers.
  const browserZone = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  const [timezone, setTimezone] = useState<string>(browserZone);
  useEffect(() => {
    let stale = false;
    api
      .getSettings()
      .then((s) => {
        if (stale) return;
        setImageMode(s.image_analysis_mode);
        if (s.owner_timezone) setTimezone(s.owner_timezone);
      })
      .catch(() => {
        // Unreachable backend: show the default; a tap still tries to save.
        if (!stale) setImageMode("full");
      });
    return () => {
      stale = true;
    };
  }, []);

  // The read-only appointments ICS feed — a revocable subscribe URL the owner
  // hands to a calendar app. Server-held token; absent => the feed is off.
  const [feed, setFeed] = useState<FeedConfig | null>(null);
  const [copied, setCopied] = useState(false);
  useEffect(() => {
    let stale = false;
    api
      .feedConfig()
      .then((f) => {
        if (!stale) setFeed(f);
      })
      .catch(() => {
        if (!stale) setFeed({ enabled: false, token: null });
      });
    return () => {
      stale = true;
    };
  }, []);

  const feedUrl =
    feed?.token != null
      ? `${window.location.origin}/api/feed/appointments.ics?token=${feed.token}`
      : "";

  function generateFeed() {
    setCopied(false);
    void api
      .rotateFeed()
      .then(setFeed)
      .catch(() => {});
  }

  function disableFeed() {
    setCopied(false);
    void api
      .disableFeed()
      .then(() => setFeed({ enabled: false, token: null }))
      .catch(() => {});
  }

  function copyFeed() {
    if (feedUrl) {
      void navigator.clipboard?.writeText(feedUrl);
      setCopied(true);
    }
  }

  function pick(pref: ThemePref) {
    setThemePref(pref);
    setTheme(pref);
  }

  function pickImageMode(mode: ImageAnalysisMode) {
    setImageMode(mode); // optimistic — the sync dot reports trouble
    void api.updateSettings({ image_analysis_mode: mode }).catch(() => {});
  }

  return (
    <main className="screen-body settings">
      <section className="settings-card">
        <h2 className="settings-label">Theme</h2>
        <div className="theme-picker" aria-label="Theme">
          {THEME_OPTIONS.map((opt) => (
            <button
              key={opt.value}
              type="button"
              aria-pressed={theme === opt.value}
              className={`seg${theme === opt.value ? " seg-on" : ""}`}
              onClick={() => pick(opt.value)}
            >
              {opt.label}
            </button>
          ))}
        </div>
      </section>

      <section className="settings-card">
        <h2 className="settings-label">Text size</h2>
        <div className="theme-picker" aria-label="Text size">
          {FONT_SCALES.map((scale) => (
            <button
              key={scale}
              type="button"
              aria-pressed={fontScale === scale}
              className={`seg${fontScale === scale ? " seg-on" : ""}`}
              onClick={() => {
                setFontScale(scale);
                setScale(scale);
              }}
            >
              {scale}%
            </button>
          ))}
        </div>
      </section>

      <section className="settings-card">
        <h2 className="settings-label">Response typing speed</h2>
        <p className="settings-meta">
          how fast the assistant's answer types out, in tokens per second — the reveal is paced
          steadily so fast local models read as smooth typing rather than snapping in. Instant turns
          pacing off; the full answer shows the moment it lands.
        </p>
        <div className="theme-picker" aria-label="Response typing speed">
          {TOKEN_RATES.map((rate) => (
            <button
              key={rate}
              type="button"
              aria-pressed={tokenRate === rate}
              className={`seg${tokenRate === rate ? " seg-on" : ""}`}
              onClick={() => {
                setTokenRate(rate);
                setRate(rate);
              }}
            >
              {rate === 0 ? "Instant" : `${rate}/s`}
            </button>
          ))}
        </div>
      </section>

      <section className="settings-card">
        <h2 className="settings-label">Image analysis</h2>
        <p className="settings-meta">
          how much a vision model reads from attached images — ocr only transcribes the text
          verbatim; full analysis adds a salient description the fact pipeline mines. either way,
          capture never waits — vision runs after sync.
        </p>
        <div className="theme-picker" aria-label="Image analysis">
          {IMAGE_ANALYSIS_OPTIONS.map((opt) => (
            <button
              key={opt.value}
              type="button"
              aria-pressed={imageMode === opt.value}
              className={`seg${imageMode === opt.value ? " seg-on" : ""}`}
              onClick={() => pickImageMode(opt.value)}
            >
              {opt.label}
            </button>
          ))}
        </div>
      </section>

      <section className="settings-card">
        <h2 className="settings-label">Time zone</h2>
        <p className="settings-meta">
          appointment times and other dates render in this zone — synced automatically from this
          device, so the assistant's answers match the cards.
        </p>
        <div className="settings-value" aria-label="Time zone">
          {timezone}
        </div>
      </section>

      <section className="settings-card">
        <h2 className="settings-label">Capture location</h2>
        <p className="settings-meta">
          tags notes with where they were written — only when a fresh fix exists; capture never
          waits for GPS.
        </p>
        <div className="theme-picker" aria-label="Capture location">
          {[true, false].map((on) => (
            <button
              key={on ? "on" : "off"}
              type="button"
              aria-pressed={locationOn === on}
              className={`seg${locationOn === on ? " seg-on" : ""}`}
              onClick={() => {
                setLocationCaptureEnabled(on);
                setLocationOn(on);
              }}
            >
              {on ? "On" : "Off"}
            </button>
          ))}
        </div>
      </section>

      <section className="settings-card">
        <h2 className="settings-label">Calendar feed</h2>
        <p className="settings-meta">
          subscribe a calendar app to your appointments, read-only. the link carries appointment
          titles from every domain — including health and finance — off your box into whatever
          calendar subscribes, so keep it private; disable it to cut access instantly.
        </p>
        {feed?.enabled && feedUrl ? (
          <>
            <input
              className="feed-url"
              readOnly
              value={feedUrl}
              aria-label="Calendar feed URL"
              onFocus={(e) => e.currentTarget.select()}
            />
            <div className="settings-actions">
              <button type="button" className="seg" onClick={copyFeed}>
                {copied ? "Copied" : "Copy link"}
              </button>
              <button type="button" className="seg" onClick={generateFeed}>
                Regenerate
              </button>
              <button type="button" className="btn-destructive" onClick={disableFeed}>
                Disable
              </button>
            </div>
          </>
        ) : (
          <button type="button" className="seg" onClick={generateFeed} disabled={feed === null}>
            Generate link
          </button>
        )}
      </section>

      <section className="settings-card">
        <h2 className="settings-label">Session</h2>
        <p className="settings-meta">{deviceLabel}</p>
        <button
          type="button"
          className="btn-destructive"
          onClick={() => (confirmingLogout ? onLogout() : setConfirmingLogout(true))}
          onBlur={() => setConfirmingLogout(false)}
        >
          {confirmingLogout ? "Tap again to confirm" : "Log out"}
        </button>
      </section>
    </main>
  );
}
