# Living Quant — research layer deployment

The research layer is additive. Nothing in `engine/`, `memory/` or `run_cycle.sh`
changes, and `tests/test_kernel_isolation.py` fails the build if that ever stops
being true.

```
research/          ← new, self-contained
  store.py         bitemporal append-only store
  contracts.py     experiment contracts + the two firewalls
  recorder.py      STEP 2 — the clock-bound feeds
  backfill.py      STEP 3 — the unhurried ones
  probe/probe.py   STEP 0 — the gate
  sources/         one module per feed
```

---

## 0. Install

No new dependencies. The store is stdlib `sqlite3`; the sources use
`urllib`. `pandas` is only needed for `AsOfView.history()` and is already a
project dependency.

```bash
cd /root/trading-agent
venv/bin/python -m tests.test_research_store
venv/bin/python -m tests.test_research_sources
venv/bin/python -m tests.test_kernel_isolation
```

All three must report **0 failed**, and so must the two existing suites —
`test_guardrails` (62) and `test_indicators` (41). If the existing two changed,
something in `engine/` moved and should not have.

---

## 1. STEP 0 — run the probe first

```bash
venv/bin/python research/probe/probe.py            # ~4 minutes
venv/bin/python research/probe/probe.py --json > /tmp/probe.json
```

It prints a count table and a verdict. **The only line that matters:**

```
"projected_events_500_names_7_years": N
"buildable": true | false
```

- `N >= 10,000` → EXP-B1 is buildable as specified.
- `4,000 <= N < 10,000` → usable, but widen the universe or period and restate
  the independence assumption **before locking**.
- `N < 4,000` → the contract must be redesigned. Revising after locking is
  precisely what the hash exists to prevent.

The probe must be run from the VPS. It reaches `api.bseindia.com` and
`nsearchives.nseindia.com`, which are blocked from some networks.

---

## 2. STEP 3 — start the backfill immediately

Do not wait for the recorder. These are independent and the backfill is the
long pole.

```bash
# Seconds. The single highest-value import in the project.
venv/bin/python -m research.backfill membership

# Hours. Safe to interrupt and resume — the store dedupes on re-run.
venv/bin/python -m research.backfill prices --from 2019-01-01 --to 2026-08-31

venv/bin/python -m research.backfill status
```

Run the price backfill under `nohup` or `tmux`; an SSH drop mid-run is harmless
but wastes the elapsed time.

Announcements are held back until the probe reports. `backfill announcements`
refuses to run without a `--scrip-map`, on purpose.

---

## 3. STEP 2 — the recorder

### Its own cron. Never inside `run_cycle.sh`.

```cron
# ---- Living Quant recorder (research only — places no orders, reads no broker)
*/30 9-16 * * 1-5  cd /root/trading-agent && venv/bin/python -m research.recorder --cycle news       --quiet-on-success >> logs/recorder.log 2>&1
15   10   * * 1-5  cd /root/trading-agent && venv/bin/python -m research.recorder --cycle intraday   --quiet-on-success >> logs/recorder.log 2>&1
30   12   * * 1-5  cd /root/trading-agent && venv/bin/python -m research.recorder --cycle intraday   --quiet-on-success >> logs/recorder.log 2>&1
30   14   * * 1-5  cd /root/trading-agent && venv/bin/python -m research.recorder --cycle intraday   --quiet-on-success >> logs/recorder.log 2>&1
45   15   * * 1-5  cd /root/trading-agent && venv/bin/python -m research.recorder --cycle intraday   --quiet-on-success >> logs/recorder.log 2>&1
30   18   * * 1-5  cd /root/trading-agent && venv/bin/python -m research.recorder --cycle evening    --quiet-on-success >> logs/recorder.log 2>&1
```

Separate entries, separate log. A recorder crash cannot abort a trading cycle
and a trading failure cannot skip a recording.

### What each cycle collects

| Cycle | Feeds | Recoverable later? |
|---|---|---|
| `news` | RSS arrival timestamps | **No** |
| `intraday` | option chain, quotes/breadth, news | **No** |
| `evening` | today's bhavcopy, agent decisions, news, closing chain | prices yes, rest no |

### Verify it before trusting it

```bash
venv/bin/python -m research.recorder --cycle intraday      # run once by hand
venv/bin/python -m research.backfill status
tail -5 research/recorder_runs.jsonl
```

`recorder_runs.jsonl` is what later distinguishes *"the market was quiet"* from
*"the recorder was down"*. Without it, a missing Tuesday is ambiguous forever.

### The failure the recorder is designed around

A source that reports success while recording nothing is worse than one that
crashes: the log says green, the perishable data is gone, and nobody checks
again for months. So a source where **every** target failed raises rather than
returning an empty success, and a cycle where **every** source failed exits 1
and sends a Telegram alert. One flaky feed is a warning; total silence is a page.

---

## 3a. The overnight Research AI cadence

### Cron RETIRED — superseded by the continuous research worker

The standalone `0 1 * * * research.overnight` cron line is **no longer
scheduled**. The continuous research worker's deeper nightly batch
(`30 23 * * *` in `deploy/research.cron`, `docs/RESEARCH_WORKER.md`) does
everything it did — discovery through the **same** `build_digest` →
`investigator.investigate` → `research.overnight._existing_duplicate` path,
same default cap of 3 attempts — **plus** it runs the locked experiments the
overnight job never touched. Scheduling both just generated the same DRAFTs
twice a night.

