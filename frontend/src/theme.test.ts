import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { getThemePref, initTheme, resolveTheme, setThemePref } from "./theme";

type ChangeListener = (event: { matches: boolean }) => void;

function stubMatchMedia(dark: boolean) {
  const listeners: ChangeListener[] = [];
  vi.stubGlobal("matchMedia", (query: string) => ({
    matches: query.includes("dark") ? dark : false,
    media: query,
    addEventListener: (_type: string, listener: ChangeListener) => listeners.push(listener),
    removeEventListener: () => {},
  }));
  return {
    fireChange: (matches: boolean) => {
      for (const listener of listeners) listener({ matches });
    },
  };
}

describe("theme manager", () => {
  beforeEach(() => {
    localStorage.clear();
    document.documentElement.removeAttribute("data-theme");
    document.head.querySelector('meta[name="theme-color"]')?.remove();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("resolves system preference when no override is stored", () => {
    expect(resolveTheme("system", true)).toBe("dark");
    expect(resolveTheme("system", false)).toBe("light");
    expect(resolveTheme("dark", false)).toBe("dark");
    expect(resolveTheme("light", true)).toBe("light");
    expect(resolveTheme("dark-bright", false)).toBe("dark-bright");
  });

  it("defaults to system and persists explicit picks", () => {
    stubMatchMedia(true);
    expect(getThemePref()).toBe("system");

    setThemePref("light");
    expect(getThemePref()).toBe("light");
    expect(localStorage.getItem("jbrain.theme")).toBe("light");

    // Back to system clears the override entirely.
    setThemePref("system");
    expect(localStorage.getItem("jbrain.theme")).toBeNull();
  });

  it("applies data-theme and the theme-color meta", () => {
    stubMatchMedia(true);
    setThemePref("light");
    expect(document.documentElement.dataset.theme).toBe("light");
    const meta = document.head.querySelector<HTMLMetaElement>('meta[name="theme-color"]');
    expect(meta?.content).toBe("#f7f7f5");

    setThemePref("dark");
    expect(document.documentElement.dataset.theme).toBe("dark");
    expect(meta?.content).toBe("#0e0f11");

    // Dark+ shares the dark background but is its own data-theme.
    setThemePref("dark-bright");
    expect(document.documentElement.dataset.theme).toBe("dark-bright");
    expect(meta?.content).toBe("#0e0f11");
    expect(localStorage.getItem("jbrain.theme")).toBe("dark-bright");
  });

  it("follows OS changes only while the pref is system", () => {
    const media = stubMatchMedia(false);
    initTheme();
    expect(document.documentElement.dataset.theme).toBe("light");

    media.fireChange(true);
    expect(document.documentElement.dataset.theme).toBe("dark");

    setThemePref("light");
    media.fireChange(false);
    media.fireChange(true);
    expect(document.documentElement.dataset.theme).toBe("light");
  });
});
