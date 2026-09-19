// frontend/js/api.js — the ONLY place that talks to the network. Every view
// module calls apiGet(path) (or, for the two deliberate control actions,
// apiPost(path, body)) and gets back a plain result object; nothing else
// in this app constructs a URL, sets a header, or calls fetch() directly.
//
// outcome 2 note: apiPost exists for exactly two backend routes —
// POST /control/mode (RUNNING/PAUSED/SAFE_MODE/STOPPED) and POST /ai/config
// (provider/model selection) — both already validated, authenticated,
// narrow write routes on the API itself (see api/app.py's own module
// docstring). This file adds no new write capability of its own; it is
// still true that nothing here can place, size, or modify an order, or
// touch research/paper state.

const CFG = window.DASHBOARD_CONFIG || {};
const API_BASE_URL = (CFG.apiBaseUrl || "http://127.0.0.1:8787").replace(/\/+$/, "");
const API_TOKEN = CFG.apiToken || "";
export const POLL_INTERVAL_MS = CFG.pollIntervalMs || 15000;
let adminToken = "";
try { adminToken = sessionStorage.getItem("livingQuantAdminToken") || ""; } catch (_) {}

export function setAdminToken(token) {
  adminToken = String(token || "").trim();
  try {
    if (adminToken) sessionStorage.setItem("livingQuantAdminToken", adminToken);
    else sessionStorage.removeItem("livingQuantAdminToken");
  } catch (_) {}
}

export function hasAdminToken() { return Boolean(adminToken); }

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
export async function apiGet(path, options = {}) {
  let resp;
  try {
    resp = await fetch(API_BASE_URL + path, {
      method: "GET",
      headers: (options.admin ? adminToken : API_TOKEN)
        ? { Authorization: `Bearer ${options.admin ? adminToken : API_TOKEN}` }
        : {},
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

/**
 * POST `${API_BASE_URL}${path}` with a JSON body. Same result shape as
 * apiGet(). Used ONLY by the two control-action forms (organism mode,
 * AI provider) — see this file's own header comment.
 */
export async function apiPost(path, body, options = {}) {
  let resp;
  try {
    resp = await fetch(API_BASE_URL + path, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        ...((options.admin ? adminToken : API_TOKEN)
          ? { Authorization: `Bearer ${options.admin ? adminToken : API_TOKEN}` }
          : {}),
      },
      body: JSON.stringify(body || {}),
    });
  } catch (e) {
    return { ok: false, status: 0, error: "network" };
  }

  let responseBody = null;
  try {
    responseBody = await resp.json();
  } catch (e) {
    // fall through with responseBody still null
  }

  if (resp.status === 401) {
    return { ok: false, status: 401, error: "unauthorized" };
  }
  if (!resp.ok) {
    return {
      ok: false,
      status: resp.status,
      error: "http",
      detail: (responseBody && (responseBody.detail || responseBody.error)) || resp.statusText,
    };
  }
  if (responseBody === null) {
    return { ok: false, status: resp.status, error: "bad_response" };
  }
  return { ok: true, data: responseBody };
}

export function apiBaseUrlForDisplay() {
  return API_BASE_URL;
}
