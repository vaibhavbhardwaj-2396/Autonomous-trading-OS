# Continuous Research Worker v2 (Unified Opportunity-Driven Selection)

`research/brain/worker.py` — makes the research loop *continuously* alive
instead of only running at 01:00 (`research/overnight.py`). Companion to
`docs/RESEARCH_DEPLOY.md` (the recorder + overnight cadence) and
**`docs/RESEARCH_CONTROL_PLANE.md`** (the Autonomous Research Control Plane
this worker now invokes every heartbeat — opportunity pool, priority,
autonomous promotion, non-terminal rejection/reassessment, user overrides,
the unified action queue — full design).

Every heartbeat repeatedly refreshes the opportunity pool, ranks EVERY
currently-executable action — running an already-locked experiment,
autonomously promoting a draft, or running the Research AI — on ONE
priority scale, and executes the single highest-priority eligible one, until
the runtime budget is spent or nothing eligible remains:

```
data ingestion (recorder)
      ↓
research state / digest        digest.build_digest                    (once per heartbeat)
      ↓
┌─────────────────────────────────────────────────────────────────┐
│  refresh the opportunity pool   opportunity.build_opportunity_pool │
│  rank every EXECUTABLE action   opportunity.build_action_queue     │
│      RUN_EXPERIMENT  ← scheduler.eligible_contracts               │
│      PROMOTE         ← a promotable draft                         │
│      DISCOVER        ← one synthetic "run the Research AI" action │
│      (all three scored via the SAME priority_score())             │
│  execute the top ELIGIBLE action (per-kind caps still enforced):  │
│      RUN_EXPERIMENT → scheduler.run_one_experiment → runner.run_experiment │
│      PROMOTE         → opportunity.attempt_autonomous_promotion   │
│      DISCOVER        → investigator.investigate  → DRAFT (proposal only) │
│  record the outcome; loop ─── until budget spent / nothing eligible │
└─────────────────────────────────────────────────────────────────┘
      ↓
evidence                       evaluator.record_verdict (inside run_experiment)
      ↓
memory (the research Store)    ← the ONE source of truth
      ↓
next heartbeat sees it in the digest
```

A rejected hypothesis whose evidence just turned in its favour, or an
already-locked robustness test, can now genuinely outrank a brand-new
discovery attempt on any given heartbeat — there is no fixed phase order
privileging one kind of work over another. See
`docs/RESEARCH_CONTROL_PLANE.md` §10 for the full design and a worked
example.

## Cadence model — cron is the heartbeat, the worker is the brain

`python -m research.brain.worker` runs **one bounded slice of work and
exits**. It never loops, sleeps, or daemonises. A cron line invokes it every
few minutes; each invocation the worker decides whether useful work exists
and does at most a configured amount.

```
09:05  worker → highest-priority action is DISCOVER → one discovery attempt,
       creates a draft → next-highest is now PROMOTE (that same draft) →
       promotes it (locks it) → next-highest is now RUN_EXPERIMENT (that
       same contract, now eligible) → runs it → nothing eligible left, exits
09:10  worker → highest-priority action is RUN_EXPERIMENT (a human just
       locked a contract) → runs it, exits
09:15  worker → a rejected hypothesis's evidence just turned (a sibling went
       PROMISING) → its retest contract now outranks a fresh discovery
       attempt → runs the retest, exits
09:30  worker → no eligible action of any kind → sets a cooldown, exits
...    (cooldown: cheap experiment-only checks, no Research AI, for 30 min)
10:00  worker → a human (or the worker itself, an earlier heartbeat) locked a
       contract → runs it
18:30  worker (larger limits) → deeper batch — the SAME selection loop,
       just with higher per-kind caps and a longer runtime budget, so more
       than one action of each kind can run before the heartbeat ends
```

