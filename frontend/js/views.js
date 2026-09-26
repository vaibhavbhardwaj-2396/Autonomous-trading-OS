// frontend/js/views.js — one render function per tab. Each fetches only
// the endpoints that tab needs, renders what it got, and reports success
// per-endpoint back to app.js (which uses that to decide the connection
// banner) — one endpoint failing never stops the others on the same page
// from rendering.

import { apiGet, apiPost, setAdminToken, hasAdminToken } from "./api.js";
import {
  esc, money, num, pnlClass, dt, badge, table, errorState,
  brokerCard, accountStaleNotice, accountInfoNotes,
  accountTotalDisplay, accountSubtotalDisplay, unmanagedHoldingsDisplay, discoveryAdmissionDisplay,
} from "./format.js";

function el(id) {
  return document.getElementById(id);
}

/** Fetch one endpoint into one container, calling render(data, container)
 * on success. Returns {ok, status} for the caller's connection tracking. */
async function load(path, containerId, render, failureMessage) {
  const container = el(containerId);
  const result = await apiGet(path);
  if (!result.ok) {
    errorState(
      container,
      result.error === "unauthorized"
        ? "Not authorized — check the API token in config.js."
        : result.error === "network"
        ? `${failureMessage} (API unreachable).`
        : `${failureMessage} (${result.detail || result.error}).`
    );
    return { ok: false, status: result.status, error: result.error };
  }
  render(result.data, container);
  return { ok: true, data: result.data };
}

// --------------------------------------------------------------------------
// Overview
// --------------------------------------------------------------------------

export async function renderOverview() {
  const results = [];

  const [acctRes, riskRes, regimeRes, runtimeRes] = await Promise.all([
    apiGet("/account"),
    apiGet("/risk"),
    apiGet("/regime"),
    apiGet("/runtime/status"),
  ]);
  if (runtimeRes.ok) {
    el("runtime-status-bar").innerHTML = (runtimeRes.data.indicators || []).map((s) => `<button class="status-pill" data-navigate="${esc(s.target)}" title="${esc(s.detail || '')}"><strong>${esc(s.label)}</strong><span>${esc(s.state)}</span><small>${esc(s.detail || '')}</small></button>`).join("");
    el("active-work").innerHTML = `<ul class="active-work-list">${(runtimeRes.data.active_work || []).map((x) => `<li>${esc(x)}</li>`).join("")}</ul>`;
    const labels = {observations:"OBSERVE", hypotheses:"HYPOTHESES", reported_experiments:"EXPERIMENTS", evidence:"ACCEPTED EVIDENCE", strategy_versions:"STRATEGIES", paper_trades:"PAPER"};
    el("overview-flow").innerHTML = Object.entries(labels).map(([key,label], i) => `${i ? '<span class="flow-arrow">→</span>' : ''}<button class="flow-stage" data-navigate="${key === 'paper_trades' ? 'paper' : key === 'strategy_versions' ? 'strategies' : 'research'}"><strong>${esc(label)}</strong><br>${num((runtimeRes.data.funnel || {})[key],0)}</button>`).join("");
  } else {
    errorState(el("runtime-status-bar"), "Runtime status unavailable.");
  }
  if (acctRes.ok) {
    el("overview-cards").innerHTML = accountCardsHtml(
      acctRes.data,
      riskRes.ok ? riskRes.data : null,
      regimeRes.ok ? regimeRes.data : null
    );
  } else {
    errorState(el("overview-cards"), "Account state unavailable.");
  }
  results.push({ ok: acctRes.ok, status: acctRes.status, error: acctRes.error });
  results.push({ ok: riskRes.ok, status: riskRes.status, error: riskRes.error });
  results.push({ ok: regimeRes.ok, status: regimeRes.status, error: regimeRes.error });
  results.push({ ok: runtimeRes.ok, status: runtimeRes.status, error: runtimeRes.error });

  results.push(
    await load("/positions", "overview-positions", (data) => {
      table(
        el("overview-positions"),
        [
          { key: "symbol", label: "Symbol" },
          { key: "side", label: "Side", cell: (r) => `<td>${badge(r.side, r.side)}</td>` },
          { key: "quantity", label: "Qty", cell: (r) => `<td class="num">${num(r.quantity, 0)}</td>` },
          { key: "entry_price", label: "Entry", cell: (r) => `<td class="num">${money(r.entry_price)}</td>` },
          { key: "stop_price", label: "Stop", cell: (r) => `<td class="num">${money(r.stop_price)}</td>` },
          { key: "target_price", label: "Target", cell: (r) => `<td class="num">${money(r.target_price)}</td>` },
        ],
        data.positions,
        "No open positions."
      );
    }, "Positions unavailable")
  );

  results.push(
    await load("/activity?limit=20", "overview-activity", (data) => {
      table(
        el("overview-activity"),
        [
          { key: "timestamp", label: "Time", cell: (r) => `<td>${dt(r.timestamp)}</td>` },
          { key: "kind", label: "Area", cell: (r) => `<td>${badge(r.kind, r.kind)}</td>` },
          { key: "summary", label: "Activity" },
          { key: "status", label: "Status", cell: (r) => `<td>${badge(r.status, r.status)}</td>` },
          { key: "artifact_id", label: "Artifact", cell: (r) => `<td>${r.artifact_id ? `<button class="link-btn" data-navigate="artifacts">${esc(r.artifact_id)}</button>` : '—'}</td>` },
        ],
        data.activity || [],
        "No operational or research activity has been recorded yet."
      );
    }, "Activity feed unavailable")
  );

  results.push(
    await load("/research/drafts", "overview-research", (data) => {
      table(
        el("overview-research"),
        [
          { key: "claim", label: "Hypothesis" },
          { key: "status", label: "Status", cell: (r) => `<td>${badge(r.status, r.status)}</td>` },
          { key: "research_area", label: "Area" },
          { key: "created_at", label: "Created", cell: (r) => `<td>${dt(r.created_at)}</td>` },
        ],
        (data.drafts || []).slice(0, 8),
        "No pending research drafts."
      );
    }, "Research drafts unavailable")
  );

  return results;
}

