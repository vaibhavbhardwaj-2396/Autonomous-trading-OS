"""
Strategy domain foundation — Phase 3 Slice X.

The bridge this slice starts building:

    Research Finding (research/, unchanged, untouched)
          |
          v
    deliberate Strategy mechanism        <- THIS PACKAGE
          |
          v
    StrategyVersion                      <- THIS PACKAGE, the reproducibility object
          |
          v
    future backtest / paper / live adapters      (NOT built here)

Nothing in `strategies/` trades, backtests, sizes a position, or touches a
broker. This slice is domain objects and a file-based registry only — the
same "define the shape first, wire it up later" discipline
research/contracts.py, research/brain/investigator.py and every other
phase of this project has followed from the start.

Critical architectural boundary
--------------------------------
`strategies/` is a top-level sibling of `engine/` and `research/`, and it
imports from NEITHER, on purpose:

    research backtest adapter  ---\
                                    >--->  strategies/
    engine live adapter        ---/

Both a future research-side backtest adapter and a future engine-side live
adapter will depend on this package to know what a Strategy IS and how to
identify a StrategyVersion. If this package depended back on either of
them, that arrow would point the wrong way — exactly the failure
tests/test_kernel_isolation.py already exists to catch for research/ vs.
engine/, extended here to a third package. See
tests/test_strategy_foundation.py's isolation section (and the small
addition to tests/test_kernel_isolation.py itself) for the enforcement.

Structurally true by absence, the same pattern every research/brain/*
module already uses: this file imports no `engine` module, no `research`
module, no broker module, no `research.store.Store`/`research.replay.Replay`,
and performs no file I/O, no network access, and no wall-clock read
(`datetime.now()`/`time.time()`) anywhere in `generate_signal()`'s own
contract — see StrategyContext below for how a Strategy is instead handed
"the current moment" from the outside, exactly the way research/replay.py
hands a backtested contract its data through an AsOfView rather than
letting it query the database directly.

What a Strategy IS and IS NOT
------------------------------
A Strategy turns (context, universe, its own locked parameters) into a list
of Signal objects — nothing more. It explicitly does NOT own any of:

    position sizing            capital allocation         portfolio weights
    stop/risk approval         broker calls                order placement
    execution                  guardrails                  live state
    journal writes

Those are all downstream responsibilities of whatever adapter eventually
consumes a Strategy's Signals — engine.guardrails/engine.execute today, and
whatever a research backtest adapter looks like once it exists. A Signal
(see below) carries no order quantity, no portfolio allocation, no broker
instruction, and no approval state, structurally: those fields simply do
not exist on the dataclass.

StrategyVersion and why parameters alone are not enough
----------------------------------------------------------
A StrategyVersion is the reproducibility object: it must let anyone, later,
answer "exactly what behavior produced this Signal?" Two temptations are
both wrong:

    hash(parameters) alone           the implementation can change while
                                      parameters stay identical, silently
                                      mutating what a locked-looking
                                      "version" actually does
    a bare incrementing version int  carries no information about WHAT
                                      changed or whether two versions are
                                      actually identical in substance

So the version identity is derived from three orthogonal things — algorithm
identity, parameters, and the actual source bytes of the implementation —
exactly research/contracts.py's own content_hash() pattern (canonical JSON,
sha256, truncated hex) applied to a different, non-Contract question:

    version_id = sha256(canonical_json({
        "algorithm_id": algorithm_id,
        "parameters": <canonical parameters JSON>,
        "implementation_source_hash": <sha256 of the implementation's own source>,
    }))[:16]

Changing the parameters, or changing the implementation's source bytes (a
literal string change, a whitespace change, anything — source hashing does
not attempt semantic diffing), produces a different version_id. Two
StrategyVersion objects built from identical algorithm_id/parameters/source
always produce the identical version_id — this is a pure function of its
three inputs, exactly like Contract.content_hash() is a pure function of
its hashed fields.
"""

from __future__ import annotations

import inspect
import hashlib
import json
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Optional, Protocol, Union, runtime_checkable

# ---------------------------------------------------------------------------
# Signal — what a Strategy returns, and nothing else.
# ---------------------------------------------------------------------------

VALID_ACTIONS = ("BUY", "SELL")
"""Reuses engine/execute.py's own existing BUY/SELL vocabulary (its --side
argument) rather than inventing a new action enum. This package never
reaches into that module to get it, though — the string vocabulary is
copied here, deliberately, rather than importing it, since a dependency on
that module at all would violate this package's whole reason for
existing."""


