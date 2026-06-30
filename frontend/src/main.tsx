import { registerSW } from "virtual:pwa-register";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import { initFontScale } from "./fontScale";
import { GuidedIntakeApp } from "./intake/GuidedIntakeApp";
import { parseIntakePath } from "./intake/share";
import { parseSharePath } from "./jcode/share";
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

// A share-link PATH (/jcode/s/{sid}, with or without the #t=token secret) mounts the
// scoped share app instead of the full owner app — the recipient sees only that one
// session. Matching on the path alone (not the secret) means a reload after the secret
// is stripped from the URL still opens the session via the redeemed cookie, rather than
// dropping to the owner login.
// A /jcode/s/{sid} path mounts the scoped code-share app; an /intake path mounts the
// guided-intake recipient stepper (it redeems the #t= fragment secret). Neither is the
// owner app — a recipient sees only that one scoped surface.
function pickRoot(): JSX.Element {
  if (parseSharePath()) return <JcodeShareApp />;
  if (parseIntakePath()) return <GuidedIntakeApp />;
  return <App />;
}

createRoot(container).render(<StrictMode>{pickRoot()}</StrictMode>);
