"""
Performance statistics — computed, never estimated.

The weekly review depends on these numbers to decide whether a strategy rule survives.
That makes them too important to leave to a model reading prose out of a markdown log:
recollection is survivorship-biased, arithmetic-by-language-model is unreliable, and a
wrong expectancy number would silently justify the wrong change.

So: everything here is computed from memory/trades.jsonl, the append-only structured
record. Results are expressed in R-multiples (1R = the amount risked on that trade),
which makes trades of different sizes comparable.

    python -m engine.stats                 # all time
    python -m engine.stats --days 7        # this week
    python -m engine.stats --json          # machine-readable
"""

from __future__ import annotations

import json
import argparse
import datetime as dt
from pathlib import Path
from collections import defaultdict
from statistics import mean, pstdev

PROJECT_ROOT = Path(__file__).parent.parent
TRADES_JSONL = PROJECT_ROOT / "memory" / "trades.jsonl"

# Below this many trades of a given type, results are noise. memory/review_process.md
# makes this binding: no performance-based rule change under this sample size.
MIN_SAMPLE_FOR_DECISIONS = 20


def _load(path: Path = TRADES_JSONL) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # never let one malformed line hide the rest of the history
    return rows


def _within(row: dict, since: dt.datetime | None) -> bool:
    if since is None:
        return True
    try:
        return dt.datetime.fromisoformat(row["ts"]).replace(tzinfo=None) >= since
    except (KeyError, ValueError):
        return False


def compute(days: int | None = None) -> dict:
    """Full statistics pack. `days=None` means all time."""
    since = None
    if days:
        since = dt.datetime.now() - dt.timedelta(days=days)

    rows = [r for r in _load() if _within(r, since)]
    exits = [r for r in rows if r.get("kind") == "EXIT"]
    entries = [r for r in rows if r.get("kind") == "ENTRY"]
    rejections = [r for r in rows if r.get("kind") == "REJECTED"]

    if not exits:
        return {
            "period_days": days,
            "closed_trades": 0,
            "entries": len(entries),
            "rejections": len(rejections),
            "verdict": "No closed trades in this period — nothing to compute. "
                       "This is normal early on and is not a bad sign.",
            "sufficient_sample": False,
        }

    r_values = [float(e.get("r_multiple", 0.0)) for e in exits]
    pnls = [float(e.get("pnl", 0.0)) for e in exits]
    wins = [r for r in r_values if r > 0]
    losses = [r for r in r_values if r <= 0]

    win_rate = len(wins) / len(r_values)
    avg_win = mean(wins) if wins else 0.0
    avg_loss = abs(mean(losses)) if losses else 0.0
    expectancy = (win_rate * avg_win) - ((1 - win_rate) * avg_loss)

    # Equity curve in R, for max drawdown
    equity, peak, max_dd = 0.0, 0.0, 0.0
    for r in r_values:
        equity += r
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)

    result = {
        "period_days": days,
        "closed_trades": len(exits),
        "entries": len(entries),
        "rejections": len(rejections),
        "win_rate_pct": round(win_rate * 100, 1),
        "avg_win_R": round(avg_win, 3),
        "avg_loss_R": round(avg_loss, 3),
        "expectancy_R": round(expectancy, 3),
        "total_R": round(sum(r_values), 2),
        "total_pnl": round(sum(pnls), 2),
        "best_R": round(max(r_values), 2),
        "worst_R": round(min(r_values), 2),
        "r_stdev": round(pstdev(r_values), 3) if len(r_values) > 1 else 0.0,
        "max_drawdown_R": round(max_dd, 2),
        "sufficient_sample": len(exits) >= MIN_SAMPLE_FOR_DECISIONS,
        "min_sample_required": MIN_SAMPLE_FOR_DECISIONS,
    }

    # Expectancy is only meaningful relative to its own noise. With few trades, a
    # positive number means very little — this makes that explicit rather than implied.
    if len(r_values) > 1 and result["r_stdev"] > 0:
        result["expectancy_t_stat"] = round(
            expectancy / (result["r_stdev"] / (len(r_values) ** 0.5)), 2
        )

    result["by_regime"] = _breakdown(exits, "regime")
    result["by_playbook"] = _breakdown(exits, "playbook")
    result["by_symbol"] = _breakdown(exits, "symbol")
    result["verdict"] = _verdict(result)
    return result


