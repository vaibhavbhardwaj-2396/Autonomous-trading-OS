// frontend/tests/test_frontend.mjs — plain Node, no test runner dependency
// (consistent with the frontend's own zero-npm-dependency design: see
// docs/API.md "Why no build tool"). Run with:
//
//   node frontend/tests/test_frontend.mjs
//
// Covers the two files with real logic worth testing: format.js's pure
// rendering helpers, and api.js's apiGet() response classification (network
// failure / 401 / other non-2xx / malformed JSON / success) under a mocked
// global fetch. views.js/app.js are DOM-orchestration glue over these and
// are covered instead by the manual browser smoke test described in
// docs/API.md — there is no headless DOM here to drive them against.

import assert from "node:assert/strict";

let passed = 0;
let failed = 0;

function check(name, cond, detail) {
  if (cond) {
    passed++;
    console.log(`  ✓ ${name}`);
  } else {
    failed++;
    console.log(`  ✗ ${name}${detail ? " — " + detail : ""}`);
  }
}

// ---------------------------------------------------------------------------
// format.js
// ---------------------------------------------------------------------------

const fmt = await import("../js/format.js");

check("money() formats a positive number with the rupee sign", fmt.money(1500) === "₹1,500");
check("money() formats a negative number with a leading minus, sign inside", fmt.money(-250) === "-₹250");
check("money() renders null as an em dash", fmt.money(null) === "—");
check("money() renders undefined as an em dash", fmt.money(undefined) === "—");
check("money() renders zero as ₹0, not an em dash (0 is a real value)", fmt.money(0) === "₹0");

check("num() renders null as an em dash", fmt.num(null) === "—");
check("num() rounds to the given digit count", fmt.num(1.2345, 2) === "1.23" || fmt.num(1.2345, 2) === "1.235");

check("pnlClass() is 'pos' for a positive number", fmt.pnlClass(10) === "pos");
check("pnlClass() is 'neg' for a negative number", fmt.pnlClass(-10) === "neg");
check("pnlClass() is '' for exactly zero", fmt.pnlClass(0) === "");
check("pnlClass() is '' for null/undefined (no false positive)", fmt.pnlClass(null) === "" && fmt.pnlClass(undefined) === "");

check("dt() renders a falsy value as an em dash", fmt.dt(null) === "—" && fmt.dt("") === "—");
check("dt() renders an invalid date string as itself, not a crash", fmt.dt("not-a-date") === "not-a-date");
check("dt() renders a valid ISO timestamp as a non-empty, different string", (() => {
  const out = fmt.dt("2026-09-09T09:20:00+05:30");
  return typeof out === "string" && out.length > 0 && out !== "2026-09-09T09:20:00+05:30";
})());

check("badge() returns empty string for a falsy label (no empty <span> in the DOM)", fmt.badge(null, "x") === "" && fmt.badge("", "x") === "");
check("badge() HTML-escapes its label", fmt.badge("<script>", "x").includes("&lt;script&gt;"));
check("badge() defaults its class to the lowercased text when no class is given", fmt.badge("DRAFT").includes('class="badge draft"'));

check("esc() escapes HTML special characters", fmt.esc('<b>"x"</b>').indexOf("<b>") === -1);
check("esc() renders null/undefined as empty string, not the literal word", fmt.esc(null) === "" && fmt.esc(undefined) === "");

// table() and errorState() write into a real DOM element — skipped here since
// there is no DOM in plain Node; they're covered by the manual browser smoke
// test (docs/API.md) instead, which is the more meaningful check for actual
// rendered HTML.

// --- broker truth / account freshness (PART A) --------------------------------
const FRESH_ACCT = {
  portfolio_value: 10250, expected_book_value: 10250, book_value_reconciled: true,
  account_value_status: "fresh", broker: { id: "indstocks", label: "INDmoney / INDstocks" },
  account_warnings: [],
};
const STALE_ACCT = {
  portfolio_value: 570000, expected_book_value: 10000, book_value_reconciled: false,
  book_value_note: "capital matches the whole brokerage account total",
  account_value_status: "stale", broker: { id: "indstocks", label: "INDmoney / INDstocks" },
  account_warnings: ["Broker account data is stale (last successful broker sync is 900h old). Showing the agent's last known book value, not a live INDmoney / INDstocks balance."],
};

check("accountIsStale() is false for a fresh account, true for a stale one",
  fmt.accountIsStale(FRESH_ACCT) === false && fmt.accountIsStale(STALE_ACCT) === true);
check("accountIsStale() is false when the field is absent (old API payload, no false alarm)",
  fmt.accountIsStale({}) === false);

check("brokerCard() always reports the active broker label",
  fmt.brokerCard(FRESH_ACCT).label === "INDmoney / INDstocks"
  && fmt.brokerCard(STALE_ACCT).label === "INDmoney / INDstocks");
