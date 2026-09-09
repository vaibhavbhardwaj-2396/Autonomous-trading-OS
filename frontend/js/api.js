// frontend/js/api.js — the ONLY place that talks to the network. Every view
// module calls apiGet(path) and gets back a plain result object; nothing
// else in this app constructs a URL, sets a header, or calls fetch()
// directly. GET only — this dashboard has no write path, on purpose.

const CFG = window.DASHBOARD_CONFIG || {};
const API_BASE_URL = (CFG.apiBaseUrl || "http://127.0.0.1:8787").replace(/\/+$/, "");
const API_TOKEN = CFG.apiToken || "";
export const POLL_INTERVAL_MS = CFG.pollIntervalMs || 15000;

/**
 * GET `${API_BASE_URL}${path}`. Never throws — every failure mode (network
 * down, non-2xx, malformed JSON) comes back as {ok: false, ...} so a view
 * can render a clear inline error instead of the whole dashboard crashing.
 *
 * Returns one of:
 *   {ok: true, data}
 *   {ok: false, status: 0, error: "network"}               - fetch itself failed (API unreachable)
 *   {ok: false, status: 401, error: "unauthorized"}         - bad/missing token
 *   {ok: false, status, error: "http", detail}              - any other non-2xx
 *   {ok: false, status, error: "bad_response"}               - 2xx but not valid JSON
 */
export async function apiGet(path) {
  let resp;
  try {
    resp = await fetch(API_BASE_URL + path, {
      method: "GET",
      headers: API_TOKEN ? { Authorization: `Bearer ${API_TOKEN}` } : {},
    });
  } catch (e) {
    return { ok: false, status: 0, error: "network" };
  }

  let body = null;
  try {
    body = await resp.json();
  } catch (e) {
    // fall through with body still null
  }

  if (resp.status === 401) {
    return { ok: false, status: 401, error: "unauthorized" };
  }
  if (!resp.ok) {
    return {
      ok: false,
      status: resp.status,
      error: "http",
      detail: (body && (body.detail || body.error)) || resp.statusText,
    };
  }
  if (body === null) {
    return { ok: false, status: resp.status, error: "bad_response" };
  }
  return { ok: true, data: body };
}

export function apiBaseUrlForDisplay() {
  return API_BASE_URL;
}
