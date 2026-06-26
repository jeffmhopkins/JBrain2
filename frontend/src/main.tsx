import { registerSW } from "virtual:pwa-register";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import { initFontScale } from "./fontScale";
import { parseShareLink } from "./jcode/share";
import { initLocationCapture } from "./location";
import { JcodeShareApp } from "./screens/JcodeShareApp";
import { initTheme } from "./theme";
import { isForeground } from "./visibility";
import "./styles/tokens.css";
import "./styles.css";

// Resolve the theme before first paint so there is no flash of wrong theme.
initTheme();
initFontScale();
// Warm geolocation fix (on by default; Settings toggle) so sends can attach
// coordinates without ever waiting on GPS.
initLocationCapture();

// autoUpdate handles relaunches; the hourly check covers a PWA left open for
// days, so it still converges on the latest deploy without a restart. A
// backgrounded app skips the check — it reaches the server the next hour it is
// foreground, so a hidden tab never wakes to hit the server.
const UPDATE_CHECK_MS = 60 * 60 * 1000;
registerSW({
  immediate: true,
  onRegisteredSW(_url, registration) {
    if (registration) {
      setInterval(() => {
        if (isForeground()) void registration.update();
      }, UPDATE_CHECK_MS);
    }
  },
});

const container = document.getElementById("root");
if (!container) throw new Error("Missing #root element");

// A share link (/jcode/s/{sid}#t=token) mounts the scoped share app instead of the
// full owner app — the recipient sees only that one session.
createRoot(container).render(
  <StrictMode>{parseShareLink() ? <JcodeShareApp /> : <App />}</StrictMode>,
);
