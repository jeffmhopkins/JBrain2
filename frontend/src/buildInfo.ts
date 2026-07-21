// The commit + timestamp this bundle was built from, injected by Vite's `define`
// (see vite.config.ts). Surfaced in Settings so a cached PWA can be traced to an exact
// build — the only reliable way to tell which code a device is actually running.

declare const __BUILD_SHA__: string;
declare const __BUILD_TIME__: string;

export const BUILD_SHA: string = typeof __BUILD_SHA__ === "string" ? __BUILD_SHA__ : "dev";
export const BUILD_TIME: string = typeof __BUILD_TIME__ === "string" ? __BUILD_TIME__ : "";
