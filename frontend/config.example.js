// frontend/config.js — copy this file to config.js and edit it per
// deployment. config.js itself is gitignored (same convention as .env at
// the repo root) so a real deployment's API base URL / token is never
// committed.
//
// This is the ONE place the API base URL is configured. Nothing else in
// the frontend hardcodes a host — see js/api.js.
window.DASHBOARD_CONFIG = {
  // Local dev: the API run via `python -m api.app` (see docs/API.md),
  // defaults to http://127.0.0.1:8787.
  apiBaseUrl: "http://127.0.0.1:8787",

  // Production (once the API is deployed to a VPS behind Caddy — see
  // docs/DEPLOYMENT.md — this is NOT done by this slice):
  //   apiBaseUrl: "https://tradingbotapi.bhardwajvaibhav.com"
  // On Netlify this value is generated at deploy time by
  // netlify-build.sh from the site's own environment variables, not hand-
  // edited here — see docs/API.md "Netlify preparation".

  // The DASHBOARD_API_TOKEN configured on the API (see api/config.py /
  // docs/API.md). This dashboard is a single-operator tool behind a shared
  // secret, not a multi-user login system — see docs/API.md's "Security
  // assumptions" section for exactly what that does and does not protect
  // against, in particular that this token is visible to anyone who loads
  // this page's JS and must be treated accordingly.
  apiToken: "",

  // How often each view re-polls the API, in milliseconds.
  pollIntervalMs: 15000,
};