The times are **not hard-coded** — the worker only honours its limits; the
cron decides how big each slot is. Which action actually runs at each
heartbeat is never hard-coded either — see `docs/RESEARCH_CONTROL_PLANE.md`
§10 for exactly how RUN_EXPERIMENT/PROMOTE/DISCOVER are ranked together.

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

## VPS cron — `deploy/research.cron` (NOT installed automatically)

The canonical schedule lives in **`deploy/research.cron`** (version-controlled,
one reviewable source of truth). To deploy: `crontab -e` on the VPS and paste
that block in its own section, well clear of `run_cycle.sh`'s live cycles.

The two worker lines it adds:

```cron
# continuous bounded heartbeat — defaults ARE the required bounds
# (discovery 1, experiments 1, runtime 300s, concurrency 1, cooldown 1800s)
*/10 6-23 * * *   cd /root/trading-agent && venv/bin/python -m research.brain.worker --quiet-on-success >> logs/research_worker.log 2>&1

# deeper batch once nightly, after the recorder's 18:30 evening cycle
30   23   * * *   cd /root/trading-agent && venv/bin/python -m research.brain.worker --max-discovery-attempts 3 --max-experiments 3 --max-runtime-seconds 900 --quiet-on-success >> logs/research_worker.log 2>&1
```

The heartbeat window `6-23` runs **straight through market hours** — research
and trading are separate processes and neither is ever gated on the other.

### The old `research.overnight` 01:00 cron is RETIRED

`deploy/research.cron` deliberately omits the standalone
`0 1 * * * research.overnight` line (`docs/RESEARCH_DEPLOY.md` §3a). The
`30 23` deeper worker batch fully subsumes it: it runs discovery through the
**same** `build_digest` → `investigator.investigate` →
`research.overnight._existing_duplicate` path (the worker imports that duplicate
check verbatim from `research/overnight.py`), with the same default attempt cap
of 3, **plus** it runs experiments — which `research.overnight` never did.
`research/overnight.py` the module stays in the tree (the worker depends on it,
and `python -m research.overnight` still runs by hand); only its cron line is
removed so the same DRAFTs are not generated twice a night.

## Resource limits (config, not scattered constants)

`research.brain.worker.WorkerLimits`, each overridable by an env var (in the
main `.env`, since cron runs as root with it) and then by a CLI flag:

| Limit | Default | Env var | Flag |
|---|---|---|---|
| `max_discovery_attempts` | 1 | `RESEARCH_WORKER_MAX_DISCOVERY_ATTEMPTS` | `--max-discovery-attempts` |
| `max_experiments` | 1 | `RESEARCH_WORKER_MAX_EXPERIMENTS` | `--max-experiments` |
| `max_promotions` | 1 | `RESEARCH_WORKER_MAX_PROMOTIONS` | `--max-promotions` |
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

### The Claude Code executable — `RESEARCH_AI_CLAUDE_BIN`

Discovery shells out to Claude Code (`research.brain.investigator.
_default_runner`). cron's PATH is minimal and does not include
`~/.local/bin` (the same reason `run_cycle.sh` resolves its own `CLAUDE_BIN`
explicitly rather than trusting `command -v claude`) — a bare `"claude"`
resolved via PATH fails under cron with `[Errno 2] No such file or directory:
'claude'` even though it works fine in an interactive shell.

`research.brain.investigator.resolve_claude_binary()` fixes this
deterministically: `RESEARCH_AI_CLAUDE_BIN` if set, else
`DEFAULT_CLAUDE_BIN` = `/root/.local/bin/claude` — this deployment's actual
install path, so **no cron env var is needed**; `deploy/research.cron` needs
no change. The resolved path is validated (absolute, exists, executable)
*before* the subprocess call, raising a clear `InvestigatorError` naming the
broken path and the env var to fix it — never a bare `OSError`/`[Errno 2]`.
Set `RESEARCH_AI_CLAUDE_BIN=/some/other/path` only if this deployment's
Claude Code install path ever changes, or for a non-`root` install.

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
opportunities_considered size of the opportunity pool this run (v1)
promotions_attempted     autonomous draft->locked attempts this run (v1)
promoted_hypothesis_ids  which drafts were actually locked (v1)
promotion_outcomes       [{opportunity_id, outcome, detail}] (v1)
reassessment_eligible_count  pool entries whose evidence just changed (v1)
action_log               [{kind, opportunity_id, contract_id, priority_score,
                          priority_components, compute_cost_estimate, relevance,
                          confidence_before/after, lifecycle_before/after,
                          next_priority, outcome, runtime_consumed_seconds}] —
                          one entry per action actually SELECTED and executed
                          this heartbeat, in order (v2) — see
                          docs/RESEARCH_CONTROL_PLANE.md §10
