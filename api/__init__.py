"""
api/ — Phase 4 Slice Z: read-only HTTP API over the existing trading/research
system.

This package is an ADAPTER, not a second source of truth. Every endpoint
reads through an already-authoritative interface (engine.guardrails,
engine.journal, research.memory / research.brain.*, strategies.registry) and
serializes what it finds. Nothing here computes a competing statistic,
duplicates business logic that already lives in engine/ or research/, or
writes anything, anywhere.

    api.app       — Flask application factory, routing, CORS, error handling
    api.auth      — bearer-token authentication for protected endpoints
    api.data      — the actual read adapters over engine/research/strategies

See api/app.py's module docstring for the full endpoint list and the
non-negotiable boundaries this package operates under.
"""
