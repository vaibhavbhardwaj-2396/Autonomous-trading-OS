"""
strategies/ — the Strategy domain package (Phase 3 Slice X).

A top-level sibling of engine/ and research/, depending on NEITHER. See
strategies/core.py's module docstring for the full architectural rationale.

Deliberately does not re-export its contents here (no `from .core import *`)
— an explicit `from strategies.core import Strategy, Signal, ...` or
`from strategies.registry import save_version, ...` at the call site is
one honest line, and keeps this file from becoming a second place a name's
origin could be ambiguous.
"""
