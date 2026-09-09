// frontend/js/app.js — tab switching + the polling loop + the connection
// banner. This is the only file that decides WHEN to fetch; views.js
// decides WHAT to fetch and how to render it.

import { POLL_INTERVAL_MS, apiGet, apiBaseUrlForDisplay } from "./api.js";
import { renderOverview, renderTrading, renderPaper, renderResearch, renderStrategies } from "./views.js";

const VIEWS = {
  overview: renderOverview,
  trading: renderTrading,
  paper: renderPaper,
  research: renderResearch,
  strategies: renderStrategies,
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
  tick(); // load the newly selected tab immediately, don't wait for the next poll
}

function setupTabs() {
  document.querySelectorAll("nav.tabs button").forEach((btn) => {
    btn.addEventListener("click", () => setActiveView(btn.dataset.view));
  });
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

async function tick() {
  const renderFn = VIEWS[activeView];
  if (!renderFn) return;
  try {
    const results = await renderFn();
    updateBanner(results || []);
  } catch (e) {
    // A rendering bug must never take down the polling loop itself.
    console.error("view render failed", e);
    updateBanner([{ ok: false, error: "network" }]);
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
healthCheckOnLoad();
tick();
startPolling();
