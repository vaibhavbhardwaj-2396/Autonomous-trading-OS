"""
Pre-run briefing.

Runs BEFORE the agent thinks, and packages everything it needs into one markdown file:
guardrail status, portfolio state, regime classification, screened candidates, and open
positions with their current prices.

Doing the deterministic work here keeps the agent's job to what it's actually good at —
judgment about markets and news — rather than arithmetic it might get subtly wrong.

    python -m engine.briefing --cycle premarket > /tmp/briefing.md
"""

from __future__ import annotations

import argparse
import datetime as dt

from . import guardrails as gr
from . import journal as jr
from . import market_data as md
from . import regime as rg
from . import screener as sc


def build(cycle: str, skip_scan: bool = False) -> str:
    lines: list[str] = []
    now = jr.now_ist()

    lines.append(f"# Run briefing — {cycle}")
    lines.append(f"_{now.strftime('%A %d %B %Y, %H:%M IST')}_\n")

    # --- 1. Broker reconciliation -------------------------------------------------
    lines.append("## 1. Broker sync")
    try:
        from .execute import sync_from_broker
        sync = sync_from_broker()
    except Exception as e:
        sync = {"ok": False, "error": f"{type(e).__name__}: {e}"}

    if not sync.get("ok"):
        lines.append(f"- ⛔ **Broker unreachable**: {sync.get('error')}")
        lines.append("- **This is a circuit-breaker condition. Do not place any trade "
                     "this run.** Research and logging only.\n")
    else:
        lines.append(
            f"- **Agent mandate: ₹{sync['allocated_capital']:,.2f} allocated** → "
            f"book value ₹{sync['agent_capital']:,.2f}"
        )
        lines.append(f"- Agent spendable cash: ₹{sync['agent_spendable_cash']:,.2f}")
        lines.append(
            f"- Agent's own positions: "
            f"{', '.join(sync['agent_positions']) if sync['agent_positions'] else 'none'}"
        )
        lines.append("")
        lines.append(
            f"- _Context only — the account also holds ₹{sync['account_total_value']:,.2f} "
            f"total across {sync['unmanaged_count']} personal holdings "
            f"(free cash ₹{sync['broker_free_cash']:,.2f})._"
        )
        if sync.get("unmanaged_symbols"):
            lines.append(
                f"- 🚫 **Off-limits — Vaibhav's own holdings, not the agent's to trade:** "
                f"{', '.join(sync['unmanaged_symbols'])}"
            )
            lines.append(
                "  These are NOT the agent's capital and NOT its positions. The guardrails "
                "will refuse any order in these symbols."
            )
        for w in sync.get("warnings", []):
            lines.append(f"- ⚠️ {w}")
        if sync.get("mismatches"):
            lines.append(f"- ⛔ **STATE MISMATCH**: {'; '.join(sync['mismatches'])}")
            lines.append("- **Do not open new positions until reconciled.**")
        else:
            lines.append("- ✅ The agent's own positions reconcile with the broker")
    lines.append("")

    # --- 2. Guardrails ------------------------------------------------------------
    status = gr.status_summary()
    lines.append("## 2. Guardrail status")
    if status["can_open_new_positions"]:
        lines.append("- ✅ **New positions permitted**")
    else:
        lines.append("- ⛔ **NEW POSITIONS BLOCKED**")
        for r in status["blocking_reasons"]:
            lines.append(f"  - {r}")
        lines.append("  - Managing/closing existing positions is still permitted.")

    lines.append(f"- Capital ₹{status['capital']:,.2f} | peak ₹{status['peak_capital']:,.2f}")
    lines.append(
        f"- Drawdown {status['drawdown_pct']:.2f}% — level **{status['drawdown_level']}** "
        f"(amber ₹{status['ladder_thresholds']['amber']:,.0f} / "
        f"orange ₹{status['ladder_thresholds']['orange']:,.0f} / "
        f"red ₹{status['ladder_thresholds']['red']:,.0f})"
    )
    lines.append(
        f"- Risk per trade: {status['risk_per_trade_pct']:.1f}% "
        f"= ₹{status['risk_budget_per_trade']:,.2f}"
    )
    lines.append(
        f"- Open positions {status['open_positions']}/{status['max_open_positions']} | "
        f"open risk ₹{status['total_open_risk']:,.2f} of ₹{status['max_total_open_risk']:,.2f} max"
    )
    lines.append(f"- Daily loss headroom: ₹{status['daily_loss_headroom']:,.2f}")
    lines.append(f"- Weekly loss headroom: ₹{status['weekly_loss_headroom']:,.2f}")
    lines.append(
        f"- Tier {status['tier']} → permitted: {', '.join(status['permitted_instruments'])}"
    )
    if status["consecutive_losing_days"]:
        lines.append(f"- ⚠️ Consecutive losing days: {status['consecutive_losing_days']}")
    lines.append("")

    # --- 3. Open positions --------------------------------------------------------
    state = gr.load_state()
    lines.append("## 3. Open positions")
    positions = state.get("open_positions", [])
    if not positions:
        lines.append("- None.\n")
    else:
        quotes = md.get_live_quotes([p["symbol"] for p in positions])
        lines.append("| Symbol | Side | Qty | Entry | Stop | Target | LTP | Unrealised | R |")
        lines.append("|---|---|---|---|---|---|---|---|---|")
        for p in positions:
            ltp = quotes.get(p["symbol"].upper())
            if ltp:
                direction = 1 if p["side"] == "BUY" else -1
                unreal = (ltp - p["entry"]) * p["quantity"] * direction
                r_mult = unreal / p["open_risk"] if p.get("open_risk") else 0
                ltp_s, unreal_s, r_s = f"₹{ltp:,.2f}", f"₹{unreal:,.2f}", f"{r_mult:+.2f}R"
            else:
                ltp_s = unreal_s = r_s = "—"
            lines.append(
                f"| {p['symbol']} | {p['side']} | {p['quantity']} | ₹{p['entry']:,.2f} | "
                f"₹{p['stop']:,.2f} | ₹{p['target']:,.2f} | {ltp_s} | {unreal_s} | {r_s} |"
            )
        lines.append("")
        for p in positions:
            lines.append(f"- **{p['symbol']}** thesis: {p.get('thesis', '—')}")
        lines.append("")

    # --- 4. Regime ----------------------------------------------------------------
    lines.append("## 4. Market regime")
    reg = rg.classify()
    lines.append(f"- **{reg.regime}** (confidence: {reg.confidence}) → playbook: `{reg.playbook}`")
    ev = reg.evidence
    lines.append(
        f"- Nifty {ev.get('nifty_close')} | EMA50 {ev.get('ema50')} | EMA200 {ev.get('ema200')} "
        f"| ADX {ev.get('adx14')}"
    )
    lines.append(
        f"- ATR {ev.get('atr_pct')}% (percentile {ev.get('atr_percentile_6m')}) "
        f"| VIX {ev.get('india_vix')} ({ev.get('vix_change_5d_pct')}% 5d) "
        f"| gap {ev.get('gap_pct')}%"
    )
    lines.append(f"- 5d {ev.get('return_5d_pct')}% | 20d {ev.get('return_20d_pct')}%")
    for n in reg.notes:
        lines.append(f"- {n}")

    shadow = reg.shadow or {}
    if shadow.get("available"):
        lines.append("")
        lines.append("**Shadow HMM (logged only — does NOT drive decisions):**")
        lines.append(
            f"- State **{shadow['regime']}** at {shadow['confidence']:.0%} posterior | "
            f"{shadow['probability_of_staying']:.0%} chance of persisting"
        )
        probs = ", ".join(f"{k} {v:.0%}" for k, v in shadow["state_probabilities"].items())
        lines.append(f"- Distribution: {probs}")
        lines.append(f"- {shadow['interpretation']}")
        lines.append(
            "- If this disagrees with the threshold call above, note it in the log — "
            "disagreements are the data that decides whether the HMM ever gets promoted."
        )
    lines.append("")

    # --- 5. Candidates ------------------------------------------------------------
    lines.append("## 5. Screened candidates")
    if skip_scan:
        lines.append("- _(scan skipped for this cycle)_\n")
    elif not status["can_open_new_positions"]:
        lines.append("- _(skipped — new positions are blocked, so there is nothing to screen for)_\n")
    elif reg.playbook in ("no_new_positions", "defensive"):
        lines.append(
            f"- _(none — regime playbook is `{reg.playbook}`; at Tier "
            f"{status['tier']} the correct action is usually cash)_\n"
        )
    else:
        candidates = sc.scan_for_playbook(reg.playbook)
        if not candidates:
            lines.append("- Nothing qualified today. A no-trade day is a valid outcome.\n")
        else:
            for c in candidates:
                sizing = gr.size_position(
                    c.suggested_entry, c.suggested_stop, c.suggested_target, state
                )
                verdict = (f"✅ size {sizing.quantity} (risk ₹{sizing.risk_amount:,.0f}, "
                           f"cost ₹{sizing.position_cost:,.0f})"
                           if sizing.approved else
                           f"❌ not sizeable: {'; '.join(sizing.reasons)}")
                lines.append(f"### {c.symbol} — score {c.score} ({c.playbook})")
                lines.append(
                    f"- Entry ₹{c.suggested_entry:,.2f} | stop ₹{c.suggested_stop:,.2f} "
                    f"| target ₹{c.suggested_target:,.2f} | R:R {c.reward_risk}"
                )
                lines.append(f"- Guardrail check: {verdict}")
                lines.append(f"- Why: {'; '.join(c.reasons)}")
                ci = c.indicators
                lines.append(
                    f"- RSI {ci.get('rsi14')} | ADX {ci.get('adx14')} | ATR% {ci.get('atr_pct')} "
                    f"| vol {ci.get('volume_ratio')}x | 20d {ci.get('return_20d')}% "
                    f"| liquidity ₹{ci.get('avg_traded_value_20d_cr')}cr/day"
                )
                lines.append("")

    # --- 6. Performance to date ---------------------------------------------------
    lines.append("## 6. Performance to date")
    try:
        from . import stats as st
        pack = st.compute()
        if pack["closed_trades"] == 0:
            lines.append(f"- {pack['verdict']}")
            lines.append(f"- Entries logged: {pack['entries']} | "
                         f"guardrail rejections: {pack['rejections']}")
        else:
            lines.append(
                f"- {pack['closed_trades']} closed trades | win rate "
                f"{pack['win_rate_pct']}% | **expectancy {pack['expectancy_R']:+.3f}R** | "
                f"total {pack['total_R']:+.2f}R"
            )
            lines.append(f"- {pack['verdict']}")
    except Exception as e:
        lines.append(f"- _(stats unavailable: {type(e).__name__}: {e})_")
    lines.append("")

    # --- 7. Reminders -------------------------------------------------------------
    lines.append("## 7. Non-negotiables for this run")
    lines.append("- The numbers above are already guardrail-checked. If something says "
                 "❌, it is not tradeable — do not look for a workaround.")
    lines.append("- Place trades ONLY via `python -m engine.execute propose ...`. It "
                 "re-validates independently and will refuse anything out of bounds.")
    lines.append("- Every entry needs a stop and target set at entry time. No exceptions.")
    lines.append("- Check the earnings calendar before entering — never hold through results.")
    lines.append("- Negative news vetoes a good chart. Positive news does not create a setup.")
    lines.append("- **No-trade is always a valid, unpunished outcome.**")

    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cycle", default="premarket")
    ap.add_argument("--skip-scan", action="store_true")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    text = build(args.cycle, args.skip_scan)
    if args.out:
        with open(args.out, "w") as f:
            f.write(text)
        print(f"Briefing written to {args.out}")
    else:
        print(text)


if __name__ == "__main__":
    main()
