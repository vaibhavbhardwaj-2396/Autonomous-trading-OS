"""
The agent's candidate sets — captured WITHOUT an engine -> research dependency.

The invariant stays absolute:

    engine  ──X──>  research
    research ────>  engine

engine/briefing.py already writes a complete briefing to
`logs/briefing-{cycle}-{YYYYMMDD-HHMMSS}.md` on every run, containing the regime
call and every screened candidate with the indicators that justified it.
run_cycle.sh then deletes those files after 60 days.

So this module reads the artifact the trading system already produces. Data
handoff, not code dependency. Nothing in engine/ changes, nothing in engine/
learns that research exists, and the architectural rule survives intact — which
is worth considerably more than the three lines it would have taken to import
directly.

The honest cost
---------------
Parsing a rendered document is more fragile than reading a structured record. If
briefing.py's markdown changes, this parser goes quietly blind.

So it does not fail silently. Every run reports `blocks_seen` against
`blocks_parsed`, and the recorder writes both to recorder_runs.jsonl. A format
change shows up as a widening gap between two numbers on the next run, not as a
dataset that simply stops growing while everything reports green — which is the
same class of failure the option-chain smoke test caught, and it deserves the
same treatment.

If that fragility ever becomes a real problem, the fix is for briefing.py to
append a JSONL line through engine's own journal.record() — still a data
handoff, still no import, just a more stable format. This module would then read
that file instead.
"""

from __future__ import annotations

import re
import datetime as dt
from pathlib import Path
from typing import Optional

from ..store import Store, to_dt, IST

PROJECT_ROOT = Path(__file__).parent.parent.parent
LOG_DIR = PROJECT_ROOT / "logs"

DATASET = "agent_candidate_set"
REGIME_DATASET = "agent_briefing_regime"
SOURCE = "briefing_artifact"

# logs/briefing-premarket-20260902-083012.md
FILENAME_RE = re.compile(r"briefing-([a-z_]+)-(\d{8})-(\d{6})\.md$")

CANDIDATE_RE = re.compile(
    r"^###\s+(?P<symbol>[A-Z0-9&\-\.]+)\s+—\s+score\s+(?P<score>[-\d.]+)\s+"
    r"\((?P<playbook>[a-z_]+)\)\s*$")
LEVELS_RE = re.compile(
    r"Entry\s+₹(?P<entry>[\d,\.]+)\s*\|\s*stop\s+₹(?P<stop>[\d,\.]+)\s*\|\s*"
    r"target\s+₹(?P<target>[\d,\.]+)\s*\|\s*R:R\s+(?P<rr>[-\d.]+)")
INDICATORS_RE = re.compile(
    r"RSI\s+(?P<rsi14>[-\d.None]+)\s*\|\s*ADX\s+(?P<adx14>[-\d.None]+)\s*\|\s*"
    r"ATR%\s+(?P<atr_pct>[-\d.None]+)\s*\|\s*vol\s+(?P<volume_ratio>[-\d.None]+)x"
    r"\s*\|\s*20d\s+(?P<return_20d>[-\d.None]+)%\s*\|\s*"
    r"liquidity\s+₹(?P<avg_traded_value_20d_cr>[-\d.None]+)cr")
REGIME_RE = re.compile(
    r"^-\s+\*\*(?P<regime>[A-Z_]+)\*\*\s+\(confidence:\s+(?P<confidence>\w+)\)"
    r"\s+→\s+playbook:\s+`(?P<playbook>[a-z_]+)`")
GUARDRAIL_RE = re.compile(r"^-\s+Guardrail check:\s+(?P<verdict>.+)$")
WHY_RE = re.compile(r"^-\s+Why:\s+(?P<why>.+)$")


