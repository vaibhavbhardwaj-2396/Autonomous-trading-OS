// frontend/js/views.js — one render function per tab. Each fetches only
// the endpoints that tab needs, renders what it got, and reports success
// per-endpoint back to app.js (which uses that to decide the connection
// banner) — one endpoint failing never stops the others on the same page
// from rendering.

import { apiGet } from "./api.js";
import { esc, money, num, pnlClass, dt, badge, table, errorState } from "./format.js";

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

  const [acctRes, riskRes, regimeRes] = await Promise.all([
    apiGet("/account"),
    apiGet("/risk"),
    apiGet("/regime"),
  ]);
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
    await load("/trades", "overview-activity", (data) => {
      table(
        el("overview-activity"),
        [
          { key: "symbol", label: "Symbol" },
          { key: "exit_price", label: "Exit", cell: (r) => `<td class="num">${money(r.exit_price)}</td>` },
          { key: "pnl", label: "P&L", cell: (r) => `<td class="num ${pnlClass(r.pnl)}">${money(r.pnl)}</td>` },
          { key: "r_multiple", label: "R", cell: (r) => `<td class="num">${num(r.r_multiple)}</td>` },
          { key: "reason", label: "Reason" },
          { key: "ts", label: "Closed", cell: (r) => `<td>${dt(r.ts)}</td>` },
        ],
        (data.trades || []).slice(0, 8),
        "No trades recorded yet."
      );
    }, "Trades unavailable")
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

function accountCardsHtml(acct, risk, regime) {
  const pnl = acct.pnl_today;
  const cards = [
    { label: "Portfolio Value", value: money(acct.portfolio_value) },
    { label: "Cash Available", value: money(acct.cash) },
    { label: "P&L Today", value: money(pnl), cls: pnlClass(pnl) },
    {
      label: "Risk / Drawdown",
      value: risk ? badge(risk.drawdown_level, risk.drawdown_level) : "—",
    },
    {
      label: "Regime",
      value: regime && regime.available ? badge(regime.regime, "normal") : "unavailable",
    },
  ];
  return cards
    .map(
      (c) => `<div class="card"><div class="label">${esc(c.label)}</div>
        <div class="value ${c.cls || ""}">${c.value}</div></div>`
    )
    .join("");
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
    cards.push({ label: "Portfolio Value", value: money(acct.portfolio_value) });
    cards.push({ label: "Cash Available", value: money(acct.cash) });
    cards.push({ label: "P&L Today", value: money(acct.pnl_today), cls: pnlClass(acct.pnl_today) });
  }
  if (risk) {
    // Field names here are engine.guardrails.status_summary()'s own, verbatim
    // (see /risk) — no second risk model is computed by this dashboard.
    cards.push({ label: "Drawdown Level", value: badge(risk.drawdown_level, risk.drawdown_level) });
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
