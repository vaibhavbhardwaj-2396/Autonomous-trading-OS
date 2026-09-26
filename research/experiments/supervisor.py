"""Hard-deadline process supervisor for one historical experiment.

The simulation runs in its own process group and owns its own SQLite connection.
The parent can therefore terminate the complete computation, wait for resource
release, clean its result file, and reconcile the Contract without killing the
cron worker or leaking its lock.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional

from .. import memory as rm
from ..contracts import Contract, REGISTRY_DIR, registry
from ..store import Store, iso, now_ist
from . import runner

EXECUTION_LOG = Path(__file__).resolve().parents[1] / "experiment_executions.jsonl"
FAILURE_REASONS = ("TIMEOUT", "WORKER_CRASH", "DATA_FAILURE", "VALIDATION_FAILURE",
                   "RESOURCE_LIMIT", "OTHER")


def _append(row: dict, *, path: Path = EXECUTION_LOG) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    with open(lock_path, "w") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        with open(path, "a") as out:
            out.write(json.dumps(row, sort_keys=True, default=str) + "\n")


def _event(contract_id: str, state: str, *, reason: Optional[str] = None,
           detail: Optional[str] = None, pid: Optional[int] = None,
           deadline_seconds: Optional[float] = None, path: Path = EXECUTION_LOG) -> None:
    _append({"timestamp": iso(now_ist()), "contract_id": contract_id, "state": state,
             "failure_reason": reason, "detail": detail, "pid": pid,
             "deadline_seconds": deadline_seconds}, path=path)


def _abandon(contract_id: str, store: Store, registry_dir: Path, *, reason: str,
             detail: str) -> None:
    contract = Contract.load(contract_id, registry_dir)
    if contract.status in ("locked", "running"):
        contract.status = "abandoned"
        note = f"Execution abandoned [{reason}]: {detail}"
        contract.notes = f"{contract.notes}\n{note}".strip()
        contract.save(registry_dir)
        rm.record_research_note(store, note=note, source="research.experiments.supervisor",
                                extra={"contract_id": contract_id,
                                       "failure_reason": reason,
                                       "scientific_evidence": False})


def run_supervised(contract_id: str, store: Store, *, registry_dir: Path = REGISTRY_DIR,
                   deadline_seconds: float = 180.0,
                   execution_log: Path = EXECUTION_LOG) -> dict:
    """Run one experiment with a real wall-clock deadline and hard cleanup."""
    if deadline_seconds <= 0:
        raise ValueError("deadline_seconds must be > 0")
    fd, result_name = tempfile.mkstemp(prefix=f"lq-{contract_id}-", suffix=".json")
    os.close(fd)
    result_file = Path(result_name)
    result_file.unlink(missing_ok=True)
    cmd = [sys.executable, "-m", "research.experiments.supervisor", "--child",
           "--contract-id", contract_id, "--db", str(store.path),
           "--registry-dir", str(registry_dir), "--result", str(result_file)]
    _event(contract_id, "QUEUED", deadline_seconds=deadline_seconds, path=execution_log)
    proc = subprocess.Popen(cmd, start_new_session=True)
    _event(contract_id, "RUNNING", pid=proc.pid, deadline_seconds=deadline_seconds,
           path=execution_log)
    try:
        rc = proc.wait(timeout=deadline_seconds)
    except subprocess.TimeoutExpired:
        _event(contract_id, "CANCEL_REQUESTED", reason="TIMEOUT", pid=proc.pid,
               detail=f"deadline {deadline_seconds:.3f}s exceeded", path=execution_log)
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait(timeout=5)
        detail = f"hard deadline {deadline_seconds:.3f}s exceeded; child process group terminated"
        _abandon(contract_id, store, registry_dir, reason="TIMEOUT", detail=detail)
        _event(contract_id, "ABANDONED", reason="TIMEOUT", detail=detail,
               pid=proc.pid, path=execution_log)
        result_file.unlink(missing_ok=True)
        return {"status": "abandoned", "contract_id": contract_id,
                "failure_reason": "TIMEOUT", "detail": detail}
    finally:
        if proc.poll() is None:
            try: os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError: pass
            proc.wait()

    try:
        payload = json.loads(result_file.read_text()) if result_file.is_file() else {}
    finally:
        result_file.unlink(missing_ok=True)
    if rc == 0 and payload.get("status") == "reported":
        _event(contract_id, "COMPLETED", detail="experiment reported", pid=proc.pid,
               path=execution_log)
        return payload
    reason = payload.get("failure_reason") or "WORKER_CRASH"
    if reason not in FAILURE_REASONS: reason = "OTHER"
    detail = payload.get("detail") or f"child exited {rc} without a valid result"
    _abandon(contract_id, store, registry_dir, reason=reason, detail=detail)
    _event(contract_id, "FAILED", reason=reason, detail=detail, pid=proc.pid,
           path=execution_log)
    return {"status": "abandoned", "contract_id": contract_id,
            "failure_reason": reason, "detail": detail}


def recover_stale_running(store: Store, *, registry_dir: Path = REGISTRY_DIR,
                          execution_log: Path = EXECUTION_LOG) -> list[str]:
    """Fail closed for RUNNING contracts with no live recorded child PID."""
    latest = {}
    try:
        for line in execution_log.read_text().splitlines():
            row = json.loads(line); latest[row.get("contract_id")] = row
    except (OSError, json.JSONDecodeError):
        pass
    recovered = []
    for contract in registry(registry_dir):
        if contract.status != "running": continue
        row = latest.get(contract.id) or {}
        pid = row.get("pid")
        alive = False
        if isinstance(pid, int):
            try: os.kill(pid, 0); alive = True
            except (ProcessLookupError, PermissionError): pass
        if alive: continue
        detail = "stale RUNNING state found at worker restart; no surviving child process"
        _abandon(contract.id, store, registry_dir, reason="WORKER_CRASH", detail=detail)
        _event(contract.id, "ABANDONED", reason="WORKER_CRASH", detail=detail,
               path=execution_log)
        recovered.append(contract.id)
    return recovered


def _child(args) -> int:
    store = Store.open(Path(args.db))
    try:
        result = runner.run_experiment(args.contract_id, store,
                                       registry_dir=Path(args.registry_dir))
        Path(args.result).write_text(json.dumps(result, default=str))
        return 0
    except runner.RunnerRejected as exc:
        Path(args.result).write_text(json.dumps({"status":"abandoned",
            "failure_reason":"VALIDATION_FAILURE", "detail":str(exc)}))
        return 2
    except Exception as exc:
        Path(args.result).write_text(json.dumps({"status":"abandoned",
            "failure_reason":"OTHER", "detail":f"{type(exc).__name__}: {exc}"}))
        return 1
    finally:
        store.close()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--child", action="store_true")
    ap.add_argument("--contract-id", required=True)
    ap.add_argument("--db", required=True)
    ap.add_argument("--registry-dir", required=True)
    ap.add_argument("--result", required=True)
    args = ap.parse_args(argv)
    return _child(args) if args.child else 2


if __name__ == "__main__":
    raise SystemExit(main())
