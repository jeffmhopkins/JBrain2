// Theme manager: system | dark | light, default system, persisted in
// localStorage. Applies [data-theme] on <html> and keeps the PWA
// theme-color meta in step so the browser chrome matches the app.

export type ThemePref = "system" | "dark" | "light";
export type ResolvedTheme = "dark" | "light";

const STORAGE_KEY = "jbrain.theme";

// Must match --bg in tokens.css for each theme.
const THEME_BG: Record<ResolvedTheme, string> = {
  dark: "#0e0f11",
  light: "#f7f7f5",
};

export function getThemePref(): ThemePref {
  const stored = localStorage.getItem(STORAGE_KEY);
  return stored === "dark" || stored === "light" ? stored : "system";
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
  // "system" clears the override so a future OS change wins again.
  if (pref === "system") localStorage.removeItem(STORAGE_KEY);
  else localStorage.setItem(STORAGE_KEY, pref);
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
