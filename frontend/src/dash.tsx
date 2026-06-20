// Entry for the member dashboard (JBrain360) served at /dash, loaded inside the
// forked app's WebView. A separate entry from the owner app (main.tsx) so a
// member never receives the owner bundle — only the location-scoped surface.
// No PWA registration: this runs inside a native WebView, not as an installed app.

import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { initFontScale } from "./fontScale";
import { MemberDashboard } from "./screens/MemberDashboard";
import { initTheme } from "./theme";
import "./styles/tokens.css";
import "./styles.css";

// Resolve the theme before first paint so there is no flash of the wrong theme.
initTheme();
initFontScale();

const container = document.getElementById("root");
if (!container) throw new Error("Missing #root element");

createRoot(container).render(
  <StrictMode>
    <MemberDashboard />
  </StrictMode>,
);