// The user-facing account model is the DYNAMIC broker account — Account
// Total / Broker Free Cash / Holdings Value / Unmanaged Holdings / Today
// P&L / Risk. The fixed ₹10k "Agent Book Value" / "Allocated Capital"
// scaffold is NOT shown here (it is not the user's portfolio); it appears
// only as a small, clearly-labelled "Autonomous Mandate" note on the
// Trading tab. See docs/CAPITAL_MODEL.md.
function accountCardsHtml(acct, risk, regime) {
  const pnl = acct.pnl_today;
  const bc = brokerCard(acct);
  const at = accountTotalDisplay(acct);
  const subtotal = accountSubtotalDisplay(acct);
  const cards = [
    { label: "Broker", value: bc.text, cls: bc.cls },
    { label: "Account Total", value: at.value, cls: at.unavailable ? "bad" : "",
      title: at.unavailable ? "verified broker account value is unavailable — not shown as cash" : "" },
    ...(subtotal.partial ? [{ label: "Priced subtotal", value: subtotal.value, cls: "amber",
      title: `${acct.unpriced_holding_count || 0} broker holding(s) are not currently quoteable; this is not the complete account total` }] : []),
    { label: "Broker Free Cash", value: money(acct.broker_free_cash) },
    { label: "Holdings Value", value: money(acct.holdings_market_value) },
    { label: "P&L Today", value: money(pnl), cls: pnlClass(pnl) },
    {
      label: acct.peak_capital_is_legacy ? "Risk / Drawdown ⚠" : "Risk / Drawdown",
      value: risk ? badge(risk.drawdown_level, risk.drawdown_level) : "—",
      title: acct.peak_capital_is_legacy
        ? "measured against a legacy pre-migration peak_capital — internal risk state not yet re-baselined (docs/CAPITAL_MODEL.md)"
        : "",
    },
    {
      label: "Regime",
      value: regime && regime.available ? badge(regime.regime, "normal") : "unavailable",
    },
  ];
  return staleNoticeHtml(acct) + cards
    .map(
      (c) => `<button class="card clickable-card" data-navigate="${c.label === "Regime" ? "research" : c.label.includes("Risk") ? "trading" : "trading"}"${c.title ? ` title="${esc(c.title)}"` : ""}><div class="label">${esc(c.label)}</div>
        <div class="value ${c.cls || ""}">${c.value}</div></button>`
    )
    .join("");
}

/** Full-width strips above the account cards: a RED problem banner when the
 * broker data is stale/incomplete or internally inconsistent, and separately
 * a calm note for truthful context (absent allocated_capital key, the legacy
 * pre-migration peak_capital) that is NOT a problem. */
function staleNoticeHtml(acct) {
  let html = "";
  const notice = accountStaleNotice(acct);
  if (notice) html += `<div class="${acct.account_value_status === "incomplete" ? "warning-state" : "error-state"}" style="grid-column:1/-1">${esc(notice)}</div>`;
  for (const note of accountInfoNotes(acct)) {
    html += `<div class="empty-state" style="grid-column:1/-1;text-align:left">${esc(note)}</div>`;
  }
  return html;
}

// --------------------------------------------------------------------------
// Trading
// --------------------------------------------------------------------------

export async function renderTrading() {
  const results = [];

  const riskRes = await apiGet("/risk");
  const acctRes = await apiGet("/account");
  if (riskRes.ok || acctRes.ok) {
    el("trading-cards").innerHTML = tradingCardsHtml(
      acctRes.ok ? acctRes.data : null,
      riskRes.ok ? riskRes.data : null
    );
  } else {
    errorState(el("trading-cards"), "Account/risk state unavailable.");
  }
  results.push({ ok: acctRes.ok, status: acctRes.status, error: acctRes.error });
  results.push({ ok: riskRes.ok, status: riskRes.status, error: riskRes.error });

  results.push(
    await load("/positions", "trading-positions", (data) => {
      table(
        el("trading-positions"),
        [
          { key: "symbol", label: "Symbol" },
          { key: "side", label: "Side", cell: (r) => `<td>${badge(r.side, r.side)}</td>` },
          { key: "quantity", label: "Qty", cell: (r) => `<td class="num">${num(r.quantity, 0)}</td>` },
          { key: "entry_price", label: "Entry", cell: (r) => `<td class="num">${money(r.entry_price)}</td>` },
          { key: "stop_price", label: "Stop", cell: (r) => `<td class="num">${money(r.stop_price)}</td>` },
          { key: "target_price", label: "Target", cell: (r) => `<td class="num">${money(r.target_price)}</td>` },
          { key: "open_risk", label: "Open Risk", cell: (r) => `<td class="num">${money(r.open_risk)}</td>` },
          { key: "opened_at", label: "Opened", cell: (r) => `<td>${dt(r.opened_at)}</td>` },
        ],
        data.positions,
        "No open positions."
      );
    }, "Positions unavailable")
  );

  results.push(
    await load("/orders", "trading-orders", (data) => {
      table(
        el("trading-orders"),
        [
          { key: "kind", label: "Kind", cell: (r) => `<td>${badge(r.kind, r.kind === "REJECTED" ? "sell" : "buy")}</td>` },
          { key: "symbol", label: "Symbol" },
          { key: "side", label: "Side" },
          { key: "quantity", label: "Qty", cell: (r) => `<td class="num">${num(r.quantity, 0)}</td>` },
          { key: "entry", label: "Entry", cell: (r) => `<td class="num">${money(r.entry)}</td>` },
          { key: "regime", label: "Regime" },
          { key: "ts", label: "Time", cell: (r) => `<td>${dt(r.ts)}</td>` },
        ],
        data.orders,
        "No orders in the journal yet."
      );
    }, "Orders unavailable")
  );

  results.push(
    await load("/trades", "trading-trades", (data) => {
      table(
        el("trading-trades"),
        [
          { key: "symbol", label: "Symbol" },
          { key: "exit_price", label: "Exit", cell: (r) => `<td class="num">${money(r.exit_price)}</td>` },
          { key: "pnl", label: "P&L", cell: (r) => `<td class="num ${pnlClass(r.pnl)}">${money(r.pnl)}</td>` },
          { key: "r_multiple", label: "R", cell: (r) => `<td class="num">${num(r.r_multiple)}</td>` },
          { key: "holding_days", label: "Held (days)", cell: (r) => `<td class="num">${num(r.holding_days, 0)}</td>` },
          { key: "reason", label: "Reason" },
          { key: "ts", label: "Closed", cell: (r) => `<td>${dt(r.ts)}</td>` },
        ],
        data.trades,
        "No closed trades yet."
      );
    }, "Trades unavailable")
  );

  return results;
}

function tradingCardsHtml(acct, risk) {
  const cards = [];
  if (acct) {
    const bc = brokerCard(acct);
    const at = accountTotalDisplay(acct);
    const subtotal = accountSubtotalDisplay(acct);
    const uh = unmanagedHoldingsDisplay(acct);
    // dynamic broker account
    cards.push({ label: "Broker", value: bc.text, cls: bc.cls });
    cards.push({ label: "Account Total", value: at.value, cls: at.unavailable ? "bad" : "",
      title: at.unavailable ? "verified broker account value is unavailable — not shown as cash" : "" });
    if (subtotal.partial) cards.push({ label: "Priced Subtotal", value: subtotal.value, cls: "amber",
      title: "current priced cash + holdings subtotal; not the complete account total" });
    cards.push({ label: "Broker Free Cash", value: money(acct.broker_free_cash) });
    cards.push({ label: "Holdings Value", value: money(acct.holdings_market_value) });
    cards.push({ label: "Unmanaged Holdings", value: uh.text,
      title: "your own INDmoney holdings — visible for reconciliation, never agent capital or tradeable" });
    cards.push({ label: "P&L Today", value: money(acct.pnl_today), cls: pnlClass(acct.pnl_today) });
  }
  if (risk) {
    // Field names here are engine.guardrails.status_summary()'s own, verbatim
    // (see /risk) — no second risk model is computed by this dashboard.
    cards.push({
      label: (acct && acct.peak_capital_is_legacy) ? "Drawdown Level ⚠" : "Drawdown Level",
      value: badge(risk.drawdown_level, risk.drawdown_level),
      title: (acct && acct.peak_capital_is_legacy)
        ? `measured against a legacy peak_capital (~${money(acct.peak_capital)}) from before the `
          + `INDmoney migration — internal risk state not yet re-baselined (docs/CAPITAL_MODEL.md)`
        : "",
    });
    cards.push({ label: "Capital Tier", value: esc(risk.tier ?? "—") });
    cards.push({
      label: "Risk / Trade",
      value: `${money(risk.risk_budget_per_trade)} <span class="subtext" style="display:inline">(${num(risk.risk_per_trade_pct)}%)</span>`,
    });
    cards.push({
      label: "Open Positions",
      value: `${num(risk.open_positions, 0)} / ${num(risk.max_open_positions, 0)}`,
    });
    cards.push({
      label: "Total Open Risk",
      value: `${money(risk.total_open_risk)} / ${money(risk.max_total_open_risk)}`,
    });
    cards.push({
      label: "Can Open New Positions?",
      value: risk.can_open_new_positions ? "yes" : "no",
      cls: risk.can_open_new_positions ? "good" : "bad",
    });
  }
  return (acct ? staleNoticeHtml(acct) : "") + cards
    .map(
      (c) => `<div class="card"${c.title ? ` title="${esc(c.title)}"` : ""}><div class="label">${esc(c.label)}</div>
        <div class="value ${c.cls || ""}">${c.value}</div></div>`
    )
    .join("");
}