`research/overnight.py` the module is unchanged and stays in the tree: the
worker imports its `_existing_duplicate` helper, and it is still directly
runnable by hand:

```cron
# (do NOT re-add this line — the 23:30 worker batch in deploy/research.cron covers it)
# 0    1    * * *    cd /root/trading-agent && venv/bin/python -m research.overnight --quiet-on-success >> logs/overnight_research.log 2>&1
```

The worker's nightly batch at 23:30 IST sits after the last recorder entry
(18:30 IST evening cycle above) and well before the next day's premarket live
cycle (08:30 IST, see `docs/ENGINE_DEPLOY.md`), so the digest it reads is
built from a fully recorded day. It has its own cron line and its own log
(`research/worker_runs.jsonl` + `logs/research_worker.log`) — the same "own
cron, own log" isolation the recorder uses.

### What it does, and what it structurally cannot do

One run: builds one deterministic digest from the research Store, makes a
small bounded number of Research AI proposal attempts against it (default 3,
`--max-attempts` to override), and for each attempt that produces a valid,
non-duplicate, validation-passing proposal, creates a hypothesis **DRAFT**.
Nothing more.

A successful overnight run ends at "Hypothesis → DRAFT." The process never
calls `approve_and_lock()` or `Contract.lock()`, never calls
`run_experiment()`, never touches `engine/*` or `memory/state.json`, and
never reads broker credentials — it inherits the same Research AI permission
profile (`.claude/settings.json`) already locked down for the daytime
Investigator. A DRAFT is a proposal for testing, not a validated result; the
Telegram summary it sends says so explicitly and never states a confidence
level, an implied edge, or a trading instruction.

### Verify it before trusting it

```bash
venv/bin/python -m research.overnight --db research/market_memory.db   # run once by hand
tail -5 research/overnight_runs.jsonl
```

`overnight_runs.jsonl` records, per run: start/end time, the digest `as_of`,
how many attempts were made, the outcome of each (`draft_created` /
`no_proposal` / `duplicate` / `rejected` / `ai_error`), and which
`hypothesis_id`s were created — the same "quiet night vs. broken night"
distinction the recorder's own run log gives the recorder.

---

## 4. Candidate-set capture — no engine change at all

The screener's candidate set is captured by **reading the artifact the trading
system already writes**, not by importing research from engine.

`engine/briefing.py` renders every run to `logs/briefing-{cycle}-{stamp}.md`,
complete with the regime call and every candidate's indicators. `run_cycle.sh`
deletes those after 60 days. `research/sources/briefing_artifacts.py` parses them
on the evening cycle, well inside that window.

```text
briefing.py ──writes──> logs/briefing-*.md ──read by──> research recorder
```

Data handoff, not code dependency. The invariant stays absolute:

```text
engine  ──X──>  research
research ────>  engine
```

Nothing in `engine/` changes and nothing in `engine/` learns that research
exists. `tests/test_kernel_isolation.py` still fails the build if that inverts.

**The honest cost.** Parsing a rendered document is more fragile than reading a
structured record. So it does not fail silently: every run reports
`candidate_blocks_seen` against `candidate_blocks_parsed`, and below 90%
coverage the result carries a `WARNING`. A format change shows up as a number
that fell, not as a dataset that quietly stops growing while the log says green
— the same failure class the option-chain smoke test caught, given the same
treatment.

If that fragility ever bites, the fix keeps the invariant: have `briefing.py`
append a JSONL line through **engine's own** `journal.record()`, and point this
module at that file instead. Still a data handoff, just a more stable format.

## 5. Permissions

Add to the `deny` list in `.claude/settings.json` (yours to make, not the
agent's):

```json
"Edit(research/registry/*)",
"Edit(research/market_memory.db)",
"Edit(research/schema.sql)",
"Edit(research/store.py)",
"Edit(research/contracts.py)"
```

The agent may read research output and reason about it. It must not be able to
rewrite a locked hypothesis, alter the store, or weaken the firewalls — for the
same reason it cannot edit `guardrails.py`.

---

## 6. Disk

Rough, at the schedules above:

| Feed | Per year |
|---|---|
| Option chain (3 indices × 5 snapshots/day) | ~250 MB |
| Intraday quotes (Nifty 500 × 4 snapshots/day) | ~400 MB |
| News arrivals | ~30 MB |
| EOD prices (2,000 symbols) | ~60 MB |
| Agent decisions | ~5 MB |

Under 1 GB/year, plus roughly 400 MB for the 2019-2026 price backfill. Trim the
strike band in `option_chain.py` or drop an intraday quote snapshot if the VPS
disk gets tight — but trim the *resolution*, never the *coverage*. A year with
four snapshots a day is worth far more than six months with eight.

---

## 7. What is deliberately not here yet

- `research/replay.py` and the `as_of` refactor of `market_data` / `regime` /
  `screener` (STEP 4)
- `research/runner.py`, `research/evaluate.py`, `research/experiments/exp_b1.py`
  (STEPS 5-6)
- Corporate actions, results calendar and delivery-percentage sources — all
  backfillable, none urgent, and the announcements probe should report first

The contract machinery in `contracts.py` is finished and tested, so EXP-B1 and
EXP-A can be drafted and locked as soon as the probe returns. The model firewall
already refuses any LLM-featured contract whose evaluation window opens before
its model's knowledge cutoff, which means Experiment A can be registered today
and the system will permanently refuse to run it on 2023.