class SignalViolation(ValueError):
    """A Signal was asked to hold something outside its contract — an
    unsupported `action` value, chiefly. Raised at construction time
    (__post_init__), so an invalid Signal can never exist even transiently."""


@dataclass(frozen=True)
class Signal:
    """One Strategy's opinion about one symbol at one point in time. Frozen
    (immutable once constructed) and deliberately minimal: no order
    quantity, no portfolio allocation/weight, no broker instruction, no
    approval state — those fields simply are not declared here, so there is
    nothing to accidentally populate or read.

    `reasons` is a tuple, not a list, so the object is immutable all the way
    down — appending to a `list` field on an otherwise-frozen dataclass
    would silently succeed and violate the "immutable once constructed"
    property this object exists to guarantee.
    """

    strategy_id: str
    strategy_version_id: str
    symbol: str
    action: str                              # "BUY" | "SELL" — see VALID_ACTIONS
    generated_at: Any                         # supplied by StrategyContext.as_of — never wall-clock
    strength: Optional[float] = None          # optional score/confidence; no unit is assumed
    reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.action not in VALID_ACTIONS:
            raise SignalViolation(
                f"action {self.action!r} is not one of {VALID_ACTIONS}")
        if not self.strategy_id or not isinstance(self.strategy_id, str):
            raise SignalViolation("strategy_id must be a non-empty string")
        if not self.strategy_version_id or not isinstance(self.strategy_version_id, str):
            raise SignalViolation("strategy_version_id must be a non-empty string")
        if not self.symbol or not isinstance(self.symbol, str):
            raise SignalViolation("symbol must be a non-empty string")
        if self.generated_at is None:
            raise SignalViolation(
                "generated_at is required and must come from the supplied "
                "StrategyContext — a Signal with no timestamp cannot be "
                "reproduced or ordered against any other Signal")
        # Immutable all the way down: reject a mutable `reasons` container at
        # construction rather than silently accepting one a caller could
        # still mutate out from under the Signal afterward.
        if not isinstance(self.reasons, tuple):
            object.__setattr__(self, "reasons", tuple(self.reasons))

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# StrategyContext — the smallest interface a Strategy needs from whatever
# runtime hands it data. Defined here as a Protocol (structural typing) so
# neither this package nor a Strategy subclass needs to import a concrete
# implementation to type against it — a future research-side adapter over
# research.replay.Replay/AsOfView, and a future engine-side adapter over
# engine.market_data, would each satisfy this Protocol on their own side of
# the boundary, without strategies/ ever importing either module to define
# what "satisfies" means.
# ---------------------------------------------------------------------------

@runtime_checkable
class StrategyContext(Protocol):
    """The entire data-access surface a Strategy is allowed to see.

    `as_of` supplies "now" for this call — the ONLY source of a timestamp a
    Strategy may use for a Signal's `generated_at`. A Strategy implementation
    must never call datetime.now()/time.time() (or any network/wall-clock
    equivalent) itself; that is what makes the exact same context, replayed
    twice, produce byte-identical Signals, in a backtest or in production.

    `history(symbol, days)` is deliberately the only data-access method:
    named after research.store.AsOfView.history(symbol, days, ...) and
    engine.market_data.get_history(symbol, days, ...), which already agree
    on this shape, but this Protocol does not import or depend on either —
    the concrete return type (a DataFrame, a list of bars, whatever a given
    adapter chooses) is intentionally unconstrained here, since strategies/
    has no dependency on pandas, research.store, or engine.market_data to
    interpret it with. A Strategy only ever calls this method for data; it
    never opens a file, a socket, a database connection, or an environment
    variable itself.
    """

    @property
    def as_of(self) -> Any:
        ...

    def history(self, symbol: str, days: int) -> Any:
        ...


# ---------------------------------------------------------------------------
# Hashing helpers — the version-identity mechanism, factored out so both
# StrategyVersion construction and later verification use the exact same
# computation (no second, potentially-drifting implementation of "what does
# this version_id mean").
# ---------------------------------------------------------------------------

def hash_source(source: Union[str, bytes]) -> str:
    """sha256 of the given source bytes/text, truncated to 16 hex chars —
    the same truncation research.contracts.Contract.content_hash() already
    uses. Deliberately a hash of the literal source bytes, not an AST or a
    semantic diff: any change at all, including a comment or whitespace
    change, is treated as "the implementation changed," which is the
    conservative, fail-safe direction for a reproducibility guarantee."""
    if isinstance(source, str):
        source = source.encode("utf-8")
    return hashlib.sha256(source).hexdigest()[:16]


