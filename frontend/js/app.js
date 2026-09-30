// frontend/js/app.js — tab switching + the polling loop + the connection
// banner. This is the only file that decides WHEN to fetch; views.js
// decides WHAT to fetch and how to render it.

import { POLL_INTERVAL_MS, apiGet, apiBaseUrlForDisplay } from "./api.js";
import {
  renderOverview, renderTrading, renderPaper, renderResearch, renderStrategies,
  renderValidation, renderControl, renderArtifacts,
  renderSystem,
} from "./views.js";

const VIEWS = {
  overview: renderOverview,
  trading: renderTrading,
  paper: renderPaper,
  research: renderResearch,
  strategies: renderStrategies,
  validation: renderValidation,
  system: renderSystem,
  control: renderControl,
  artifacts: renderArtifacts,
};

let activeView = "overview";
let pollTimer = null;
let lastGoodAt = null;

function setActiveView(name) {
  activeView = name;
  document.querySelectorAll("nav.tabs button").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.view === name);
  });
  document.querySelectorAll("main .view").forEach((sec) => {
    sec.classList.toggle("active", sec.id === `view-${name}`);
  });
  tick({ force: true }); // load the newly selected tab immediately
}

function setupTabs() {
  document.querySelectorAll("nav.tabs button").forEach((btn) => {
    btn.addEventListener("click", () => setActiveView(btn.dataset.view));
  });
}

function setupActions() {
  document.addEventListener("click", (event) => {
    const target = event.target.closest("[data-navigate]");
    if (target) setActiveView(target.dataset.navigate);
  });
  document.getElementById("refresh-now")?.addEventListener("click", () => tick({ force: true }));
}

function updateBanner(results) {
  const banner = document.getElementById("connection-banner");
  const anyOk = results.some((r) => r.ok);
  const anyUnauthorized = results.some((r) => r.error === "unauthorized");
  const anyNetwork = results.some((r) => r.error === "network");

  if (anyOk) lastGoodAt = new Date();

  if (anyUnauthorized) {
    banner.className = "show error";
    banner.textContent =
      "Unauthorized — the API token in config.js is missing or wrong for this API.";
  } else if (anyNetwork && !anyOk) {
    banner.className = "show error";
    banner.textContent = `Cannot reach the API at ${apiBaseUrlForDisplay()}. Is it running?`;
  } else if (!anyOk) {
    banner.className = "show error";
    banner.textContent = "The API responded with an error on every endpoint for this page.";
  } else if (results.some((r) => !r.ok)) {
    banner.className = "show stale";
    banner.textContent = "Some data on this page could not be refreshed — showing the last good values where available.";
  } else {
    banner.className = "";
    banner.textContent = "";
  }

  const lastUpdatedEl = document.getElementById("last-updated");
  lastUpdatedEl.textContent = lastGoodAt
    ? `updated ${lastGoodAt.toLocaleTimeString("en-IN")}`
    : "no successful update yet";
}

async function tick(options) {
  const force = options && options.force === true;
  // The Admin view contains multi-field write forms. Re-rendering it every
  // polling interval destroys the operator's unsaved input, so it refreshes
  // only when entered, after a completed action, or via the Refresh button.
  if (activeView === "control" && !force) return;
  const renderFn = VIEWS[activeView];
  if (!renderFn) return;
  try {
    const results = await renderFn();
    updateBanner(results || []);
  } catch (e) {
    // A rendering bug must never take down the polling loop itself.
    console.error("view render failed", e);
    const banner = document.getElementById("connection-banner");
    banner.className = "show error";
    banner.textContent = "This page could not finish rendering. The API may still be healthy; reload the dashboard and check the browser console.";
  }
}

async function healthCheckOnLoad() {
  const health = await apiGet("/health");
  if (!health.ok) {
    updateBanner([{ ok: false, error: health.error }]);
  }
}

function startPolling() {
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(tick, POLL_INTERVAL_MS);
}

setupTabs();
setupActions();
healthCheckOnLoad();
tick();
startPolling();
