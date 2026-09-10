// frontend/js/format.js — small, dumb rendering helpers shared by every
// view. None of these compute a number that isn't already in the API
// response; they only format one that is.

const ESCAPE_MAP = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };

export function esc(s) {
  if (s === null || s === undefined) return "";
  return String(s).replace(/[&<>"']/g, (c) => ESCAPE_MAP[c]);
}

export function money(v) {
  if (v === null || v === undefined) return "—";
  const n = Number(v);
  const sign = n < 0 ? "-" : "";
  return `${sign}₹${Math.abs(n).toLocaleString("en-IN", { maximumFractionDigits: 2 })}`;
}

export function num(v, digits = 2) {
  if (v === null || v === undefined) return "—";
  return Number(v).toLocaleString("en-IN", { maximumFractionDigits: digits });
}

export function pnlClass(v) {
  if (v === null || v === undefined) return "";
  return Number(v) > 0 ? "pos" : Number(v) < 0 ? "neg" : "";
}

export function dt(iso) {
  if (!iso) return "—";
  try {
    const d = new Date(iso);
    if (isNaN(d.getTime())) return String(iso);
    return d.toLocaleString("en-IN", { dateStyle: "medium", timeStyle: "short" });
  } catch (e) {
    return String(iso);
  }
}

export function badge(text, cls) {
  if (!text) return "";
  return `<span class="badge ${esc(cls || String(text).toLowerCase())}">${esc(text)}</span>`;
}

/** Render a <table> from rows (array of plain objects) and column defs
 * [{key, label, cell?}], or an empty-state message if rows is empty. */
export function table(container, columns, rows, emptyMessage) {
  if (!rows || rows.length === 0) {
    container.innerHTML = `<div class="empty-state">${esc(emptyMessage || "Nothing here yet.")}</div>`;
    return;
  }
  const thead = `<tr>${columns.map((c) => `<th>${esc(c.label)}</th>`).join("")}</tr>`;
  const tbody = rows
    .map((row) => {
      const cells = columns
        .map((c) => (c.cell ? c.cell(row) : `<td>${esc(row[c.key])}</td>`))
        .join("");
      return `<tr>${cells}</tr>`;
    })
    .join("");
  container.innerHTML = `<table><thead>${thead}</thead><tbody>${tbody}</tbody></table>`;
}

export function errorState(container, message) {
  container.innerHTML = `<div class="error-state">${esc(message)}</div>`;
}

// --------------------------------------------------------------------------
// Broker truth / account freshness — pure helpers over the /account payload
// (see api/broker_truth.py). None of these compute a figure; they only
// decide how to LABEL one so a stale Kite-era snapshot can never be shown
// as a current INDmoney balance.
// --------------------------------------------------------------------------

/** True when the broker account figures backing /account are NOT a fresh
 * successful sync (stale, never synced, unknown, or a different broker era). */
export function accountIsStale(acct) {
  return !!acct && acct.account_value_status !== undefined && acct.account_value_status !== "fresh";
}

/** {label, cls, status} for a "Broker" card. label is the active broker
 * ("INDmoney / INDstocks"); cls is "good" only when the account data is
 * fresh, "bad" otherwise. */
export function brokerCard(acct) {
  const label = (acct && acct.broker && acct.broker.label) || "broker unknown";
  const status = (acct && acct.account_value_status) || "unknown";
  return {
    label,
    status,
    cls: status === "fresh" ? "good" : "bad",
    text: status === "fresh" ? label : `${label} · ${String(status).replace(/_/g, " ")}`,
  };
}

// (portfolioValueDisplay removed — the "Agent Book Value" / fixed-capital
// card it fed is no longer shown. `book_value_reconciled` / `book_value_note`
// remain in the /account payload for the stale-state check, but the dynamic
// broker account is what the dashboard presents. See docs/CAPITAL_MODEL.md.)

/** The red PROBLEM banner above the account cards — stale/incomplete broker
 * data, or a genuine internal-state inconsistency. "" when the account data
 * is fine (calm context lives in accountInfoNotes(), not here). Includes the
 * broker sync timestamp. */
export function accountStaleNotice(acct) {
  if (!acct) return "";
  const warnings = Array.isArray(acct.account_warnings) ? acct.account_warnings : [];
  const stamp = syncedAtLabel(acct);
  if (warnings.length) return `${warnings.join(" ")} (${stamp})`;
  if (accountIsStale(acct)) {
    return `Broker account data is ${String(acct.account_value_status).replace(/_/g, " ")} — showing the agent's last known book value, not a live balance. (${stamp})`;
  }
  return "";
}

/** Calm, informational context (NOT problems): an absent allocated_capital
 * key (harmless — the engine defaults it), or the legacy pre-migration
 * peak_capital the drawdown is measured against. Array of strings, possibly
 * empty. Rendered as a low-key note, never a red error. */
export function accountInfoNotes(acct) {
  if (!acct || !Array.isArray(acct.account_notes)) return [];
  return acct.account_notes.filter((n) => typeof n === "string" && n.length);
}

/** "Account Total" card: the verified broker account value, or the literal
 * word "Unavailable" — never cash mislabelled as a total. */
export function accountTotalDisplay(acct) {
  if (acct && acct.total_value !== null && acct.total_value !== undefined) {
    return { value: money(acct.total_value), unavailable: false };
  }
  return { value: "Unavailable", unavailable: true };
}

/** "Unmanaged holdings" card: count, plus value only when the broker total
 * was verified (otherwise the value is unknown, not zero). */
export function unmanagedHoldingsDisplay(acct) {
  const uh = (acct && acct.unmanaged_holdings) || {};
  const count = uh.count || 0;
  if (!count) return { text: "0", hasValue: false };
  if (uh.value !== null && uh.value !== undefined) {
    return { text: `${count} · ${money(uh.value)}`, hasValue: true };
  }
  return { text: `${count} · value unverified`, hasValue: false };
}

/** Short "synced <when>" / "never synced" label for the broker snapshot. */
export function syncedAtLabel(acct) {
  const ts = acct && acct.broker_synced_at;
  return ts ? `synced ${dt(ts)}` : "never synced";
}
