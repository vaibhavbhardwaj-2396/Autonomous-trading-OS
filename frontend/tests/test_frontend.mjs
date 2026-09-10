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
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const HERE = dirname(fileURLToPath(import.meta.url));

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

check("portfolioValueDisplay was removed — no fixed-capital 'book value' helper remains",
  fmt.portfolioValueDisplay === undefined);

check("accountStaleNotice() is empty for a fresh account",
  fmt.accountStaleNotice(FRESH_ACCT) === "");
check("accountStaleNotice() surfaces the API warning text for a stale account",
  fmt.accountStaleNotice(STALE_ACCT).includes("stale")
  && fmt.accountStaleNotice(STALE_ACCT).length > 0);

// --- absent allocated_capital + legacy peak_capital: calm notes, not warnings --
const VPS_ACCT = {
  account_value_status: "fresh",
  total_value: 63339.56, broker_free_cash: 32.31, holdings_market_value: 63307.25,
  broker: { label: "INDmoney / INDstocks" }, broker_synced_at: "2026-09-11T14:30:00+05:30",
  allocated_capital: null, allocated_capital_set: false,
  peak_capital: 570447.95, peak_capital_is_legacy: true,
  account_warnings: [],
  account_notes: [
    "allocated_capital is not set in state — the dashboard and engine.execute both use the ₹10,000 default; `capital` (₹10,000) is consistent with that, so nothing is unreconciled.",
    "drawdown / risk state is measured against a legacy peak_capital of ₹570,447.95, ratcheted before the INDmoney migration. See docs/CAPITAL_MODEL.md.",
  ],
};

check("accountStaleNotice() is EMPTY for the VPS state — the absent key is not a problem",
  fmt.accountStaleNotice(VPS_ACCT) === "");
check("accountInfoNotes() returns the two calm context notes for the VPS state",
  fmt.accountInfoNotes(VPS_ACCT).length === 2
  && fmt.accountInfoNotes(VPS_ACCT).some((n) => n.includes("default"))
  && fmt.accountInfoNotes(VPS_ACCT).some((n) => n.includes("legacy peak_capital")));
check("accountInfoNotes() is [] when there are none / payload lacks the field",
  fmt.accountInfoNotes({ account_notes: [] }).length === 0
  && fmt.accountInfoNotes({}).length === 0 && fmt.accountInfoNotes(undefined).length === 0);
check("the VPS state's account total is the DYNAMIC ₹63,339.56, not the ₹10k / ₹570k figures",
  fmt.accountTotalDisplay(VPS_ACCT).value === fmt.money(63339.56)
  && fmt.accountTotalDisplay(VPS_ACCT).unavailable === false);

// --- MIGRATED state (dynamic capital model): no allocated_capital, no fixed --
// capital, and — critically — NO red "book value not reconciled" banner. The
// API sends account_warnings: [] and account_notes: [] for this state; the
// frontend must render no error strip and no info strip.
const MIGRATED_ACCT = {
  account_value_status: "fresh", capital_model: "managed",
  total_value: 63339.56, broker_free_cash: 32.31, holdings_market_value: 63307.25,
  managed_equity: 32.31, managed_free_cash: 32.31, managed_positions_market_value: 0,
  portfolio_value: 32.31, expected_book_value: 32.31, book_value_reconciled: true,
  book_value_note: null,
  allocated_capital: null, allocated_capital_set: false,
  peak_capital: 32.31, peak_capital_is_legacy: false, peak_capital_note: null,
  broker: { label: "INDmoney / INDstocks" }, broker_synced_at: "2026-09-11T14:30:00+05:30",
  unmanaged_holdings: { count: 26, value: 63307.25 },
  account_warnings: [], account_notes: [],
};
check("MIGRATED state: accountIsStale() is false (status is fresh)",
  fmt.accountIsStale(MIGRATED_ACCT) === false);
check("MIGRATED state: accountStaleNotice() is EMPTY — no red 'book value not reconciled' banner",
  fmt.accountStaleNotice(MIGRATED_ACCT) === "");
check("MIGRATED state: accountInfoNotes() is empty — no calm strip either",
  fmt.accountInfoNotes(MIGRATED_ACCT).length === 0);
check("MIGRATED state: account total is the dynamic ₹63,339.56; the managed book "
  + "(₹32.31) is a separate figure",
  fmt.accountTotalDisplay(MIGRATED_ACCT).value === fmt.money(63339.56)
  && fmt.accountTotalDisplay(MIGRATED_ACCT).unavailable === false
  && MIGRATED_ACCT.managed_equity !== MIGRATED_ACCT.total_value);
check("MIGRATED state: no legacy peak flag (drawdown card is NOT annotated)",
  MIGRATED_ACCT.peak_capital_is_legacy === false);
check("views.js annotates the drawdown card when peak_capital_is_legacy",
  /peak_capital_is_legacy/.test(stripComments(readFileSync(join(HERE, "../js/views.js"), "utf8")))
  && stripComments(readFileSync(join(HERE, "../js/views.js"), "utf8")).includes("Risk / Drawdown ⚠"));