def _f(v) -> Optional[float]:
    if v is None:
        return None
    s = str(v).replace(",", "").strip()
    if s in ("", "None", "—", "-"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def timestamp_from_name(path: Path) -> Optional[dt.datetime]:
    """The run's own timestamp, from the filename run_cycle.sh generated.

    Preferred over the date line inside the document: the filename is produced
    by the shell at run time and cannot be reflowed, reformatted or localised.
    """
    m = FILENAME_RE.search(path.name)
    if not m:
        return None
    try:
        return dt.datetime.strptime(m.group(2) + m.group(3), "%Y%m%d%H%M%S").replace(
            tzinfo=IST)
    except ValueError:
        return None


def cycle_from_name(path: Path) -> str:
    m = FILENAME_RE.search(path.name)
    return m.group(1) if m else "unknown"


def parse(text: str, run_at: dt.datetime, cycle: str = "unknown") -> dict:
    """Briefing markdown -> rows + parse coverage.

    knowledge_time == run_at. The agent knew what its own briefing said at the
    moment the briefing was generated; there is no publication lag to model.
    """
    lines = text.splitlines()

    regime = confidence = playbook = ""
    for line in lines:
        m = REGIME_RE.match(line.strip())
        if m:
            regime, confidence, playbook = (m.group("regime"), m.group("confidence"),
                                            m.group("playbook"))
            break

    rows: list[dict] = []
    blocks_seen = 0
    i = 0
    while i < len(lines):
        head = CANDIDATE_RE.match(lines[i].strip())
        if not head:
            i += 1
            continue

        blocks_seen += 1
        block = lines[i + 1: i + 8]
        levels = indicators = guard = why = None
        for b in block:
            b = b.strip()
            if b.startswith("### "):
                break
            levels = levels or LEVELS_RE.search(b)
            indicators = indicators or INDICATORS_RE.search(b)
            guard = guard or GUARDRAIL_RE.match(b)
            why = why or WHY_RE.match(b)

        # A block whose levels did not parse is a block whose format moved.
        # Counting it as seen-but-not-parsed is what makes the drift visible.
        if not levels:
            i += 1
            continue

        ind = {k: _f(v) for k, v in indicators.groupdict().items()} if indicators else {}
        rows.append({
            "dataset": DATASET,
            "entity": head.group("symbol"),
            "event_time": run_at,
            "knowledge_time": run_at,
            "source": SOURCE,
            "confidence": "observed",
            "payload": {
                "cycle": cycle,
                "regime": regime,
                "regime_confidence": confidence,
                "playbook": head.group("playbook") or playbook,
                "symbol": head.group("symbol"),
                "score": _f(head.group("score")),
                "suggested_entry": _f(levels.group("entry")),
                "suggested_stop": _f(levels.group("stop")),
                "suggested_target": _f(levels.group("target")),
                "reward_risk": _f(levels.group("rr")),
                "guardrail_verdict": guard.group("verdict") if guard else None,
                "reasons": ([r.strip() for r in why.group("why").split(";")]
                            if why else None),
                "indicators": ind,
            },
        })
        i += 1

    regime_rows = []
    if regime:
        regime_rows.append({
            "dataset": REGIME_DATASET,
            "entity": "_market",
            "event_time": run_at,
            "knowledge_time": run_at,
            "source": SOURCE,
            "confidence": "observed",
            "payload": {"cycle": cycle, "regime": regime,
                        "confidence": confidence, "playbook": playbook,
                        "candidates_surfaced": len(rows)},
        })

    return {"candidates": rows, "regime": regime_rows,
            "blocks_seen": blocks_seen, "blocks_parsed": len(rows)}


def load(store: Store, log_dir: Path = LOG_DIR, limit: Optional[int] = None) -> dict:
    if not log_dir.exists():
        return {"rows_seen": 0, "rows_new": 0, "files": 0,
                "note": f"no log directory at {log_dir}"}

    files = sorted(log_dir.glob("briefing-*.md"))
    if limit:
        files = files[-limit:]

    seen = new = 0
    blocks_seen = blocks_parsed = 0
    skipped: list[str] = []

    for f in files:
        run_at = timestamp_from_name(f)
        if run_at is None:
            skipped.append(f.name)
            continue
        try:
            p = parse(f.read_text(), run_at, cycle_from_name(f))
        except Exception:
            skipped.append(f.name)
            continue
        blocks_seen += p["blocks_seen"]
        blocks_parsed += p["blocks_parsed"]
        rows = p["candidates"] + p["regime"]
        seen += len(rows)
        new += store.append_many(rows)

    coverage = round(100 * blocks_parsed / blocks_seen, 1) if blocks_seen else 100.0
    result = {
        "rows_seen": seen, "rows_new": new, "files": len(files),
        "candidate_blocks_seen": blocks_seen,
        "candidate_blocks_parsed": blocks_parsed,
        "parse_coverage_pct": coverage,
        "unreadable_files": skipped[:10],
    }
    # Loud, not silent. A parser that has gone blind reports a number that fell,
    # rather than a dataset that stopped growing while everything says green.
    if blocks_seen and coverage < 90:
        result["WARNING"] = (
            f"Only {coverage}% of candidate blocks parsed. briefing.py's format "
            f"has probably changed — fix the regexes in this module before the "
            f"60-day log retention deletes the evidence.")
    return result
