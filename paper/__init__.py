"""
paper/ — Slice AA: the paper/shadow execution engine.

    StrategyVersion (strategies/, unchanged)
          |
          v
    paper eligibility            <- paper/eligibility.py
          |
          v
    signal generation            <- strategies.core.Strategy, via paper/context.py
          |
          v
    paper fill simulation        <- paper/fills.py
          |
          v
    paper portfolio accounting   <- paper/portfolio.py
          |
          v
    paper state (SQLite)         <- paper/store.py, paper/paper_shadow.db
          |
          v
    dashboard/API + Telegram     <- api/app.py's /paper/* routes, paper/notify.py

THE SAFETY BOUNDARY (read this before touching anything in this package)
--------------------------------------------------------------------------
This package NEVER calls a broker, NEVER calls engine.execute, and NEVER
calls engine.guardrails for the purpose of placing or approving a live
order. It has its own, completely separate state (paper_shadow.db, never
memory/state.json), its own risk limits (paper/config.py, never
engine/guardrails.py's limits), and its own journal (paper_orders /
paper_trades tables, never memory/trades.jsonl).

The dependency direction is, and must remain:

    strategies/  --->  paper/  --->  paper's own SQLite store

NEVER:

    strategies/  --->  paper/  --->  engine.execute / engine.broker*

paper/ is allowed to IMPORT a small, deliberately narrow set of read-only /
pure-function / static-data pieces of engine/ — engine.market_data.
get_history (a data read, no broker call, the same function
engine.screener/engine.regime already use for indicators), engine.costs (a
pure arithmetic function, no I/O, no live state), and engine.watchlist (a
static, hardcoded list of symbols, no I/O at all) — and nothing else. It
never imports engine.execute, engine.guardrails, engine.journal,
engine.broker, engine.broker_kite, or engine.broker_indstocks.
tests/test_paper_isolation.py enforces this the same way
tests/test_kernel_isolation.py already enforces the engine/research/
boundary.

Paper results are a research signal, never a live-trading certification.
Nothing in this package writes to, reads for the purpose of promotion into,
or otherwise feeds a live execution decision. See docs/PAPER_TRADING.md.
"""