def _breakdown(exits: list[dict], field: str) -> dict:
    """Expectancy sliced by a dimension — this is where the edge shows up or doesn't."""
    groups: dict[str, list[float]] = defaultdict(list)
    for e in exits:
        key = e.get(field) or "unknown"
        groups[key].append(float(e.get("r_multiple", 0.0)))

    out = {}
    for key, rs in sorted(groups.items()):
        w = [r for r in rs if r > 0]
        l = [r for r in rs if r <= 0]
        wr = len(w) / len(rs)
        aw = mean(w) if w else 0.0
        al = abs(mean(l)) if l else 0.0
        out[key] = {
            "trades": len(rs),
            "win_rate_pct": round(wr * 100, 1),
            "expectancy_R": round((wr * aw) - ((1 - wr) * al), 3),
            "total_R": round(sum(rs), 2),
            "sufficient_sample": len(rs) >= MIN_SAMPLE_FOR_DECISIONS,
        }
    return out


def _verdict(s: dict) -> str:
    """A plain-language read, deliberately conservative about small samples."""
    n, e = s["closed_trades"], s["expectancy_R"]

    if n < MIN_SAMPLE_FOR_DECISIONS:
        return (
            f"{n} closed trades — below the {MIN_SAMPLE_FOR_DECISIONS}-trade minimum. "
            f"Expectancy reads {e:+.3f}R but at this sample size that is NOISE, whether "
            f"positive or negative. No performance-based strategy change is permitted. "
            f"The correct action is to keep following the current rules and gather data."
        )

    t = s.get("expectancy_t_stat", 0)
    if e > 0 and t > 2:
        return (
            f"Expectancy {e:+.3f}R over {n} trades (t≈{t}). Statistically meaningful and "
            f"positive — evidence of a real edge. Keep going; change nothing without a "
            f"specific reason."
        )
    if e > 0:
        return (
            f"Expectancy {e:+.3f}R over {n} trades, but t≈{t} — positive yet not clearly "
            f"distinguishable from luck. Encouraging, not conclusive. Keep gathering."
        )
    return (
        f"Expectancy {e:+.3f}R over {n} trades — negative on a sufficient sample. This is "
        f"real evidence the current approach is not working. Review which setup or regime "
        f"is responsible (see the breakdowns) and drop or revise it. Stopping is a valid "
        f"outcome."
    )


def render(days: int | None = None) -> str:
    s = compute(days)
    period = f"last {days} days" if days else "all time"
    L = [f"# Performance — {period}", ""]

    if not s["closed_trades"]:
        L += [s["verdict"], "",
              f"- Entries logged: {s['entries']}",
              f"- Trades rejected by guardrails: {s['rejections']}"]
        return "\n".join(L)

    L += [
        "| Metric | Value |", "|---|---|",
        f"| Closed trades | {s['closed_trades']} |",
        f"| Win rate | {s['win_rate_pct']}% |",
        f"| Average win | {s['avg_win_R']:+.3f}R |",
        f"| Average loss | −{s['avg_loss_R']:.3f}R |",
        f"| **Expectancy** | **{s['expectancy_R']:+.3f}R per trade** |",
        f"| Total | {s['total_R']:+.2f}R (₹{s['total_pnl']:,.2f}) |",
        f"| Best / worst | {s['best_R']:+.2f}R / {s['worst_R']:+.2f}R |",
        f"| Std dev of R | {s['r_stdev']:.3f} |",
        f"| Max drawdown | {s['max_drawdown_R']:.2f}R |",
        f"| Rejected by guardrails | {s['rejections']} |",
        "",
        f"**Verdict:** {s['verdict']}", "",
    ]

    for title, key in [("By regime", "by_regime"), ("By playbook", "by_playbook"),
                       ("By symbol", "by_symbol")]:
        if not s.get(key):
            continue
        L += [f"## {title}", "",
              "| Group | Trades | Win rate | Expectancy | Total | Sample OK? |",
              "|---|---|---|---|---|---|"]
        for name, g in s[key].items():
            ok = "✅" if g["sufficient_sample"] else f"❌ need {MIN_SAMPLE_FOR_DECISIONS}"
            L.append(
                f"| {name} | {g['trades']} | {g['win_rate_pct']}% | "
                f"{g['expectancy_R']:+.3f}R | {g['total_R']:+.2f}R | {ok} |"
            )
        L.append("")

    L += ["> Groups marked ❌ do not have enough trades for a rule change to be "
          "justified on their numbers. Restraint on small samples is the whole point."]
    return "\n".join(L)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=None)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    print(json.dumps(compute(args.days), indent=2) if args.json else render(args.days))


if __name__ == "__main__":
    main()