// --------------------------------------------------------------------------
// Paper / shadow (Slice AA) — every card/table here reads from /paper/*
// only; nothing on this tab ever touches a live endpoint, and nothing on
// the live Trading tab ever reads a /paper/* endpoint. See api/paper_data.py.
// --------------------------------------------------------------------------

export async function renderPaper() {
  const results = [];

  const [acctRes, perfRes] = await Promise.all([
    apiGet("/paper/account"),
    apiGet("/paper/performance"),
  ]);
  if (acctRes.ok || perfRes.ok) {
    el("paper-cards").innerHTML = paperCardsHtml(
      acctRes.ok ? acctRes.data : null,
      perfRes.ok ? perfRes.data : null
    );
  } else {
    errorState(el("paper-cards"), "Paper account/performance unavailable.");
  }
  results.push({ ok: acctRes.ok, status: acctRes.status, error: acctRes.error });
  results.push({ ok: perfRes.ok, status: perfRes.status, error: perfRes.error });

  results.push(
    await load("/paper/strategies", "paper-strategies", (data) => {
      table(
        el("paper-strategies"),
        [
          { key: "strategy_id", label: "Strategy" },
          { key: "version_id", label: "Version" },
          { key: "algorithm_id", label: "Algorithm" },
          { key: "approved_by", label: "Approved By" },
          { key: "approved_at", label: "Approved", cell: (r) => `<td>${dt(r.approved_at)}</td>` },
        ],
        data.strategies,
        "No StrategyVersion is currently paper-eligible."
      );
    }, "Paper strategy list unavailable")
  );

  results.push(
    await load("/paper/positions", "paper-positions", (data) => {
      table(
        el("paper-positions"),
        [
          { key: "symbol", label: "Symbol" },
          { key: "strategy_version_id", label: "Version" },
          { key: "quantity", label: "Qty", cell: (r) => `<td class="num">${num(r.quantity, 0)}</td>` },
          { key: "avg_entry_price", label: "Avg Entry", cell: (r) => `<td class="num">${money(r.avg_entry_price)}</td>` },
          { key: "current_price", label: "Current", cell: (r) => `<td class="num">${money(r.current_price)}</td>` },
          {
            key: "unrealized_pnl", label: "Unrealized P&L",
            cell: (r) => `<td class="num ${pnlClass(r.unrealized_pnl)}">${money(r.unrealized_pnl)}</td>`,
          },
          { key: "opened_at", label: "Opened", cell: (r) => `<td>${dt(r.opened_at)}</td>` },
        ],
        data.positions,
        "No open paper positions."
      );
    }, "Paper positions unavailable")
  );

  results.push(
    await load("/paper/orders", "paper-orders", (data) => {
      table(
        el("paper-orders"),
        [
          { key: "symbol", label: "Symbol" },
          { key: "side", label: "Side", cell: (r) => `<td>${badge(r.side, r.side)}</td>` },
          { key: "status", label: "Status", cell: (r) => `<td>${badge(r.status, r.status)}</td>` },
          { key: "requested_quantity", label: "Qty", cell: (r) => `<td class="num">${num(r.requested_quantity, 0)}</td>` },
          { key: "fill_price", label: "Fill", cell: (r) => `<td class="num">${money(r.fill_price)}</td>` },
          { key: "strategy_version_id", label: "Version" },
          { key: "reason", label: "Reason" },
          { key: "created_at", label: "Time", cell: (r) => `<td>${dt(r.created_at)}</td>` },
        ],
        data.orders,
        "No paper orders yet."
      );
    }, "Paper orders unavailable")
  );

  results.push(
    await load("/paper/trades", "paper-trades", (data) => {
      table(
        el("paper-trades"),
        [
          { key: "symbol", label: "Symbol" },
          { key: "quantity", label: "Qty", cell: (r) => `<td class="num">${num(r.quantity, 0)}</td>` },
          { key: "entry_price", label: "Entry", cell: (r) => `<td class="num">${money(r.entry_price)}</td>` },
          { key: "exit_price", label: "Exit", cell: (r) => `<td class="num">${money(r.exit_price)}</td>` },
          { key: "net_pnl", label: "Net P&L", cell: (r) => `<td class="num ${pnlClass(r.net_pnl)}">${money(r.net_pnl)}</td>` },
          { key: "strategy_version_id", label: "Version" },
          { key: "closed_at", label: "Closed", cell: (r) => `<td>${dt(r.closed_at)}</td>` },
        ],
        data.trades,
        "No paper trades closed yet."
      );
    }, "Paper trades unavailable")
  );

  return results;
}

function paperCardsHtml(acct, perf) {
  const cards = [];
  if (acct) {
    cards.push({ label: "Paper Capital", value: money(acct.initial_capital) });
    cards.push({ label: "Paper Cash", value: money(acct.cash) });
  }
  if (perf) {
    cards.push({ label: "Paper Equity", value: money(perf.current_equity) });
    cards.push({ label: "Realized P&L", value: money(perf.realized_pnl), cls: pnlClass(perf.realized_pnl) });
    cards.push({ label: "Unrealized P&L", value: money(perf.unrealized_pnl), cls: pnlClass(perf.unrealized_pnl) });
    cards.push({ label: "Total Net P&L", value: money(perf.total_net_pnl), cls: pnlClass(perf.total_net_pnl) });
    cards.push({
      label: "Trades / Win Rate",
      value: `${num(perf.n_trades, 0)} <span class="subtext" style="display:inline">(${
        perf.win_rate === null || perf.win_rate === undefined ? "—" : num(perf.win_rate * 100, 0) + "%"
      })</span>`,
    });
    cards.push({ label: "Open Paper Positions", value: num(perf.open_position_count, 0) });
  }
  return cards
    .map(
      (c) => `<div class="card"><div class="label">${esc(c.label)}</div>
        <div class="value ${c.cls || ""}">${c.value}</div></div>`
    )
    .join("");
}

