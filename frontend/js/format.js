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
