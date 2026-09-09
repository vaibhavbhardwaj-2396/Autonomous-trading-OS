"""
Two-way Telegram — the control channel.

Until now Telegram was send-only, which left two real gaps:
  - The drawdown ladder requires Vaibhav's acknowledgement to resume after Amber/Orange,
    but there was no mechanism to give it short of SSH-ing into the box.
  - Overlap approvals (letting the agent trade a symbol Vaibhav already holds) needed a
    decision channel.

This polls for messages from the configured chat and applies a deliberately small command
set. Run at the start of every cycle, before the agent thinks.

SECURITY MODEL — read before extending:
  - Commands are accepted ONLY from TELEGRAM_CHAT_ID. Anything else is ignored silently.
  - The command set can pause, resume, and approve specific symbol overlaps. It can NOT
    change any risk limit, capital allocation, or guardrail. Those stay file-edits by a
    human on the box, on purpose: a chat message is too easy to send carelessly, and an
    attacker with the bot token should not be able to widen the risk envelope.
  - Every processed command is logged.

    python scripts/telegram_inbox.py            # process new commands
    python scripts/telegram_inbox.py --report   # process, then send a status reply
"""

from __future__ import annotations

import os
import sys
import json
import argparse
from pathlib import Path

import requests
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).parent.parent
load_dotenv(PROJECT_ROOT / ".env")
sys.path.insert(0, str(PROJECT_ROOT))

from engine import guardrails as gr          # noqa: E402
from engine import journal as jr             # noqa: E402
from scripts.telegram_notify import send_message  # noqa: E402

OFFSET_FILE = PROJECT_ROOT / "memory" / ".telegram_offset"

HELP = """Commands I understand:

• `status` — capital, drawdown, positions, guardrail state
• `pause [reason]` — stop opening new positions
• `resume` — acknowledge and resume (needed after Amber/Orange drawdown)
• `approve SYMBOL` — allow trading a stock you already hold (see warning)
• `unapprove SYMBOL` — revoke that permission
• `positions` — the agent's own open positions
• `token <value>` — store today's INDstocks access token (expires every 24h)
• `stats` — performance to date
• `help` — this message

I can't change risk limits or capital from here — those are deliberate file edits."""


def _get_updates(token: str, offset: int | None) -> list[dict]:
    params = {"timeout": 0}
    if offset is not None:
        params["offset"] = offset
    r = requests.get(f"https://api.telegram.org/bot{token}/getUpdates",
                     params=params, timeout=20)
    r.raise_for_status()
    return r.json().get("result", [])


def _load_offset() -> int | None:
    if OFFSET_FILE.exists():
        try:
            return int(OFFSET_FILE.read_text().strip())
        except ValueError:
            return None
    return None


def _save_offset(v: int) -> None:
    OFFSET_FILE.write_text(str(v))


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_status() -> str:
    s = gr.status_summary()
    snap = gr.load_state().get("broker_snapshot", {}) or {}
    lines = [
        f"📊 *Status*",
        f"Allocation: ₹{gr.load_state().get('allocated_capital', 0):,.0f}",
        f"Book value: ₹{s['capital']:,.2f} (peak ₹{s['peak_capital']:,.2f})",
        f"Spendable: ₹{s['cash_available']:,.2f}",
        f"Drawdown: {s['drawdown_pct']:.2f}% — {s['drawdown_level']}",
        f"Risk/trade: {s['risk_per_trade_pct']:.1f}% (₹{s['risk_budget_per_trade']:,.0f})",
        f"Positions: {s['open_positions']}/{s['max_open_positions']}",
        f"Can open new: {'yes' if s['can_open_new_positions'] else 'NO'}",
    ]
    if not s["can_open_new_positions"]:
        for r in s["blocking_reasons"]:
            lines.append(f"  • {r}")
    if snap.get("free_cash") is not None:
        lines.append(f"Broker free cash: ₹{snap['free_cash']:,.2f}")
    return "\n".join(lines)


def cmd_pause(reason: str) -> str:
    jr.set_pause(True, reason or "paused from Telegram", awaiting_ack=True)
    return f"⏸ Paused. No new positions until you send `resume`.\nReason: {reason or '—'}"


def cmd_resume() -> str:
    state = gr.load_state()
    level = gr.drawdown_level(state)
    jr.set_pause(False, "", awaiting_ack=False, state=state)

    msg = "▶️ Resumed — acknowledgement recorded."
    if level in ("ORANGE", "RED"):
        msg += (f"\n\n⚠️ Note: drawdown level is still *{level}*. Risk per trade stays at "
                f"1% until capital recovers to within 5% of peak. That de-risking is not "
                f"lifted by resuming.")
    if level == "RED":
        msg += ("\n\n🔴 At RED the guardrails still refuse new positions. Resuming does "
                "not override the hard floor — that needs a decision recorded in "
                "guardrails.md by you on the box.")
    return msg