// --------------------------------------------------------------------------
// Research
// --------------------------------------------------------------------------

export async function renderResearch() {
  const results = [];

  results.push(
    await load("/research/worker-status", "research-worker-cards", (data) => {
      const admission = discoveryAdmissionDisplay(data);
      el("research-worker-cards").innerHTML = [
        { label: "Last heartbeat", value: dt(data.last_heartbeat_at) },
        { label: "Last cycle", value: (data.last_work_selected || []).join(", ") || "idle" },
        { label: "Discovery admission", ...admission },
        { label: "Recent errors", value: num((data.recent_errors || []).length, 0), cls: (data.recent_errors || []).length ? "bad" : "good" },
      ].map((card) => `<div class="card"><div class="label">${esc(card.label)}</div><div class="value ${card.cls || ""}">${esc(card.value)}</div><div class="subtext">${esc(card.detail || "")}</div></div>`).join("");
    }, "Research worker telemetry unavailable")
  );

  results.push(
    await load("/research/data-quality", "research-quality", (data) => {
      el("research-quality-cards").innerHTML = [
        { label: "Readiness", value: data.status, cls: data.status === "READY" ? "good" : data.status === "DEGRADED" ? "amber" : "bad" },
        { label: "Append-only store", value: data.append_only_enforced ? "Enforced" : "FAILED", cls: data.append_only_enforced ? "good" : "bad" },
        { label: "Checked at", value: dt(data.as_of) },
      ].map((card) => `<div class="card"><div class="label">${esc(card.label)}</div><div class="value ${card.cls || ""}">${esc(card.value)}</div></div>`).join("");
      table(el("research-quality"), [
        { key: "dataset", label: "Dataset" },
        { key: "status", label: "Status", cell: (r) => `<td>${badge(r.status, r.status)}</td>` },
        { key: "rows", label: "Rows", cell: (r) => `<td class="num">${num(r.rows, 0)}</td>` },
        { key: "entities", label: "Entities", cell: (r) => `<td class="num">${num(r.entities, 0)}</td>` },
        { key: "age_hours", label: "Age (h)", cell: (r) => `<td class="num">${r.age_hours == null ? "—" : num(r.age_hours, 1)}</td>` },
      ], data.checks, "No data-quality requirements configured.");
    }, "Data readiness unavailable")
  );

  results.push(
    await load("/research/drafts", "research-drafts", (data) => {
      table(
        el("research-drafts"),
        [
          { key: "claim", label: "Hypothesis" },
          { key: "status", label: "Status", cell: (r) => `<td>${badge(r.status, r.status)}</td>` },
          { key: "research_area", label: "Area" },
          { key: "source", label: "Source" },
          {
            key: "already_tested",
            label: "Already Tested?",
            cell: (r) => `<td>${r.already_tested ? "yes" : "no"}</td>`,
          },
          { key: "created_at", label: "Created", cell: (r) => `<td>${dt(r.created_at)}</td>` },
        ],
        data.drafts,
        "No pending research drafts."
      );
    }, "Research drafts unavailable")
  );

  results.push(
    await load("/research/evidence", "research-evidence", (data) => {
      table(
        el("research-evidence"),
        [
          { key: "title", label: "Hypothesis", cell: (r) => `<td>${esc(r.title || r.hypothesis_id)}</td>` },
          { key: "verdict", label: "Verdict", cell: (r) => `<td>${badge(r.verdict, r.verdict)}</td>` },
          { key: "n_variants_scored", label: "Variants Scored", cell: (r) => `<td class="num">${num(r.n_variants_scored, 0)}</td>` },
          { key: "positive_count", label: "Positive", cell: (r) => `<td class="num">${num(r.positive_count, 0)}</td>` },
          { key: "negative_count", label: "Negative", cell: (r) => `<td class="num">${num(r.negative_count, 0)}</td>` },
          { key: "research_area", label: "Area" },
        ],
        data.hypotheses,
        "No scored evidence yet."
      );
    }, "Research evidence unavailable")
  );

  results.push(
    await load("/research/areas", "research-areas", (data) => {
      table(
        el("research-areas"),
        [
          { key: "name", label: "Area" },
          { key: "hypothesis_count", label: "Hypotheses", cell: (r) => `<td class="num">${num(r.hypothesis_count, 0)}</td>` },
        ],
        data.areas,
        "No research areas tagged yet."
      );
    }, "Research areas unavailable")
  );

  return results;
}

// --------------------------------------------------------------------------
// Strategies
// --------------------------------------------------------------------------

export async function renderStrategies() {
  const results = [];

  results.push(
    await load("/strategies", "strategies-list", (data) => {
      table(
        el("strategies-list"),
        [
          { key: "strategy_id", label: "Strategy" },
          { key: "algorithm_id", label: "Algorithm" },
          { key: "version_id", label: "Version" },
          {
            key: "derived_from_hypothesis_id",
            label: "Derived From",
            cell: (r) => `<td>${esc(r.derived_from_hypothesis_id || "—")}</td>`,
          },
        ],
        data.strategies,
        "No StrategyVersions registered yet."
      );
    }, "Strategy registry unavailable")
  );

  results.push(
    await load("/backtests", "strategies-backtests", (data) => {
      table(
        el("strategies-backtests"),
        [
          { key: "strategy_id", label: "Strategy" },
          { key: "version_id", label: "Version" },
          { key: "n_trades", label: "Trades", cell: (r) => `<td class="num">${num(r.n_trades, 0)}</td>` },
          { key: "summary", label: "Summary" },
          { key: "recorded_at", label: "Recorded", cell: (r) => `<td>${dt(r.recorded_at)}</td>` },
        ],
        data.backtests,
        "No backtest runs recorded yet."
      );
    }, "Backtest records unavailable")
  );

  return results;
}

// --------------------------------------------------------------------------
// Phase 10.5 validation campaign — a read-only view over the durable funnel
// ledger.  It never starts an experiment or paper cycle from the browser.
// --------------------------------------------------------------------------

const FUNNEL_LABELS = {
  observations: "Observations", detections: "Detections", hypotheses: "Hypotheses",
  locked_experiments: "Locked experiments", reported_experiments: "Reported experiments",
  evidence: "Evidence summaries", strategy_versions: "Strategy versions",
  backtests: "Backtests", paper_eligible: "Paper eligible", paper_cycles: "Paper cycles",
  paper_trades: "Paper trades",
};

