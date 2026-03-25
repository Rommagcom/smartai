import React from "react";
import { createRoot } from "react-dom/client";
import App from "./App.jsx";
import "./styles.css";

const rootElement = document.getElementById("root");

function showBootError(title, details) {
  if (!rootElement) {
    return;
  }

  rootElement.innerHTML = `
    <div style="
      max-width: 900px;
      margin: 40px auto;
      padding: 16px;
      border-radius: 12px;
      background: #fff4f4;
      border: 1px solid #f0b4b4;
      color: #5a1b1b;
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
      white-space: pre-wrap;
      line-height: 1.4;
    ">
      <strong>${title}</strong>\n\n${String(details || "Unknown error")}
    </div>
  `;
}

window.addEventListener("error", (event) => {
  showBootError("Runtime Error", event.error?.stack || event.message);
});

window.addEventListener("unhandledrejection", (event) => {
  const reason = event.reason?.stack || event.reason?.message || String(event.reason);
  showBootError("Unhandled Promise Rejection", reason);
});

try {
  createRoot(rootElement).render(
    <React.StrictMode>
      <App />
    </React.StrictMode>
  );
} catch (error) {
  showBootError("Boot Error", error?.stack || error?.message || String(error));
}