def cmd_approve(symbol: str) -> str:
    if not symbol:
        return "Usage: `approve SYMBOL`"
    symbol = symbol.upper()
    state = gr.load_state()
    approved = set(state.get("overlap_approved_symbols", []))
    approved.add(symbol)
    state["overlap_approved_symbols"] = sorted(approved)
    gr.save_state(state)
    return (
        f"✅ {symbol} approved for overlap.\n\n"
        f"⚠️ You're accepting two things:\n"
        f"1. *FIFO tax lots* — when the agent exits, the tax treatment sells your OLDEST "
        f"shares first, so it realises your gains on your return, not the agent's.\n"
        f"2. *Rug risk* — if you sell your own {symbol} while the agent holds a position, "
        f"its stop may fail or cause short delivery.\n\n"
        f"Send `unapprove {symbol}` to revoke."
    )


def cmd_unapprove(symbol: str) -> str:
    if not symbol:
        return "Usage: `unapprove SYMBOL`"
    symbol = symbol.upper()
    state = gr.load_state()
    approved = set(state.get("overlap_approved_symbols", []))
    approved.discard(symbol)
    state["overlap_approved_symbols"] = sorted(approved)
    gr.save_state(state)
    return f"🚫 {symbol} overlap revoked. The agent will refuse to trade it."


def cmd_token(value: str) -> str:
    """The daily INDstocks token. Stored, never echoed back — a credential repeated into
    a chat log is a credential leaked."""
    if not value:
        from scripts.indstocks_auth import TOKEN_INSTRUCTIONS, token_status
        st = token_status()
        head = (f"Current token: valid, {st['expires_in_hours']}h left."
                if st.get("valid") else f"No valid token ({st.get('reason')}).")
        return f"{head}\n\n{TOKEN_INSTRUCTIONS}"

    from scripts.indstocks_auth import save_token
    try:
        save_token(value)
    except ValueError as e:
        return f"❌ {e}"

    try:
        from engine.broker_indstocks import INDstocksBroker
        funds = INDstocksBroker().funds()
        return (f"✅ Token stored and verified — available funds ₹{funds:,.2f}.\n"
                f"Valid for 24 hours.")
    except Exception as e:
        return (f"⚠️ Token stored, but the broker call failed: {type(e).__name__}. "
                f"It may be mistyped or already expired.")


def cmd_positions() -> str:
    state = gr.load_state()
    pos = state.get("open_positions", [])
    if not pos:
        return "No open positions."
    out = ["📈 *Agent positions*"]
    for p in pos:
        out.append(
            f"{p['symbol']} {p['side']} ×{p['quantity']} @ ₹{p['entry']:,.2f} "
            f"| stop ₹{p['stop']:,.2f} | target ₹{p['target']:,.2f}"
        )
    return "\n".join(out)


def cmd_stats() -> str:
    try:
        from engine import stats
        s = stats.compute()
        if not s["closed_trades"]:
            return f"No closed trades yet.\n{s['verdict']}"
        return (
            f"📊 {s['closed_trades']} trades | win {s['win_rate_pct']}% | "
            f"expectancy {s['expectancy_R']:+.3f}R | total {s['total_R']:+.2f}R\n\n"
            f"{s['verdict']}"
        )
    except Exception as e:
        return f"Couldn't compute stats: {type(e).__name__}: {e}"


def handle(text: str) -> str | None:
    parts = text.strip().split()
    if not parts:
        return None
    cmd = parts[0].lower().lstrip("/")
    arg = parts[1] if len(parts) > 1 else ""
    rest = " ".join(parts[1:])

    if cmd == "status":
        return cmd_status()
    if cmd == "pause":
        return cmd_pause(rest)
    if cmd == "resume":
        return cmd_resume()
    if cmd == "approve":
        return cmd_approve(arg)
    if cmd == "unapprove":
        return cmd_unapprove(arg)
    if cmd == "positions":
        return cmd_positions()
    if cmd == "token":
        return cmd_token(rest)
    if cmd == "stats":
        return cmd_stats()
    if cmd in ("help", "start"):
        return HELP
    return None  # not a command — ignore chatter silently


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", action="store_true",
                    help="send a status message after processing")
    args = ap.parse_args()

    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = str(os.environ.get("TELEGRAM_CHAT_ID", ""))
    if not token or not chat_id:
        print("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set", file=sys.stderr)
        return 1

    try:
        updates = _get_updates(token, _load_offset())
    except Exception as e:
        print(f"getUpdates failed: {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    processed = 0
    for u in updates:
        _save_offset(u["update_id"] + 1)
        msg = u.get("message") or u.get("edited_message")
        if not msg:
            continue
        # Only the configured chat may issue commands.
        if str(msg.get("chat", {}).get("id")) != chat_id:
            continue
        text = msg.get("text", "")
        reply = handle(text)
        if reply:
            send_message(reply)
            print(f"handled: {text[:60]}")
            processed += 1

    if args.report:
        send_message(cmd_status())

    print(json.dumps({"updates_seen": len(updates), "commands_handled": processed}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
