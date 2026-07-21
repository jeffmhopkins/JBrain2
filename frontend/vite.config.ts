/// <reference types="vitest/config" />
import { execSync } from "node:child_process";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";
import { VitePWA } from "vite-plugin-pwa";

// Stamp the build so a running install can be traced back to an exact commit — the only
// reliable way to tell which bundle a cached PWA is actually serving (shown in Settings).
// Best-effort: a build with no git still gets a timestamp.
function gitSha(): string {
  try {
    return execSync("git rev-parse --short HEAD", { stdio: ["ignore", "pipe", "ignore"] })
      .toString()
      .trim();
  } catch {
    return "unknown";
  }
}
const BUILD_SHA = gitSha();
const BUILD_TIME = new Date().toISOString();

export default defineConfig({
  // Entries built into one /srv/www: the owner app (index.html), the member
  // dashboard (dash.html, served at /dash inside the forked app's WebView), and
  // the owner debug console (debug-console.html, a token-authed standalone page).
  // Assets are shared and hash-named, so all pick up a new deploy.
  build: {
    rollupOptions: {
      input: {
        main: "index.html",
        dash: "dash.html",
        "debug-console": "debug-console.html",
      },
    },
  },
  define: {
    __BUILD_SHA__: JSON.stringify(BUILD_SHA),
    __BUILD_TIME__: JSON.stringify(BUILD_TIME),
  },
  plugins: [
    react(),
    VitePWA({
      registerType: "autoUpdate",
      manifest: {
        name: "JBrain",
        short_name: "JBrain",
        description: "Personal knowledge system",
        theme_color: "#0e0f11",
        background_color: "#0e0f11",
        display: "standalone",
        icons: [
          { src: "pwa-192x192.png", sizes: "192x192", type: "image/png" },
          { src: "pwa-512x512.png", sizes: "512x512", type: "image/png" },
          {
            src: "pwa-maskable-512x512.png",
            sizes: "512x512",
            type: "image/png",
            purpose: "maskable",
          },
        ],
      },
    }),
  ],
  test: {
    environment: "jsdom",
    setupFiles: ["src/test/setup.ts"],
  },
});