export async function renderValidation() {
  const result = await apiGet("/validation/status?history_limit=30");
  if (!result.ok) {
    errorState(el("validation-cards"), "Validation campaign status unavailable.");
    return [{ ok: false, status: result.status, error: result.error }];
  }
  const current = result.data.current || {};
  const funnel = current.funnel || {};
  const readiness = current.paper_readiness || {};
  const economics = current.research_economics || {};
  const capacity = current.capacity || {};
  el("validation-cards").innerHTML = [
    { label: "Campaign", value: `Phase ${current.phase || "10.5"}`, cls: "good", detail: dt(current.captured_at) },
    { label: "Capacity", value: capacity.state || "—", cls: capacity.state === "CRITICAL" ? "bad" : capacity.state === "HIGH_LOAD" ? "amber" : "good", detail: `${capacity.legacy_resource_state || "—"} infrastructure` },
    { label: "Paper readiness", value: readiness.ready ? "READY" : "BLOCKED", cls: readiness.ready ? "good" : "amber", detail: `${readiness.eligible_versions || 0} eligible version(s)` },
    { label: "AI interactions", value: num(economics.calls, 0), detail: economics.mean_latency_seconds === null ? "latency unmeasured" : `${economics.mean_latency_seconds}s mean latency` },
    { label: "Live promotion", value: (current.live_promotion || {}).state || "LOCKED", cls: "bad", detail: (current.live_promotion || {}).reason || "human gate required" },
  ].map((card) => `<div class="card"><div class="label">${esc(card.label)}</div><div class="value ${card.cls || ""}">${esc(card.value)}</div><div class="subtext">${esc(card.detail || "")}</div></div>`).join("");

  table(el("validation-funnel"), [
    { key: "stage", label: "Stage" },
    { key: "count", label: "Count", cell: (r) => `<td class="num">${num(r.count, 0)}</td>` },
    { key: "conversion", label: "Conversion from prior", cell: (r) => `<td class="num">${r.conversion === null ? "—" : `${num(r.conversion * 100, 1)}%`}</td>` },
  ], Object.keys(FUNNEL_LABELS).map((key, index, keys) => ({
    stage: FUNNEL_LABELS[key], count: funnel[key] || 0,
    conversion: index === 0 ? null : (current.conversion || {})[`${keys[index - 1]}_to_${key}`],
  })), "No validation metrics available.");

  table(el("validation-capacity"), [
    { key: "work_class", label: "Work class" },
    { key: "enabled", label: "Admission", cell: (r) => `<td>${badge(r.enabled ? "enabled" : "paused", r.enabled ? "normal" : "red")}</td>` },
    { key: "concurrency", label: "Concurrency", cell: (r) => `<td class="num">${num(r.concurrency, 0)}</td>` },
    { key: "cpu_share", label: "Policy share", cell: (r) => `<td class="num">${num((r.cpu_share || 0) * 100, 0)}%</td>` },
    { key: "reason", label: "Reason" },
  ], capacity.allocations || [], "No capacity plan available.");

  const blockers = readiness.blockers || [];
  el("validation-blockers").innerHTML = blockers.length
    ? `<ul class="blocker-list">${blockers.map((b) => `<li>${esc(b)}</li>`).join("")}</ul>`
    : `<div class="empty-state">No current Phase 10.5 blocker.</div>`;

  table(el("validation-history"), [
    { key: "captured_at", label: "Captured", cell: (r) => `<td>${dt(r.captured_at)}</td>` },
    { key: "hypotheses", label: "Hypotheses", cell: (r) => `<td class="num">${num((r.funnel || {}).hypotheses, 0)}</td>` },
    { key: "evidence", label: "Evidence", cell: (r) => `<td class="num">${num((r.funnel || {}).evidence, 0)}</td>` },
    { key: "strategies", label: "Strategies", cell: (r) => `<td class="num">${num((r.funnel || {}).strategy_versions, 0)}</td>` },
    { key: "paper", label: "Paper trades", cell: (r) => `<td class="num">${num((r.funnel || {}).paper_trades, 0)}</td>` },
  ], [...(result.data.history || [])].reverse(), "No persisted campaign snapshots yet.");
  return [{ ok: true, data: result.data }];
}

// --------------------------------------------------------------------------
// Control (outcome 1: RUNNING/PAUSED/SAFE_MODE/STOPPED) + Resource Governor
// + AI provider/model (outcome 2) — the FIRST tab on this dashboard with a
// real write path. Every action still requires a typed reason, mirroring
// the backend's own validation (api/app.py rejects a blank reason with
// 400 before this ever reaches control/runtime.py or control/ai_config.py).
// --------------------------------------------------------------------------

const CONTROL_MODES = ["RUNNING", "PAUSED", "SAFE_MODE", "STOPPED"];
const AI_PROVIDERS = ["anthropic_cli", "openai"];

function adminGateHtml() {
  if (hasAdminToken()) {
    return `<div class="admin-access-row"><div><strong class="good-text">Administrator session unlocked</strong><div class="subtext">Administrative writes use a separate credential and remain audited.</div></div><button id="admin-lock" class="ghost-btn">Lock admin</button></div>`;
  }
  return `<form id="admin-unlock-form" class="control-form admin-unlock-form">
    <div class="admin-lock-copy"><strong>Observer mode</strong><div class="subtext">Status remains visible. Enter the separate administrator token to enable control changes.</div></div>
    <label>Administrator token<input type="password" name="token" autocomplete="current-password" required /></label>
    <button type="submit">Unlock Admin</button><span class="control-form-status"></span>
  </form>`;
}

function wireAdminGate() {
  const lock = el("admin-lock");
  if (lock) lock.addEventListener("click", () => { setAdminToken(""); renderControl(); });
  const form = el("admin-unlock-form");
  if (!form) return;
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const status = form.querySelector(".control-form-status");
    setAdminToken(form.token.value);
    const verified = await apiGet("/admin/status", { admin: true });
    if (verified.ok) renderControl();
    else {
      setAdminToken("");
      status.textContent = "Access denied";
    }
  });
}

function lockedAdminActionHtml() {
  return `<div class="admin-locked-note">🔒 Unlock Administrator Access above to change this setting.</div>`;
}

function controlModeCardsHtml(status) {
  const modeCls = { RUNNING: "good", PAUSED: "amber", SAFE_MODE: "amber", STOPPED: "bad" }[status.mode] || "";
  const livePaused = !!status.live_trading_paused;
  return `
    <div class="card">
      <div class="label">Organism State</div>
      <div class="value ${modeCls}">${esc(status.mode || "—")}</div>
      <div class="subtext">since ${dt(status.changed_at)} by ${esc(status.actor || "—")}</div>
    </div>
    <div class="card">
      <div class="label">Research / Paper</div>
      <div class="value ${status.research_allowed ? "good" : "bad"}">${status.research_allowed ? "Allowed" : "Blocked"}</div>
      <div class="subtext">data ingestion: ${status.data_ingestion_allowed ? "on" : "off"}</div>
    </div>
    <div class="card">
      <div class="label">Live Execution</div>
      <div class="value ${livePaused ? "bad" : "good"}">${livePaused ? "PAUSED" : "Active pause-gate off"}</div>
      <div class="subtext">${status.live_paused_by_global_control ? "paused by global control" : esc(status.live_trading_pause_reason || "no global-control pause set")}</div>
    </div>
  `;
}

