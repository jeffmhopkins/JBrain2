// Theme manager: system | dark | light | dark-bright, default dark-bright (Dark+, the
// owner's standard), persisted in localStorage. Applies [data-theme] on <html> and keeps the PWA
// theme-color meta in step so the browser chrome matches the app.
// "dark-bright" is a dark variant that only lifts --border for stronger edges.

export type ThemePref = "system" | "dark" | "light" | "dark-bright";
export type ResolvedTheme = "dark" | "light" | "dark-bright";

const STORAGE_KEY = "jbrain.theme";

// What every surface — the app, dash, debug console, and every share link — opens in for
// a viewer who never picked a theme. A share recipient has nothing stored on this origin,
// so this is what decides how a shared conversation looks to them.
export const DEFAULT_THEME: ThemePref = "dark-bright";

// Must match --bg in tokens.css for each theme. dark-bright shares the dark bg.
const THEME_BG: Record<ResolvedTheme, string> = {
  dark: "#0e0f11",
  light: "#f7f7f5",
  "dark-bright": "#0e0f11",
};

export function getThemePref(): ThemePref {
  const stored = localStorage.getItem(STORAGE_KEY);
  return stored === "system" || stored === "dark" || stored === "light" || stored === "dark-bright"
    ? stored
    : DEFAULT_THEME;
}

export function resolveTheme(pref: ThemePref, systemDark: boolean): ResolvedTheme {
  if (pref === "system") return systemDark ? "dark" : "light";
  return pref;
}

function systemPrefersDark(): boolean {
  return window.matchMedia("(prefers-color-scheme: dark)").matches;
}

function apply(theme: ResolvedTheme): void {
  document.documentElement.dataset.theme = theme;
  let meta = document.head.querySelector<HTMLMetaElement>('meta[name="theme-color"]');
  if (!meta) {
    meta = document.createElement("meta");
    meta.name = "theme-color";
    document.head.appendChild(meta);
  }
  meta.content = THEME_BG[theme];
}

export function setThemePref(pref: ThemePref): void {
  // "system" is stored like any pick: clearing the key would fall back to DEFAULT_THEME.
  localStorage.setItem(STORAGE_KEY, pref);
  apply(resolveTheme(pref, systemPrefersDark()));
}

export function initTheme(): void {
  apply(resolveTheme(getThemePref(), systemPrefersDark()));
  window
    .matchMedia("(prefers-color-scheme: dark)")
    .addEventListener("change", (event: MediaQueryListEvent) => {
      if (getThemePref() === "system") apply(event.matches ? "dark" : "light");
    });
}