def source_of(implementation: Union[type, Any]) -> str:
    """Best-effort source text for a Strategy class (or any other
    class/function/module) via inspect.getsource() — used when the caller
    passes a live class/callable rather than a literal source string.
    Raises OSError/TypeError (inspect's own exceptions) unchanged if the
    source genuinely cannot be retrieved (e.g. a class defined in a REPL) —
    fail closed rather than silently hashing an empty string."""
    return inspect.getsource(implementation)


def _canonical_parameters(parameters: dict) -> str:
    """Canonical JSON form of `parameters` — sorted keys, compact
    separators, exactly research.contracts.Contract.canonical()'s own
    convention. No `default=str` fallback: parameters must already be pure
    JSON-serializable primitives (the slice's own explicit requirement), so
    a non-serializable value fails loudly here rather than being silently
    coerced and losing round-trip fidelity."""
    if not isinstance(parameters, dict):
        raise TypeError(f"parameters must be a dict, got {type(parameters).__name__}")
    try:
        return json.dumps(parameters, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as e:
        raise ValueError(f"parameters must be JSON-serializable: {e}") from e


def compute_version_id(
    *, algorithm_id: str, parameters_json: str, implementation_source_hash: str,
) -> str:
    """The version-identity function itself — a pure function of exactly
    the three things the module docstring says must all be accounted for.
    Used both when a StrategyVersion is first created and again by
    StrategyVersion.verify() to detect any drift after a round trip through
    the registry."""
    canonical = json.dumps(
        {
            "algorithm_id": algorithm_id,
            "parameters": parameters_json,
            "implementation_source_hash": implementation_source_hash,
        },
        sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# StrategyVersion — the reproducibility object.
# ---------------------------------------------------------------------------

class StrategyVersionViolation(RuntimeError):
    """Raised when a StrategyVersion fails to verify — either internal
    self-consistency (its own version_id no longer matches its own stored
    components) or against a freshly-hashed current implementation. Fail
    closed: this is never repaired or silently re-hashed, the same posture
    research.contracts.Contract.verify() already takes."""


@dataclass(frozen=True)
class StrategyVersion:
    """Immutable once created. `parameters` is exposed only as a read-only
    property that decodes a fresh dict from the stored canonical JSON on
    every access — mutating the dict a caller gets back can never change
    the stored object or its version_id, which is the actual immutability
    guarantee (a frozen dataclass alone would not stop `version.parameters
    ["x"] = 1` if the field held a plain, shared dict).

    Fields intentionally kept to the slice's stated minimum: no lifecycle
    status, no created_at, no promotion state — see the module docstring
    and the completion report for why those are explicitly out of scope
    here (DEFINED/REGISTERED, tracked structurally by "does this file exist
    in the registry", not by a status field).
    """

    strategy_id: str
    algorithm_id: str
    parameters_json: str
    implementation_source_hash: str
    version_id: str
    derived_from_hypothesis_id: Optional[str] = None

    @property
    def parameters(self) -> dict:
        return json.loads(self.parameters_json)

    def verify(self) -> None:
        """Recompute version_id from this object's own stored
        (algorithm_id, parameters_json, implementation_source_hash) and
        raise if it disagrees. This detects corruption or hand-editing of a
        persisted registry file — it does NOT check whether the current,
        live implementation still matches (see verify_implementation() for
        that); it only checks that this object is telling a consistent
        story about itself."""
        expected = compute_version_id(
            algorithm_id=self.algorithm_id,
            parameters_json=self.parameters_json,
            implementation_source_hash=self.implementation_source_hash,
        )
        if expected != self.version_id:
            raise StrategyVersionViolation(
                f"{self.strategy_id}'s StrategyVersion {self.version_id} is "
                f"internally inconsistent: recomputing from its own stored "
                f"algorithm_id/parameters/implementation_source_hash yields "
                f"{expected}, not {self.version_id}. This indicates the "
                f"registry file was hand-edited or corrupted after it was "
                f"created — refusing to silently repair it.")

    def verify_implementation(self, implementation: Union[str, bytes, type]) -> None:
        """Recompute the source hash from a FRESH read of `implementation`
        (a live class/callable, or literal source text/bytes) and raise if
        it no longer matches this version's own implementation_source_hash.
        This is what detects "the algorithm's code changed underneath an
        already-registered StrategyVersion" — the whole reason source
        hashing exists rather than hashing parameters alone."""
        source = source_of(implementation) if isinstance(implementation, type) else implementation
        current_hash = hash_source(source)
        if current_hash != self.implementation_source_hash:
            raise StrategyVersionViolation(
                f"{self.strategy_id}'s StrategyVersion {self.version_id} was "
                f"created from implementation_source_hash="
                f"{self.implementation_source_hash}, but the implementation "
                f"given now hashes to {current_hash} — the algorithm's "
                f"source has changed since this version was registered. "
                f"Register a NEW StrategyVersion; this one must not be "
                f"treated as still describing the current implementation.")

    def to_dict(self) -> dict:
        return asdict(self)


def create_strategy_version(
    *,
    strategy_id: str,
    algorithm_id: str,
    parameters: dict,
    implementation: Union[str, bytes, type],
    derived_from_hypothesis_id: Optional[str] = None,
) -> StrategyVersion:
    """The one way a StrategyVersion is ever built — so version_id is always
    computed by compute_version_id(), never assembled or guessed by a
    caller. `implementation` is either a literal source string/bytes (the
    convention tests use, per the slice's own instruction to prefer a
    deterministic fixture over a real production Strategy file) or a live
    class/callable, hashed via inspect.getsource().

    Deliberately a free function rather than a StrategyVersion classmethod:
    it needs no access to any StrategyVersion internals beyond what
    compute_version_id() already exposes, and keeping it free-standing
    makes "this is the only construction path" easier to see and to test.
    """
    if not strategy_id or not isinstance(strategy_id, str):
        raise ValueError("strategy_id must be a non-empty string")
    if not algorithm_id or not isinstance(algorithm_id, str):
        raise ValueError("algorithm_id must be a non-empty string")

    source = source_of(implementation) if isinstance(implementation, type) else implementation
    implementation_source_hash = hash_source(source)
    parameters_json = _canonical_parameters(parameters)
    version_id = compute_version_id(
        algorithm_id=algorithm_id,
        parameters_json=parameters_json,
        implementation_source_hash=implementation_source_hash,
    )
    return StrategyVersion(
        strategy_id=strategy_id,
        algorithm_id=algorithm_id,
        parameters_json=parameters_json,
        implementation_source_hash=implementation_source_hash,
        version_id=version_id,
        derived_from_hypothesis_id=derived_from_hypothesis_id,
    )


# ---------------------------------------------------------------------------
# Strategy — the abstract base. No concrete strategy is defined anywhere in
# this package on purpose (see the module docstring and the slice's own
# explicit non-goal) — only the shape every future one must satisfy.
# ---------------------------------------------------------------------------

class Strategy(ABC):
    """Turns (context, universe) into a list[Signal] using this instance's
    own locked `version`. Holds no capital, no broker handle, no risk
    parameters — only its own StrategyVersion identity and whatever
    read-only lookback/data-requirement metadata it declares for a future
    caller to plan around.

    `required_lookback`/`required_data` are declarative metadata only —
    this base class does not enforce or use them; a future adapter (e.g. a
    backtest runner deciding how much history to make available) is free to
    read them before calling generate_signal(), but generate_signal() itself
    is always free to call context.history(symbol, days) for whatever it
    actually needs, independent of what it declared.
    """

    def __init__(
        self,
        version: StrategyVersion,
        *,
        required_lookback: int = 0,
        required_data: Iterable[str] = (),
    ) -> None:
        self.version = version
        self.strategy_id = version.strategy_id
        self.required_lookback = required_lookback
        self.required_data: tuple[str, ...] = tuple(required_data)

    @abstractmethod
    def generate_signal(self, context: StrategyContext, universe: list[str]) -> list[Signal]:
        """Return zero or more Signals for symbols in `universe`, using only
        `context` for data and `self.version.parameters` for configuration.
        Must be deterministic given (context, universe, self.version): no
        datetime.now()/time.time(), no random/np.random, no network call, no
        file open, no environment variable read. `context.as_of` is the only
        permitted source of a Signal's `generated_at`."""
        raise NotImplementedError

    def _make_signal(
        self,
        *,
        symbol: str,
        action: str,
        generated_at: Any,
        strength: Optional[float] = None,
        reasons: Iterable[str] = (),
    ) -> Signal:
        """Convenience for subclasses: stamps strategy_id/strategy_version_id
        consistently so a Strategy implementation never has to (mis)construct
        those two fields by hand. Not required — a subclass may build a
        Signal directly — but reduces the chance of a copy-paste mismatch
        between `self.strategy_id` and `self.version.version_id`."""
        return Signal(
            strategy_id=self.strategy_id,
            strategy_version_id=self.version.version_id,
            symbol=symbol,
            action=action,
            generated_at=generated_at,
            strength=strength,
            reasons=tuple(reasons),
        )
