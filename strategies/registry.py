"""
strategies/registry.py — minimal file-based registry for StrategyVersion.

Mirrors research/contracts.py's registry shape (JSON files in a sibling
`registry/` directory, save/load/list, verify-on-load) without importing
anything from research/ or engine/ — see strategies/core.py's module
docstring for the full boundary rationale this package exists under.

Deliberately narrow, per this slice's own scope: save, load, list. Nothing
here executes a strategy, modifies a strategy's parameters, promotes or
approves a version, connects to a broker, or creates an order. There is no
status field, no lifecycle transition, and no lifecycle-changing function
in this module — a StrategyVersion's presence in the registry IS the only
state this slice represents (informally, "DEFINED/REGISTERED"; see the
completion report for why an explicit status field was judged unnecessary
for that one state).
"""

from __future__ import annotations

import json
from pathlib import Path

from .core import StrategyVersion, StrategyVersionViolation

REGISTRY_DIR = Path(__file__).parent / "registry"


def save_version(version: StrategyVersion, directory: Path = REGISTRY_DIR) -> Path:
    """Persist `version` as `<version_id>.json`. Verifies internal
    self-consistency BEFORE writing (fail closed — nothing is written for
    an already-inconsistent object), and if a file already exists at this
    exact version_id, requires its content to match byte-for-byte rather
    than silently overwriting it — a version_id collision with different
    content should be structurally impossible (it would mean the hash
    function itself disagrees with itself), so treating it as an error
    rather than "last write wins" is the safe default.

    Saving the identical version twice is a harmless no-op, not an error —
    this makes the function idempotent, which matters for a caller that
    re-registers the same StrategyVersion across multiple runs without
    having to track "have I already saved this" itself.
    """
    version.verify()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{version.version_id}.json"
    payload = json.dumps(version.to_dict(), indent=2, sort_keys=True)
    if path.exists():
        existing = json.loads(path.read_text())
        if existing != version.to_dict():
            raise StrategyVersionViolation(
                f"{version.version_id} already exists in the registry with "
                f"different content — refusing to overwrite. A version_id "
                f"is a content hash; two different contents should never "
                f"produce the same id. This indicates registry corruption "
                f"or a change to the hashing scheme itself.")
        return path
    path.write_text(payload)
    return path


def load_version(version_id: str, directory: Path = REGISTRY_DIR) -> StrategyVersion:
    """Load and verify `version_id`. Raises StrategyVersionViolation (fail
    closed, never silently repaired) if the on-disk file has been
    hand-edited or corrupted since it was saved — the same posture
    research.contracts.Contract.load() + .verify() already takes for
    Contracts, applied here to StrategyVersion instead."""
    path = directory / f"{version_id}.json"
    raw = json.loads(path.read_text())
    version = StrategyVersion(**raw)
    version.verify()
    return version


def list_versions(directory: Path = REGISTRY_DIR) -> list[StrategyVersion]:
    """Every StrategyVersion currently in the registry, sorted by
    version_id for a deterministic order independent of filesystem glob
    order. Purely a read: never writes, never executes, never mutates a
    file. A file that fails to load or verify is silently skipped (the same
    defensive posture research.contracts.registry() already takes toward a
    malformed registry file) rather than raising and blocking every other,
    valid entry from being listed."""
    if not directory.exists():
        return []
    out = []
    for p in sorted(directory.glob("*.json")):
        try:
            out.append(load_version(p.stem, directory))
        except Exception:
            continue
    return out


def versions_for_strategy(
    strategy_id: str, directory: Path = REGISTRY_DIR,
) -> list[StrategyVersion]:
    """Convenience filter over list_versions() — no separate query path or
    index file, so this can never disagree with what list_versions() itself
    returns."""
    return [v for v in list_versions(directory) if v.strategy_id == strategy_id]