check("brokerCard() is 'good' only when fresh, 'bad' when stale, and annotates the status",
  fmt.brokerCard(FRESH_ACCT).cls === "good" && fmt.brokerCard(FRESH_ACCT).text === "INDmoney / INDstocks"
  && fmt.brokerCard(STALE_ACCT).cls === "bad" && fmt.brokerCard(STALE_ACCT).text.includes("stale"));
check("brokerCard() degrades to 'broker unknown' with no crash on an empty payload",
  fmt.brokerCard({}).label === "broker unknown" && fmt.brokerCard(undefined).label === "broker unknown");

check("portfolioValueDisplay() shows the raw portfolio_value when the book value is reconciled",
  fmt.portfolioValueDisplay(FRESH_ACCT).value === fmt.money(10250)
  && fmt.portfolioValueDisplay(FRESH_ACCT).warn === false);
check("portfolioValueDisplay() falls back to expected_book_value (not the stale ₹5.7L) when NOT reconciled",
  fmt.portfolioValueDisplay(STALE_ACCT).value === fmt.money(10000)
  && fmt.portfolioValueDisplay(STALE_ACCT).warn === true
  && fmt.portfolioValueDisplay(STALE_ACCT).value !== fmt.money(570000));

check("accountStaleNotice() is empty for a fresh account",
  fmt.accountStaleNotice(FRESH_ACCT) === "");
check("accountStaleNotice() surfaces the API warning text for a stale account",
  fmt.accountStaleNotice(STALE_ACCT).includes("stale")
  && fmt.accountStaleNotice(STALE_ACCT).length > 0);

// ---------------------------------------------------------------------------
// api.js — apiGet()'s response classification under a mocked fetch
// ---------------------------------------------------------------------------

globalThis.window = {
  DASHBOARD_CONFIG: {
    apiBaseUrl: "http://example.invalid:8787",
    apiToken: "test-token-123",
    pollIntervalMs: 1000,
  },
};

let lastFetchCall = null;
function mockFetch(impl) {
  globalThis.fetch = async (url, opts) => {
    lastFetchCall = { url, opts };
    return impl(url, opts);
  };
}

const api = await import("../js/api.js");

// -- network failure --
mockFetch(() => {
  throw new Error("simulated network failure");
});
{
  const r = await api.apiGet("/account");
  check("apiGet(): a thrown fetch() (network down) -> {ok:false, error:'network'}, never throws",
    r.ok === false && r.error === "network");
}

// -- the Authorization header is set from config, on every request --
mockFetch(() => ({ ok: true, status: 200, json: async () => ({ hello: "world" }) }));
{
  await api.apiGet("/account");
  const authHeader = lastFetchCall.opts.headers.Authorization;
  check("apiGet(): sends 'Authorization: Bearer <configured token>'",
    authHeader === "Bearer test-token-123");
  check("apiGet(): requests API_BASE_URL + path, never a hardcoded host",
    lastFetchCall.url === "http://example.invalid:8787/account");
}

// -- 401 --
mockFetch(() => ({ ok: false, status: 401, json: async () => ({ error: "unauthorized" }) }));
{
  const r = await api.apiGet("/account");
  check("apiGet(): HTTP 401 -> {ok:false, status:401, error:'unauthorized'}",
    r.ok === false && r.status === 401 && r.error === "unauthorized");
}

// -- other non-2xx, with a detail message from the body --
mockFetch(() => ({ ok: false, status: 503, json: async () => ({ error: "service_unavailable", detail: "x is unavailable" }) }));
{
  const r = await api.apiGet("/risk");
  check("apiGet(): HTTP 503 -> {ok:false, status:503, error:'http', detail:<from body>}",
    r.ok === false && r.status === 503 && r.error === "http" && r.detail === "x is unavailable");
}

// -- 2xx but a body that isn't valid JSON --
mockFetch(() => ({
  ok: true,
  status: 200,
  json: async () => { throw new Error("not json"); },
}));
{
  const r = await api.apiGet("/account");
  check("apiGet(): a 2xx with an unparseable body -> {ok:false, error:'bad_response'}, never throws",
    r.ok === false && r.error === "bad_response");
}

// -- success --
mockFetch(() => ({ ok: true, status: 200, json: async () => ({ cash: 1000, as_of: "now" }) }));
{
  const r = await api.apiGet("/account");
  check("apiGet(): a 2xx with a valid JSON body -> {ok:true, data:<parsed body>}",
    r.ok === true && r.data.cash === 1000);
}

check("apiBaseUrlForDisplay() strips a trailing slash and reflects config, never localhost by default",
  api.apiBaseUrlForDisplay() === "http://example.invalid:8787");

console.log(`\n${passed} passed, ${failed} failed`);
process.exit(failed === 0 ? 0 : 1);