function resourceCardHtml(resource) {
  if (!resource) return "";
  const cls = { HEALTHY: "good", CONSTRAINED: "amber", PRESSURED: "amber", CRITICAL: "bad" }[resource.state] || "";
  const snap = resource.snapshot || {};
  const pct = (r) => (r === null || r === undefined ? "—" : `${Math.round(r * 100)}%`);
  return `
    <div class="card">
      <div class="label">Resource State</div>
      <div class="value ${cls}">${esc(resource.state || "—")}</div>
      <div class="subtext">${(resource.reasons || []).join("; ") || "no pressure detected"}</div>
    </div>
    <div class="card">
      <div class="label">CPU load ratio</div>
      <div class="value">${snap.load_ratio === null || snap.load_ratio === undefined ? "—" : num(snap.load_ratio, 2)}</div>
      <div class="subtext">${snap.cpu_count || "—"} vCPU</div>
    </div>
    <div class="card">
      <div class="label">Memory available</div>
      <div class="value">${pct(snap.mem_available_ratio)}</div>
    </div>
    <div class="card">
      <div class="label">Disk free</div>
      <div class="value">${pct(snap.disk_free_ratio)}</div>
    </div>
  `;
}

function aiCardHtml(ai) {
  if (!ai) return "";
  const budget = ai.budget || {};
  return `
    <div class="card">
      <div class="label">AI Provider / Model</div>
      <div class="value">${esc(ai.effective_provider || "—")}</div>
      <div class="subtext">${esc(ai.effective_model || "—")}</div>
    </div>
    <div class="card">
      <div class="label">Tokens Today</div>
      <div class="value">${num(budget.tokens_today, 0)} / ${num(budget.max_tokens_per_day, 0)}</div>
    </div>
    <div class="card">
      <div class="label">Expensive Calls Today</div>
      <div class="value">${num(budget.expensive_calls_today, 0)} / ${num(budget.max_expensive_calls_per_day, 0)}</div>
      <div class="subtext">last call: ${dt(budget.last_call_at)}</div>
    </div>
  `;
}

function controlActionsHtml(currentMode) {
  return `
    <form id="control-mode-form" class="control-form">
      <label>Set organism mode
        <select name="mode">
          ${CONTROL_MODES.map((m) => `<option value="${m}" ${m === currentMode ? "selected" : ""}>${m}</option>`).join("")}
        </select>
      </label>
      <label>Reason (required)
        <input type="text" name="reason" placeholder="why are you changing this?" required />
      </label>
      <button type="submit">Apply</button>
      <span class="control-form-status"></span>
    </form>
  `;
}

function aiConfigFormHtml(ai) {
  return `
    <form id="ai-config-form" class="control-form">
      <label>Provider
        <select name="provider">
          ${AI_PROVIDERS.map((p) => `<option value="${p}" ${p === ai.configured_provider ? "selected" : ""}>${p}</option>`).join("")}
        </select>
      </label>
      <label>Model (optional — only used by openai)
        <input type="text" name="model" placeholder="e.g. gpt-4o-mini" value="${esc(ai.configured_model || "")}" />
      </label>
      <label>Reason (required)
        <input type="text" name="reason" placeholder="why switch?" required />
      </label>
      <button type="submit">Switch</button>
      <span class="control-form-status"></span>
    </form>
  `;
}

function wireControlForm(currentStatus) {
  const form = el("control-mode-form");
  if (!form) return;
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const statusEl = form.querySelector(".control-form-status");
    const mode = form.mode.value;
    const reason = form.reason.value;
    statusEl.textContent = "applying...";
    const result = await apiPost("/control/mode", { mode, reason, actor: "dashboard" }, { admin: true });
    if (result.ok) {
      statusEl.textContent = `now ${result.data.mode}`;
      renderControl();
    } else {
      statusEl.textContent = `failed: ${result.detail || result.error}`;
    }
  });
}

function wireAiConfigForm() {
  const form = el("ai-config-form");
  if (!form) return;
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const statusEl = form.querySelector(".control-form-status");
    const provider = form.provider.value;
    const model = form.model.value || null;
    const reason = form.reason.value;
    statusEl.textContent = "applying...";
    const result = await apiPost("/ai/config", { provider, model, reason, actor: "dashboard" }, { admin: true });
    if (result.ok) {
      statusEl.textContent = `now ${result.data.effective_provider}`;
      renderControl();
    } else {
      statusEl.textContent = `failed: ${result.detail || result.error}`;
    }
  });
}

export async function renderSystem() {
  const results = [];
  results.push(await load("/system/status", "system-components", (data) => {
    const wd = data.watchdog || {};
    table(el("system-components"), [
      {key:"component",label:"Component"},
      {key:"desired_state",label:"Desired",cell:(r)=>`<td>${badge(r.desired_state,r.desired_state)}</td>`},
      {key:"scheduler_state",label:"Scheduler"},
      {key:"heartbeat_state",label:"Heartbeat"},
      {key:"dependency_state",label:"Dependency"},
      {key:"effective_state",label:"Effective",cell:(r)=>`<td>${badge(r.effective_state,r.effective_state)}</td>`},
      {key:"reason",label:"Reason"},
      {key:"last_success",label:"Last Success",cell:(r)=>`<td>${dt(r.last_success)}</td>`},
    ], wd.components || [], "Watchdog has not produced its first runtime snapshot yet.");
    const alerts = wd.alerts || [];
    el("system-alerts").innerHTML = alerts.length ? `<ul class="alert-list">${alerts.map(a=>`<li><strong>${esc(a.code)}</strong> — ${esc(a.reason)}</li>`).join("")}</ul>` : `<div class="empty-state">No current watchdog alerts.</div>`;
    const op = data.operations || {};
    el("system-operations").innerHTML = [
      {label:"Recorder",value:(op.recorder||{}).state||"UNKNOWN"},
      {label:"Research heartbeat",value:dt((op.research_worker||{}).last_heartbeat_at)},
      {label:"Paper",value:(op.paper||{}).state||"UNKNOWN"},
      {label:"Phase 11",value:data.phase_11,cls:"amber"},
    ].map(c=>`<div class="card"><div class="label">${esc(c.label)}</div><div class="value ${c.cls||''}">${esc(c.value)}</div></div>`).join("");
  }, "System status unavailable"));
  return results;
}

