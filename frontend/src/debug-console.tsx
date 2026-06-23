// Entry for the owner debug console (served at /debug-console.html, opened from
// the PWA's Debug-access screen). A separate entry from the owner app (main.tsx):
// it authenticates with a capability token, not the owner cookie, and registers
// no service worker / PWA — it is a throwaway debugging surface, not an app.

import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { Console } from "./debug-console/Console";
import { initFontScale } from "./fontScale";
import { initTheme } from "./theme";
import "./styles/tokens.css";
import "./styles/debug-console.css";

initTheme();
initFontScale();

const container = document.getElementById("root");
if (!container) throw new Error("Missing #root element");

createRoot(container).render(
  <StrictMode>
    <Console />
  </StrictMode>,
);
