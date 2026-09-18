import { registerSW } from "virtual:pwa-register";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import { initFontScale } from "./fontScale";
import { GuidedIntakeApp } from "./intake/GuidedIntakeApp";
import { parseIntakePath } from "./intake/share";
import { parseSharePath } from "./jcode/share";
import { parseJlaunchSharePath } from "./jlaunch/share";
import { initLocationCapture } from "./location";
import { parseResearchSharePath } from "./research/share";
import { JcodeShareApp } from "./screens/JcodeShareApp";
import { JlaunchShareApp } from "./screens/JlaunchShareApp";
import { ResearchShareApp } from "./screens/ResearchShareApp";
import { armUpdateChecks } from "./swUpdate";
import { initTheme } from "./theme";
import "./styles/tokens.css";
import "./styles.css";

// Resolve the theme before first paint so there is no flash of wrong theme.
initTheme();
initFontScale();

// When the app asks whether a new build exists — `swUpdate` carries the reasoning, and
// the short version is that the hourly timer alone left the owner on the previous bundle
// for up to an hour after a deploy, reporting a defect that had already shipped.
registerSW({
  immediate: true,
  onRegisteredSW(_url, registration) {
    if (registration) armUpdateChecks(registration);
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
  if (parseResearchSharePath()) return <ResearchShareApp />;
  if (parseJlaunchSharePath()) return <JlaunchShareApp />;
  if (parseIntakePath()) return <GuidedIntakeApp />;
  // Owner app only. Warm the geolocation fix here — NOT at module load — so a public
  // share/intake recipient (a scoped surface above) is never prompted for location; only the
  // owner's own app, which can attach coordinates to a note, asks. (On by default; Settings toggle.)
  initLocationCapture();
  return <App />;
}

createRoot(container).render(<StrictMode>{pickRoot()}</StrictMode>);