```

## Status — `--status` (read-only)

```bash
venv/bin/python -m research.brain.worker --status        # human summary
venv/bin/python -m research.brain.worker --status --json  # machine-readable
```

Reads `research/worker_runs.jsonl` + `research/.worker_state.json` and prints:
last heartbeat time and runtime, what the last cycle attempted
(`work_selected`), experiments run, drafts created, `no_work_reason`, runtime
budget remaining, whether discovery is in cooldown (and until when), recent
errors, and the last N cycle labels. Opens no Store, takes no lock, does no
work — safe to run any time, including while a heartbeat is in flight.

## Telegram — bounded

- a **new DRAFT proposal** → one message (says "proposals only, not
  validated, not locked, not tradeable")
- an **error** this cycle → one message
- otherwise **silent**, except **one periodic roll-up** at most every
  `summary_interval_seconds` (default 12h). The first heartbeat after a
  (re)deploy only seeds that clock — no deploy burst.

A routine "ran one experiment, nothing notable" heartbeat sends **nothing**.

## Safety — what the worker cannot do (true by absence of the import)

- **No live, ever.** `engine.execute`, `engine.guardrails`, `engine.broker*`
  are never imported anywhere in the worker or in
  `research.brain.opportunity` — no order, no broker call, no
  `memory/state.json` write is reachable.
- **Locking is possible now (v1), but through exactly ONE narrow, audited
  path.** `worker.py` itself never imports or calls
  `hypothesis_intake.approve_and_lock`/`Contract.lock` directly — the only
  call site in the whole control plane is inside
  `research.brain.opportunity.attempt_autonomous_promotion()`, using the
  SAME unmodified locking function every human approval already used, an
  explicit named system approver (never blank, never a forged human name),
  and the SAME unmodified research-budget rate limit. See
  `docs/RESEARCH_CONTROL_PLANE.md` for the full design and safety
  argument — this is the one behavioural change from v0.
- **No second source of truth.** Only durable writes:
  `research/worker_runs.jsonl` (telemetry), `research/.worker_state.json`
  (cooldown/summary bookmark), and — new in v1 — one
  `research_opportunity_event` row per lifecycle transition / promotion /
  user override, itself just another dataset value in the SAME append-only
  `observations` table every other research-memory dataset already lives
  in. The research Store stays the sole authority.
- **No direct experiment execution either (v2).** `worker.py` never
  imports `research.experiments.runner` — running one experiment goes
  exclusively through `research.brain.scheduler.run_one_experiment()` (new
  in v2, alongside the existing batch `run_scheduler()`, both delegating to
  the same unmodified runner primitive), the same way locking goes
  exclusively through `opportunity.attempt_autonomous_promotion()`. The
  unified action loop (`docs/RESEARCH_CONTROL_PLANE.md` §10) changed ONLY
  the ORDER work is selected in — every actual execution path is unchanged.
- Enforced by `tests/test_research_worker.py` sections J and R,
  `tests/test_research_worker_selection.py` section M, plus
  `tests/test_research_opportunity.py` section K, and the existing
  `tests/test_kernel_isolation.py`, `tests/test_research_engine_boundary.py`,
  and `tests/test_broker_probe.py`.
