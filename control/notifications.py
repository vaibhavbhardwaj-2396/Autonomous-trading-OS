"""Audited, credential-free notification policy and delivery telemetry."""

from __future__ import annotations

import datetime as dt
import fcntl
import json
from pathlib import Path

CONTROL_DIR = Path(__file__).resolve().parent
STATE_PATH = CONTROL_DIR / "notification_config.json"
LOCK_PATH = CONTROL_DIR / ".notification_config.lock"
AUDIT_PATH = CONTROL_DIR / "notification_audit.jsonl"
_IST = dt.timezone(dt.timedelta(hours=5, minutes=30))

CATEGORIES = ("SYSTEM", "RESEARCH", "EVIDENCE", "STRATEGY", "PAPER", "RISK", "ERROR", "DAILY_DIGEST")


def now_iso() -> str:
    return dt.datetime.now(_IST).isoformat(timespec="seconds")


def default_config() -> dict:
    return {
        "enabled": True,
        "categories": {name: True for name in CATEGORIES},
        "thresholds": {"research_milestone_count": 25, "repeated_error_count": 3},
        "changed_at": None, "changed_by": None, "reason": None, "history": [],
    }


def get_config(*, path: Path = STATE_PATH) -> dict:
    base = default_config()
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return base
    if not isinstance(value, dict):
        return base
    base.update({k: v for k, v in value.items() if k != "categories"})
    base["categories"].update(value.get("categories") or {})
    base["thresholds"].update(value.get("thresholds") or {})
    return base


def set_config(*, enabled: bool, categories: dict, thresholds: dict,
               actor: str, reason: str, path: Path = STATE_PATH,
               lock_path: Path = LOCK_PATH) -> dict:
    if not actor.strip() or not reason.strip():
        raise ValueError("actor and reason are required")
    unknown = set(categories) - set(CATEGORIES)
    if unknown or any(not isinstance(v, bool) for v in categories.values()):
        raise ValueError(f"invalid notification categories: {sorted(unknown)}")
    clean_thresholds = {}
    for key in ("research_milestone_count", "repeated_error_count"):
        value = thresholds.get(key, default_config()["thresholds"][key])
        if not isinstance(value, int) or value < 1 or value > 10000:
            raise ValueError(f"{key} must be an integer from 1 to 10000")
        clean_thresholds[key] = value
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        old = get_config(path=path)
        history = list(old.get("history") or [])
        history.append({k: old.get(k) for k in ("enabled", "categories", "thresholds", "changed_at", "changed_by", "reason")})
        state = {"enabled": enabled, "categories": {**default_config()["categories"], **categories},
                 "thresholds": clean_thresholds, "changed_at": now_iso(),
                 "changed_by": actor.strip(), "reason": reason.strip(), "history": history[-20:]}
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
        tmp.replace(path)
        return state


def category_enabled(category: str, *, config: dict | None = None) -> bool:
    cfg = config or get_config()
    return bool(cfg.get("enabled")) and bool((cfg.get("categories") or {}).get(category, False))


def record_delivery(*, category: str, message_type: str, actor: str,
                    status: str, latency_ms: int | None, response: dict | None = None,
                    error: str | None = None, path: Path = AUDIT_PATH) -> dict:
    row = {"timestamp": now_iso(), "category": category, "message_type": message_type,
           "actor": actor, "status": status, "latency_ms": latency_ms,
           "telegram_response": response, "error": error}
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as out:
        out.write(json.dumps(row, sort_keys=True, default=str) + "\n")
    return row


def recent_audit(*, path: Path = AUDIT_PATH, limit: int = 200) -> list[dict]:
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(errors="replace").splitlines()[-limit:]:
        try:
            value = json.loads(line)
            if isinstance(value, dict): rows.append(value)
        except json.JSONDecodeError:
            continue
    return rows
