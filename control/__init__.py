"""control/ — the global runtime control layer (Priority Phase 4).

See control/runtime.py for the actual state machine. This package exists
so research/, paper/, and api/ can all depend on ONE small, neutral,
dependency-free thing instead of on each other — see that module's own
docstring for exactly why that matters.
"""