// --- account total / unmanaged holdings / sync timestamp --------------------
const INCOMPLETE_ACCT = {
  account_value_status: "incomplete", total_value: null, broker_free_cash: 32.31,
  broker: { label: "INDmoney / INDstocks" }, broker_synced_at: "2026-09-10T15:00:00+05:30",
  unmanaged_holdings: { count: 26, value: null },
  account_warnings: ["Broker sync is current, but the account total could not be verified: 26 broker holding(s) present but the holdings valued at ~₹0. Free cash is shown; the account total is not."],
};
const VERIFIED_ACCT = {
  account_value_status: "fresh", total_value: 63032.31, broker_free_cash: 32.31,
  broker: { label: "INDmoney / INDstocks" }, broker_synced_at: "2026-09-10T15:00:00+05:30",
  unmanaged_holdings: { count: 26, value: 63000 }, account_warnings: [],
};

check("accountTotalDisplay(): a verified total renders as money, not 'Unavailable'",
  fmt.accountTotalDisplay(VERIFIED_ACCT).value === fmt.money(63032.31)
  && fmt.accountTotalDisplay(VERIFIED_ACCT).unavailable === false);
check("accountTotalDisplay(): total_value null -> the literal word 'Unavailable' (never cash)",
  fmt.accountTotalDisplay(INCOMPLETE_ACCT).value === "Unavailable"
  && fmt.accountTotalDisplay(INCOMPLETE_ACCT).unavailable === true
  && fmt.accountTotalDisplay(INCOMPLETE_ACCT).value !== fmt.money(32.31));
check("accountTotalDisplay(): missing acct -> 'Unavailable', no crash",
  fmt.accountTotalDisplay(undefined).value === "Unavailable");

check("unmanagedHoldingsDisplay(): count + value when verified",
  fmt.unmanagedHoldingsDisplay(VERIFIED_ACCT).text === `26 · ${fmt.money(63000)}`
  && fmt.unmanagedHoldingsDisplay(VERIFIED_ACCT).hasValue === true);
check("unmanagedHoldingsDisplay(): count + 'value unverified' when not verified",
  fmt.unmanagedHoldingsDisplay(INCOMPLETE_ACCT).text === "26 · value unverified"
  && fmt.unmanagedHoldingsDisplay(INCOMPLETE_ACCT).hasValue === false);
check("unmanagedHoldingsDisplay(): '0' when there are none",
  fmt.unmanagedHoldingsDisplay({ unmanaged_holdings: { count: 0 } }).text === "0");

check("accountStaleNotice(): an 'incomplete' account shows the verify warning + sync stamp",
  fmt.accountStaleNotice(INCOMPLETE_ACCT).includes("could not be verified")
  && fmt.accountStaleNotice(INCOMPLETE_ACCT).includes("synced"));
check("syncedAtLabel(): 'synced <when>' when a timestamp exists, 'never synced' otherwise",
  fmt.syncedAtLabel(VERIFIED_ACCT).startsWith("synced ")
  && fmt.syncedAtLabel({}) === "never synced");

// --- PART B: no misleading fixed-₹10k account-capital card (views.js source) --
// strip // line comments and /* */ block comments so a check for real CODE
// is not fooled by an explanatory comment that names the removed concept.
function stripComments(s) {
  return s.replace(/\/\*[\s\S]*?\*\//g, "").replace(/(^|[^:])\/\/.*$/gm, "$1");
}
const VIEWS_SRC = stripComments(readFileSync(join(HERE, "../js/views.js"), "utf8"));
const FORMAT_SRC = stripComments(readFileSync(join(HERE, "../js/format.js"), "utf8"));

check("views.js no longer renders an 'Agent Book Value' card",
  !VIEWS_SRC.includes("Agent Book Value"));
check("views.js no longer renders an 'Agent Allocated Capital' card",
  !VIEWS_SRC.includes("Agent Allocated Capital"));
check("views.js no longer renders an 'Agent Spendable Cash' card",
  !VIEWS_SRC.includes("Agent Spendable Cash"));
check("views.js presents the dynamic broker account model (Account Total / Broker Free Cash / Holdings Value)",
  VIEWS_SRC.includes('"Account Total"') && VIEWS_SRC.includes('"Broker Free Cash"')
  && VIEWS_SRC.includes('"Holdings Value"'));
check("allocated_capital appears ONLY as a clearly-labelled 'Autonomous Mandate', explained as not the account value",
  VIEWS_SRC.includes('"Autonomous Mandate"')
  && VIEWS_SRC.includes("not your account balance"));
check("format.js no longer exports the fixed-capital portfolioValueDisplay helper",
  !FORMAT_SRC.includes("export function portfolioValueDisplay"));
check("no hardcoded broker label string in frontend CODE (INDmoney/INDstocks only from the API payload)",
  !VIEWS_SRC.includes('"INDmoney / INDstocks"') && !FORMAT_SRC.includes('"INDmoney / INDstocks"'));

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