export async function renderControl() {
  const results = [];

  el("admin-access").innerHTML = adminGateHtml();
  wireAdminGate();

  const [controlRes, resourceRes, aiRes, operationsRes, accountRes, notifyRes, runtimeRes, systemRes] = await Promise.all([
    apiGet("/control/status"),
    apiGet("/resources/status"),
    apiGet("/ai/status"),
    apiGet("/operations/status"),
    apiGet("/account"),
    apiGet("/notifications/status"),
    apiGet("/runtime/status"),
    apiGet("/system/status"),
  ]);
  results.push({ ok: controlRes.ok, status: controlRes.status, error: controlRes.error });
  results.push({ ok: resourceRes.ok, status: resourceRes.status, error: resourceRes.error });
  results.push({ ok: aiRes.ok, status: aiRes.status, error: aiRes.error });
  results.push({ ok: operationsRes.ok, status: operationsRes.status, error: operationsRes.error });
  results.push({ ok: accountRes.ok, status: accountRes.status, error: accountRes.error });
  results.push({ ok: notifyRes.ok, status: notifyRes.status, error: notifyRes.error });
  results.push({ ok: runtimeRes.ok, status: runtimeRes.status, error: runtimeRes.error });
  results.push({ ok: systemRes.ok, status: systemRes.status, error: systemRes.error });

  if (controlRes.ok) {
    el("control-cards").innerHTML = controlModeCardsHtml(controlRes.data);
    el("control-actions").innerHTML = hasAdminToken()
      ? controlActionsHtml(controlRes.data.mode) : lockedAdminActionHtml();
    if (hasAdminToken()) wireControlForm(controlRes.data);

    table(
      el("control-history"),
      [
        { key: "changed_at", label: "When", cell: (r) => `<td>${dt(r.changed_at)}</td>` },
        { key: "mode", label: "Mode", cell: (r) => `<td>${badge(r.mode, r.mode)}</td>` },
        { key: "actor", label: "By" },
        { key: "reason", label: "Reason" },
      ],
      [...(controlRes.data.history || [])].reverse().slice(0, 15),
      "No mode changes recorded yet."
    );
  } else {
    errorState(el("control-cards"), "Control status unavailable.");
  }

  if (systemRes.ok) {
    const states = Object.values(systemRes.data.desired_components || {});
    table(el("component-controls"), [
      {key:"component",label:"Component"},
      {key:"desired_state",label:"Desired",cell:r=>`<td>${badge(r.desired_state,r.desired_state)}</td>`},
      {key:"reason",label:"Reason"},{key:"actor",label:"Actor"},
      {key:"changed_at",label:"Changed",cell:r=>`<td>${dt(r.changed_at)}</td>`},
      {key:"action",label:"Control",cell:r=>`<td>${hasAdminToken()?`<button class="component-toggle" data-component="${esc(r.component)}" data-next="${r.desired_state==='RUNNING'?'PAUSED':'RUNNING'}">${r.desired_state==='RUNNING'?'Pause':'Resume'}</button>`:'Admin token required'}</td>`},
    ], states, "No component controls available.");
    document.querySelectorAll(".component-toggle").forEach(btn=>btn.addEventListener("click",async()=>{
      const reason=window.prompt(`Reason to ${btn.dataset.next.toLowerCase()} ${btn.dataset.component}:`);
      if(!reason) return;
      const result=await apiPost("/control/component",{component:btn.dataset.component,desired_state:btn.dataset.next,reason,actor:"dashboard-admin",resume_policy:"MANUAL"},{admin:true});
      if(result.ok) renderControl(); else window.alert(result.detail||result.error);
    }));
  }

  el("resource-cards").innerHTML = resourceRes.ok
    ? resourceCardHtml(resourceRes.data)
    : `<div class="error-state">Resource status unavailable.</div>`;

  if (operationsRes.ok) {
    const op = operationsRes.data;
    const worker = op.research_worker || {};
    const brokerFresh = accountRes.ok && accountRes.data.account_value_status === "fresh";
    el("operations-cards").innerHTML = [
      { label: "Broker Snapshot", value: accountRes.ok ? accountRes.data.account_value_status : "Unavailable", cls: brokerFresh ? "good" : "bad", detail: accountRes.ok ? syncedAtLabel(accountRes.data) : "account endpoint unavailable" },
      { label: "Research Worker", value: worker.last_heartbeat_at ? "Reporting" : "No heartbeat", cls: worker.last_heartbeat_at ? "good" : "amber", detail: dt(worker.last_heartbeat_at) },
      { label: "Recorder", value: op.recorder.state, cls: op.recorder.state === "HEALTHY" ? "good" : op.recorder.state === "DEGRADED" ? "amber" : "bad", detail: dt(op.recorder.last_run && op.recorder.last_run.ts) },
      { label: "Paper Cycle", value: op.paper.state, cls: op.paper.state === "FAILED" ? "bad" : op.paper.state === "COMPLETED" ? "good" : "amber", detail: dt(op.paper.last_cycle && op.paper.last_cycle.completed_at) },
    ].map((card) => `<div class="card"><div class="label">${esc(card.label)}</div><div class="value ${card.cls}">${esc(card.value)}</div><div class="subtext">${esc(card.detail || "")}</div></div>`).join("");
  } else {
    errorState(el("operations-cards"), "Operational status unavailable.");
  }

  if (aiRes.ok) {
    el("ai-cards").innerHTML = aiCardHtml(aiRes.data);
    el("ai-config-actions").innerHTML = hasAdminToken()
      ? aiConfigFormHtml(aiRes.data) : lockedAdminActionHtml();
    if (hasAdminToken()) wireAiConfigForm();
    table(el("ai-model-routing"), [
      {key:"role",label:"Workflow"},{key:"model",label:"Model"}
    ], Object.entries(aiRes.data.role_mappings || {}).map(([role,model]) => ({role,model})), "No role mappings configured.");

    table(
      el("ai-config-history"),
      [
        { key: "changed_at", label: "When", cell: (r) => `<td>${dt(r.changed_at)}</td>` },
        { key: "provider", label: "Provider" },
        { key: "model", label: "Model", cell: (r) => `<td>${esc(r.model || "—")}</td>` },
        { key: "reason", label: "Reason" },
      ],
      [...(aiRes.data.config_history || [])].reverse().slice(0, 10),
      "No provider switches recorded yet."
    );
  } else {
    errorState(el("ai-cards"), "AI status unavailable.");
  }

  if (notifyRes.ok) {
    const n = notifyRes.data;
    el("notification-cards").innerHTML = [
      {label:"Telegram",value:n.configured ? "CONFIGURED" : "NOT CONFIGURED",cls:n.configured?"good":"bad"},
      {label:"Last Success",value:n.last_successful_message ? dt(n.last_successful_message.timestamp) : "Never",cls:n.last_successful_message?"good":"amber"},
      {label:"Sent Today",value:num(n.messages_sent_today,0),cls:""},
    ].map(c => `<div class="card"><div class="label">${esc(c.label)}</div><div class="value ${c.cls}">${esc(c.value)}</div></div>`).join("");
    el("notification-actions").innerHTML = hasAdminToken() ? `<form id="notification-test-form" class="control-form"><button type="submit">Send Test Notification</button><span class="control-form-status"></span></form>` : lockedAdminActionHtml();
    el("notification-test-form")?.addEventListener("submit", async (e) => { e.preventDefault(); const s=e.target.querySelector(".control-form-status"); s.textContent="sending..."; const r=await apiPost("/notifications/test",{actor:"dashboard-admin"},{admin:true}); s.textContent=r.ok?"delivered":`failed: ${r.detail||r.error}`; if(r.ok) renderControl(); });
    table(el("notification-history"), [{key:"timestamp",label:"When",cell:r=>`<td>${dt(r.timestamp)}</td>`},{key:"category",label:"Category"},{key:"message_type",label:"Type"},{key:"status",label:"Status",cell:r=>`<td>${badge(r.status,r.status)}</td>`},{key:"latency_ms",label:"Latency ms"}], [...(n.recent||[])].reverse(), "No audited notification attempts yet.");
  }
  if (runtimeRes.ok) table(el("runtime-matrix"), [
    {key:"module",label:"Module"},{key:"expected_state",label:"Expected"},{key:"actual_state",label:"Actual"},{key:"last_successful_run",label:"Last success",cell:r=>`<td>${dt(r.last_successful_run)}</td>`},{key:"last_attempt",label:"Last attempt",cell:r=>`<td>${dt(r.last_attempt)}</td>`},{key:"queue_depth",label:"Queue"}
  ], runtimeRes.data.runtime_matrix || [], "No runtime telemetry.");

  return results;
}

