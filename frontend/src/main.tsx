import { registerSW } from "virtual:pwa-register";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import { initFontScale } from "./fontScale";
import { initTheme } from "./theme";
import "./styles/tokens.css";
import "./styles.css";

// Resolve the theme before first paint so there is no flash of wrong theme.
initTheme();
initFontScale();

// autoUpdate handles relaunches; the hourly check covers a PWA left open for
// days, so it still converges on the latest deploy without a restart.
const UPDATE_CHECK_MS = 60 * 60 * 1000;
registerSW({
  immediate: true,
  onRegisteredSW(_url, registration) {
    if (registration) {
      setInterval(() => void registration.update(), UPDATE_CHECK_MS);
    }
  },
});

const container = document.getElementById("root");
if (!container) throw new Error("Missing #root element");

createRoot(container).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
