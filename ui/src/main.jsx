import React from "react";
import { createRoot } from "react-dom/client";
import App from "./App.jsx";
import "./styles/scroll-fix.css";

createRoot(document.getElementById("root")).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
);

// The service worker caches only the versioned app shell and same-origin
// static assets. API, auth, WebSocket, and third-party radar traffic always
// remain network-only so an offline shell cannot fabricate live state.
if ("serviceWorker" in navigator) {
  let reloadingForServiceWorker = false;
  navigator.serviceWorker.addEventListener("controllerchange", () => {
    if (reloadingForServiceWorker) return;
    reloadingForServiceWorker = true;
    window.location.reload();
  });

  window.addEventListener("load", () => {
    navigator.serviceWorker
      .register("/sw.js", { updateViaCache: "none" })
      .then((registration) => registration.update())
      .catch(() => {
        // Installation is an enhancement; Core connectivity is surfaced in-app.
      });
  });
}
