# Continuous Research Worker v0

`research/brain/worker.py` — makes the research loop *continuously* alive
instead of only running at 01:00 (`research/overnight.py`). Companion to
`docs/RESEARCH_DEPLOY.md` (the recorder + overnight cadence).

```
data ingestion (recorder)
      ↓
research state / digest        digest.build_digest
      ↓
bounded discovery work         investigator.investigate         → DRAFT (proposal only)
      ↓
priority selection             priority.rank_experiments (via scheduler)
      ↓
bounded experiments            scheduler.run_scheduler → runner.run_experiment
      ↓
evidence                       evaluator.record_verdict (inside run_experiment)
      ↓
memory (the research Store)    ← the ONE source of truth
      ↓
next heartbeat sees it in the digest
```

## Cadence model — cron is the heartbeat, the worker is the brain

`python -m research.brain.worker` runs **one bounded slice of work and
exits**. It never loops, sleeps, or daemonises. A cron line invokes it every
few minutes; each invocation the worker decides whether useful work exists
and does at most a configured amount.

```
09:05  worker → digest → one discovery attempt
09:10  worker → a pending experiment
09:15  worker → another discovery attempt
09:30  worker → no useful work → sets a cooldown, exits
...    (cooldown: cheap experiment-only checks, no Research AI, for 30 min)
10:00  worker → a human locked a contract overnight → runs it
18:30  worker (larger limits) → deeper batch
```

The times are **not hard-coded** — the worker only honours its limits; the
cron decides how big each slot is.

## The worker command

```bash
# one heartbeat (experiments + up to 1 discovery attempt), then exit
cd /root/trading-agent && venv/bin/python -m research.brain.worker --quiet-on-success

# experiments only, no Research AI this run
venv/bin/python -m research.brain.worker --no-discovery

# a larger overnight slice
venv/bin/python -m research.brain.worker --max-discovery-attempts 3 --max-experiments 3 \
    --max-runtime-seconds 900

# inspect a run without side effects
venv/bin/python -m research.brain.worker --json --no-notify
```

