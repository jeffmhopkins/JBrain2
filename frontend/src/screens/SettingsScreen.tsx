import { useState } from "react";
import { type ThemePref, getThemePref, setThemePref } from "../theme";

const THEME_OPTIONS: { value: ThemePref; label: string }[] = [
  { value: "system", label: "System" },
  { value: "dark", label: "Dark" },
  { value: "light", label: "Light" },
];

interface SettingsScreenProps {
  deviceLabel: string;
  onLogout: () => void;
}

export function SettingsScreen({ deviceLabel, onLogout }: SettingsScreenProps) {
  const [theme, setTheme] = useState<ThemePref>(getThemePref);
  // Inline confirm per DESIGN.md — no window.confirm for destructive acts.
  const [confirmingLogout, setConfirmingLogout] = useState(false);

  function pick(pref: ThemePref) {
    setThemePref(pref);
    setTheme(pref);
  }

  return (
    <main className="screen-body settings">
      <section className="settings-card">
        <h2 className="settings-label">Theme</h2>
        <div className="theme-picker" role="radiogroup" aria-label="Theme">
          {THEME_OPTIONS.map((opt) => (
            <button
              key={opt.value}
              type="button"
              role="radio"
              aria-checked={theme === opt.value}
              className={`seg${theme === opt.value ? " seg-on" : ""}`}
              onClick={() => pick(opt.value)}
            >
              {opt.label}
            </button>
          ))}
        </div>
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