// --------------------------------------------------------------------------
// Artifacts — the unified explorer (outcome 2, Part D/E6/E7). List shows
// metadata only; a detail view is fetched on demand when an artifact is
// clicked (Part N: never load full prompt/response bodies into the list).
// --------------------------------------------------------------------------

const ARTIFACT_TYPES = [
  "", "model_interaction", "detection", "hypothesis", "experiment_result",
  "evidence", "opportunity_event", "note", "discovery_search", "experiment",
  "system_event",
];

let artifactsCurrentType = "";
let artifactsQuery = "";
let artifactsSort = "timestamp";
let artifactsOrder = "desc";
let artifactsOffset = 0;
const ARTIFACT_PAGE_SIZE = 25;

function artifactTypeFilterHtml() {
  return `
    <form id="artifact-search-form" class="artifact-toolbar">
    <label>Search
      <input id="artifact-query" type="search" value="${esc(artifactsQuery)}" placeholder="id, summary, source, status" />
    </label>
    <label>Type
      <select id="artifact-type-filter">
        ${ARTIFACT_TYPES.map(
          (t) => `<option value="${t}" ${t === artifactsCurrentType ? "selected" : ""}>${t || "(all)"}</option>`
        ).join("")}
      </select>
    </label>
    <label>Sort
      <select id="artifact-sort">
        ${["timestamp", "type", "status", "source", "summary"].map((s) => `<option value="${s}" ${s === artifactsSort ? "selected" : ""}>${s}</option>`).join("")}
      </select>
    </label>
    <label>Order
      <select id="artifact-order"><option value="desc" ${artifactsOrder === "desc" ? "selected" : ""}>descending</option><option value="asc" ${artifactsOrder === "asc" ? "selected" : ""}>ascending</option></select>
    </label>
    <button type="submit">Apply</button>
    </form>
  `;
}

async function showArtifactDetail(artifactId) {
  const panel = el("artifact-detail");
  panel.innerHTML = `<div class="empty-state">loading ${esc(artifactId)}...</div>`;
  const result = await apiGet(`/artifacts/${encodeURIComponent(artifactId)}`);
  if (!result.ok) {
    errorState(panel, `Could not load artifact ${artifactId} (${result.detail || result.error}).`);
    return;
  }
  const a = result.data;
  if (a.type === "model_interaction") {
    const p = a.payload || {};
    panel.innerHTML = `
      <h3>Model Interaction — ${esc(a.id)}</h3>
      <div class="stat-row">
        <div class="stat"><div class="label">Provider</div><div class="value">${esc(p.provider)}</div></div>
        <div class="stat"><div class="label">Model</div><div class="value">${esc(p.model)}</div></div>
        <div class="stat"><div class="label">Status</div><div class="value">${badge(p.status, p.status)}</div></div>
        <div class="stat"><div class="label">Purpose</div><div class="value">${esc(p.purpose)}</div></div>
        <div class="stat"><div class="label">Trigger</div><div class="value">${esc(p.trigger)}</div></div>
        <div class="stat"><div class="label">Latency</div><div class="value">${p.latency_seconds ?? "—"}s</div></div>
        <div class="stat"><div class="label">Input tokens</div><div class="value">${p.input_tokens ?? "unknown"}</div></div>
        <div class="stat"><div class="label">Output tokens</div><div class="value">${p.output_tokens ?? "unknown"}</div></div>
      </div>
      ${p.error ? `<div class="error-state">Error: ${esc(p.error)}</div>` : ""}
      <h4>Prompt sent to the model${p.prompt && p.prompt.truncated ? " (truncated)" : ""}</h4>
      <pre class="artifact-text">${esc((p.prompt && p.prompt.text) || "(none)")}</pre>
      <h4>Response received${p.response && p.response.truncated ? " (truncated)" : ""}</h4>
      <pre class="artifact-text">${esc((p.response && p.response.text) || "(none)")}</pre>
    `;
    return;
  }
  panel.innerHTML = `
    <h3>${esc(a.type)} — ${esc(a.id)}</h3>
    <div class="subtext">${dt(a.timestamp)} · source: ${esc(a.source || "—")}</div>
    <pre class="artifact-text">${esc(JSON.stringify(a.payload, null, 2))}</pre>
  `;
}

export async function renderArtifacts() {
  const results = [];
  el("artifact-filter").innerHTML = artifactTypeFilterHtml();
  const filterSelect = el("artifact-type-filter");
  filterSelect.addEventListener("change", () => {
    artifactsCurrentType = filterSelect.value;
    artifactsOffset = 0;
    renderArtifacts();
  });
  el("artifact-search-form").addEventListener("submit", (event) => {
    event.preventDefault();
    artifactsQuery = el("artifact-query").value.trim();
    artifactsSort = el("artifact-sort").value;
    artifactsOrder = el("artifact-order").value;
    artifactsOffset = 0;
    renderArtifacts();
  });

  const params = new URLSearchParams({ limit: ARTIFACT_PAGE_SIZE, offset: artifactsOffset,
    sort: artifactsSort, order: artifactsOrder });
  if (artifactsCurrentType) params.set("type", artifactsCurrentType);
  if (artifactsQuery) params.set("q", artifactsQuery);
  results.push(
    await load(`/artifacts?${params}`, "artifact-list", (data) => {
      const container = el("artifact-list");
      if (!data.artifacts || data.artifacts.length === 0) {
        container.innerHTML = `<div class="empty-state">No artifacts recorded yet for this filter.</div>`;
        return;
      }
      table(
        container,
        [
          { key: "timestamp", label: "When", cell: (r) => `<td>${dt(r.timestamp)}</td>` },
          { key: "type", label: "Type", cell: (r) => `<td>${badge(r.type, r.type)}</td>` },
          { key: "status", label: "Status", cell: (r) => `<td>${r.status ? badge(r.status, r.status) : "—"}</td>` },
          { key: "summary", label: "Summary" },
          {
            key: "id",
            label: "",
            cell: (r) => `<td><button class="link-btn" data-artifact-id="${esc(r.id)}">view</button></td>`,
          },
        ],
        data.artifacts,
        "No artifacts recorded yet for this filter."
      );
      container.querySelectorAll("button[data-artifact-id]").forEach((btn) => {
        btn.addEventListener("click", () => showArtifactDetail(btn.dataset.artifactId));
      });
      container.insertAdjacentHTML("beforeend", `<div class="pager"><span>${data.total_count ? `${data.offset + 1}–${data.offset + data.shown_count} of ${data.total_count}` : "0 results"}</span><button id="artifact-prev" class="ghost-btn" ${data.offset <= 0 ? "disabled" : ""}>Previous</button><button id="artifact-next" class="ghost-btn" ${data.truncated ? "" : "disabled"}>Next</button></div>`);
      el("artifact-prev")?.addEventListener("click", () => { artifactsOffset = Math.max(0, artifactsOffset - ARTIFACT_PAGE_SIZE); renderArtifacts(); });
      el("artifact-next")?.addEventListener("click", () => { artifactsOffset += ARTIFACT_PAGE_SIZE; renderArtifacts(); });
    }, "Artifacts unavailable")
  );

  return results;
}