Exit code: `0` on success (including a clean "another worker is already
running" no-op and a legitimate "no work" heartbeat), `1` if a component
errored this cycle (the error is also in the telemetry row and, if notable,
on Telegram).

## Recommended VPS cron (NOT installed automatically)

`crontab -e`, in its own block, well clear of `run_cycle.sh`'s live cycles:

```cron
# ---- Continuous Research Worker v0 (research only — proposal-only, never
# ---- locks a hypothesis, never trades, never touches a broker or live state).
# ---- Safe during market hours: research/live separation is at the import
# ---- boundary. Overlapping ticks are made safe by research/.worker.lock.
*/10 6-23 * * *   cd /root/trading-agent && venv/bin/python -m research.brain.worker --quiet-on-success >> logs/research_worker.log 2>&1

# ---- one deeper batch late evening, after the recorder's 18:30 evening cycle
30   23   * * *   cd /root/trading-agent && venv/bin/python -m research.brain.worker --max-discovery-attempts 3 --max-experiments 3 --max-runtime-seconds 900 --quiet-on-success >> logs/research_worker.log 2>&1
```

If you adopt this, you can **retire the standalone
`research.overnight` 01:00 line** (`docs/RESEARCH_DEPLOY.md` §3a) — the worker
covers discovery and adds experiments. `research/overnight.py` is kept in the
tree either way (still directly invocable); this slice does not remove it.
Both create DRAFTs through the *same* `investigator.investigate` +
duplicate-detection path, so running both is safe (the research Store's own
idempotency + the shared fingerprint check dedupe), just redundant.

## Resource limits (config, not scattered constants)

`research.brain.worker.WorkerLimits`, each overridable by an env var (in the
main `.env`, since cron runs as root with it) and then by a CLI flag:

| Limit | Default | Env var | Flag |
|---|---|---|---|
| `max_discovery_attempts` | 1 | `RESEARCH_WORKER_MAX_DISCOVERY_ATTEMPTS` | `--max-discovery-attempts` |
| `max_experiments` | 1 | `RESEARCH_WORKER_MAX_EXPERIMENTS` | `--max-experiments` |
| `max_runtime_seconds` | 300 | `RESEARCH_WORKER_MAX_RUNTIME_SECONDS` | `--max-runtime-seconds` |
| `max_concurrent_experiments` | 1 | `RESEARCH_WORKER_MAX_CONCURRENT_EXPERIMENTS` | — (v0 rejects any other value) |
| `cooldown_seconds` | 1800 | `RESEARCH_WORKER_COOLDOWN_SECONDS` | `--cooldown-seconds` |
| `discovery_enabled` | true | `RESEARCH_WORKER_DISCOVERY_ENABLED` | `--no-discovery` |
| `summary_interval_seconds` | 43200 (12h) | `RESEARCH_WORKER_SUMMARY_INTERVAL_SECONDS` | — |

`max_runtime_seconds` bounds **new work started** — one discovery attempt
already in flight can run to the Research AI's own 300s timeout, so the true
ceiling of a single heartbeat is ~`max_runtime_seconds + 300s`. Overlapping
cron ticks in that window exit immediately (lock held).

**Start conservatively** (the defaults): one process, one experiment at a
time, one discovery attempt, no unbounded historical scan, no parallel
`claude` invocations. Widen only after watching `logs/research_worker.log`
and VPS load.

## Concurrency / idempotency

- **`research/.worker.lock`** — a POSIX `flock`. A second concurrent
  invocation exits at once with `0` and does nothing. The kernel releases
  the lock on process exit or crash (nothing to unstick).
- **Experiments** — `scheduler`/`runner` only touch `status == "locked"`
  contracts and transition them; a `REPORTED` contract is never re-run.
- **Discovery** — `investigator.investigate`'s duplicate check
  (`research.overnight._existing_duplicate`, the shared fingerprint) stops a
  repeat proposal before a draft is created.
- **Cooldown** — after a heartbeat that found no useful work, discovery is
  suppressed for `cooldown_seconds` (experiments are still checked — a human
  locking a contract is new work). Tracked in `research/.worker_state.json`,
  a two-field bookmark, *not* research state.
- Safe during market hours, after hours, run multiple times, from
  overlapping triggers.

## Telemetry — `research/worker_runs.jsonl`

One JSON object per heartbeat (append-only, same idea as
`overnight_runs.jsonl` / `recorder_runs.jsonl`). Fields:

```
started_at, finished_at, runtime_seconds, worker_id, digest_as_of,
work_selected            e.g. ["experiments","discovery"]  ([] = idle)
proposals_created        new DRAFT hypotheses this run
drafts                   their hypothesis_ids
duplicate_rejections     proposals caught as exact duplicates
experiments_run          locked contracts attempted this run
experiment_outcomes      [{contract_id, outcome, detail}]
evidence_updates         distinct hypotheses whose verdict was (re)recorded
no_work_reason           why the heartbeat was idle (null if it did work)
errors                   [str] — a component failing is recorded, never raised out
limits                   the effective WorkerLimits for this run
notified                 whether a Telegram message went out
```

## Telegram — bounded

- a **new DRAFT proposal** → one message (says "proposals only, not
  validated, not locked, not tradeable")
- an **error** this cycle → one message
- otherwise **silent**, except **one periodic roll-up** at most every
  `summary_interval_seconds` (default 12h). The first heartbeat after a
  (re)deploy only seeds that clock — no deploy burst.

A routine "ran one experiment, nothing notable" heartbeat sends **nothing**.

## Safety — what the worker cannot do (true by absence of the import)

- **No lock, no live.** `hypothesis_intake.approve_and_lock` and
  `Contract.lock` are never imported. Discovery ends at DRAFT; a human
  approves before anything is locked. `engine.execute`, `engine.guardrails`,
  `engine.broker*` are never imported — no order, no broker call, no
  `memory/state.json` write is reachable.
- **No second source of truth.** Only durable writes:
  `research/worker_runs.jsonl` (telemetry) and `research/.worker_state.json`
  (cooldown/summary bookmark). The research Store stays authoritative.
- Enforced by `tests/test_research_worker.py` section J, plus the existing
  `tests/test_kernel_isolation.py`, `tests/test_research_engine_boundary.py`,
  and `tests/test_broker_probe.py`.
