# Autonomous Research Control Plane v1 + v2 (Unified Selection) + v3 (Action Expansion)

Implements the first three slices of the Living Quant OS Master Vision's
roadmap ("current foundation" → **AUTONOMOUS RESEARCH CONTROL PLANE** →
strategy factory → portfolio engine → capital allocation → execution).
Companion to `docs/RESEARCH_WORKER.md` (the heartbeat/cron this plugs into)
and `docs/RESEARCH_DEPLOY.md`.

**What changed in v1, in one sentence:** the ONE remaining human-gated step
in the research pipeline — DRAFT → LOCKED (`hypothesis_intake.approve_and_lock`'s
mandatory named approver) — can now also be satisfied autonomously, by an
explicit, named, budget-respecting system action, with everything downstream
of a lock (running the experiment, scoring it, aggregating evidence)
unchanged, because it was **already** fully autonomous before this slice.

**What changed in v2 (Unified Opportunity-Driven Worker Selection), in one
sentence:** the worker no longer runs experiments, then discovery, then
promotion as three fixed phases every heartbeat — it ranks RUN_EXPERIMENT /
PROMOTE / DISCOVER actions together, on the exact same priority score, and
repeatedly executes the single highest-priority eligible one until its
runtime budget is spent, so a rejected hypothesis whose evidence just turned
in its favour (or an existing robustness test) can now genuinely outrank a
brand-new discovery attempt — see §10.

**What changed in v3 (Autonomous Research Action Expansion), in one
sentence:** a high-priority opportunity with no runnable experiment and no
pending draft — the common REASSESS_REJECTED case — used to reach the top
of the queue with nothing to execute; the worker can now create the
smallest available research substrate itself (a validation/holdout split
derived from an already-locked contract of the same hypothesis — never a
new rule invented, never a Research AI call), via a fourth action kind,
CREATE_EXPERIMENT, scored on the exact same priority scale — see §14.

```
data ingestion → digest → discovery (AI proposal) → DRAFT
                                                        │
                                                        ▼
                               research.brain.opportunity.build_opportunity_pool()
                                            │
                                            ▼
                    priority-ranked pool (drafts + locked-eligible + rejected-for-reassessment)
                                            │
                                            ▼
                research.brain.opportunity.attempt_autonomous_promotion()   <- NEW
                    (hypothesis_intake.approve_and_lock(), system approver,
                     the EXISTING research budget — unmodified)
                                            │
                                            ▼
                          LOCKED → scheduler.run_scheduler() → runner.run_experiment()
                                            │                        (unchanged, already autonomous)
                                            ▼
                              evaluator.record_verdict() → evidence → next digest
```

## 1. The opportunity pool (`research/brain/opportunity.py`)

**Unit of work:** `Opportunity` — one per hypothesis (`id = "OPP-<hypothesis_id>"`),
not per contract. Fields: `type`, `hypothesis_id`, `contract_id`,
`lifecycle_stage`, `priority_score` + `priority_components` (fully
explainable), `confidence`, `evidence_verdict`, `research_area`,
`is_duplicate`, `reassessment_eligible`, `override_state`
(`{frozen, retired}`), `compute_cost_estimate`, `evidence_signature`,
`portfolio_relevance` (always `None` in v1 — see §7).

`OPPORTUNITY_TYPES` is a 20-member vocabulary (NEW_HYPOTHESIS,
RETEST_HYPOTHESIS, REASSESS_REJECTED, ROBUSTNESS_TEST, OUT_OF_SAMPLE_TEST,
REGIME_TEST, COST_SENSITIVITY, ..., OPTIONS_RESEARCH, STAT_ARB_RESEARCH,
OTHER_RESEARCH). The auto-classifier (`_opportunity_type`) only actively
assigns a handful of these (NEW_HYPOTHESIS, RETEST_HYPOTHESIS,
REASSESS_REJECTED, ROBUSTNESS_TEST) from data already on hand — the rest
exist so a future, narrower caller (a human, the Research AI, or a later
slice) can construct one explicitly, without a schema change. This is
deliberate (Master Vision §2: "do not overbuild every type immediately").

**Nothing here is a new source of truth.** `build_opportunity_pool()` is
read-mostly, composing existing, unmodified components:

| what | reused from |
|---|---|
| which drafts exist | `hypothesis_intake` (via the hypothesis-claim log) |
| which locked contracts are runnable, in what order | `scheduler.eligible_contracts()` (itself `priority.rank_experiments()`-ordered) |
| per-hypothesis evidence/verdict | `digest.build_digest()`'s own `evidence` section, unbounded |
| exact-duplicate detection | `similarity.is_duplicate()` |
| research-area grouping | `research_areas.groups()` / `area_of()` |
| confirmation/robustness signal | `priority.is_confirmation_experiment()` |
| variant direction (pos/neg/flat) | `comparison.classify_variant()` |

## 2. Lifecycle — 9 stages, derived and read-only

```
DISCOVERED → TRIAGED → TESTING → EVALUATING → PROMISING → ROBUST
                                      │
                                      ▼
                                  REJECTED ⇄ REASSESSING
                                      │
                                      ▼
                                   RETIRED   (explicit override only, always reopenable)
```

Every stage is **computed** from `Contract.status` + the digest's evidence
verdict + the override event log — **never a new field stored on Contract**,
so it can never drift out of sync with the data it's derived from.
`TESTING`/`EVALUATING`/`PROMISING`/`ROBUST` already happened autonomously
before this slice (the scheduler and evaluator have no human step). The only
genuinely new transition is the one into `TESTING` via autonomous promotion
(§3).

`ROBUST` = `PROMISING` **and** at least one validation/holdout sibling
contract (`priority.is_confirmation_experiment`) has itself been scored with
a positive direction (`comparison.classify_variant`).

## 3. Non-terminal rejection & reassessment — no calendar, ever

**"REJECTED" never means "never again."** An opportunity becomes
`REASSESSING` the moment its `evidence_signature` — a plain, inspectable
string (`v=<verdict>;n=<variants_scored>;siblings_promising=<count>`), never
an opaque hash — differs from the signature recorded the last time it was
looked at. Nothing here uses `datetime.now()`, a fixed retest period, or any
wall-clock input at all (mechanically proven by
`tests/test_research_opportunity.py` §E). Concrete triggers this covers
today: a new variant of the *same* hypothesis gets scored; a *sibling*
hypothesis in the same research area turns `PROMISING` ("a related strategy
succeeds", straight from the Master Vision's own trigger list); an explicit
user `force_reassess()`.

A `REASSESSING` opportunity's priority score rises relative to the same
opportunity sitting idle as plain `REJECTED` (the `reassessment_rising_value`
component) — but note the *research-area-balance* component can pull the
other way once an area gets crowded, so the net effect on any one opportunity
depends on the full component breakdown, always visible via
`priority_components`.

**What this v1 does NOT do:** automatically launch a *new* discovery attempt
targeted at a newly-`REASSESSING` hypothesis. The existing discovery prompt
(`routines/research_investigate.md`, "Inspect prior evidence before
proposing") already reads the digest's evidence section and reasons about
WEAK/CONTRADICTED entries when the Research AI runs — this slice makes
*which* rejected work is actually worth surfacing computed and explainable,
not the act of re-proposing itself. Explicit remaining gap; see §8.

## 4. Priority — transparent, versioned, explainable

`PRIORITY_MODEL_VERSION = "v1"`. A **named, weighted sum** of independently
inspectable components (`Opportunity.priority_components`) — never a single
opaque formula:

| component | weight | what it measures |
|---|---|---|
| `evidence_strength` | 3.0 | verdict quality (PROMISING highest, REDUNDANT lowest, unscored = neutral 0.5) |
| `novelty` | 2.0 | 0 if an exact-duplicate rule spec exists anywhere in the registry |
| `confirmation_bonus` | 2.5 | is this a robustness/OOS test of an already-promising parent |
| `research_area_balance` | 1.0 | round-robin across research areas — an under-represented area scores higher |
| `reassessment_rising_value` | 2.0 | is there genuinely new evidence since this was last looked at |
| `compute_cost_penalty` | −1.5 | a static, per-type relative cost estimate (§5) |
| `age_bonus` | 0.5 | a still-unlocked draft never starves behind a backlog |

Not claimed to be calibrated or optimal — the same "deliberately conservative
placeholder" posture every other bounded constant in this codebase takes
(`WorkerLimits`, `MAX_LOCKS_PER_PERIOD`, ...). Recalibrating these weights
from real outcome data is explicit future work. "Why is this opportunity
#1" is always answerable by reading `priority_components` — no black box.

## 5. Compute allocation — an interface, not a resource model yet

Per-type `_TYPE_COMPUTE_COST` is a static, documented estimate (a discovery
attempt ≈ an AI subprocess call, costs more; a robustness test ≈ one bounded
backtest, costs less) — explicitly **not** a CPU/memory profiler (out of
scope for v1; the Master Vision itself asks only for "resource-awareness
INTERFACES", not a resource model, in this slice). The actual bounding
mechanism is still `WorkerLimits` (`max_discovery_attempts`,
`max_experiments`, **`max_promotions`** — new, default 1 — and
`max_runtime_seconds`), unchanged in kind from v0. Widening these, or
replacing the static cost table with real measured cost, is future work.

## 6. Autonomous promotion — the one new consequential action

`opportunity.attempt_autonomous_promotion()`:

1. Refuses a frozen/retired opportunity outright (`skipped_frozen`) — an
   active user override always wins.
2. Refuses anything not `DISCOVERED`/`TRIAGED` or without a contract
   (`skipped_not_eligible`).
3. Refuses an exact-duplicate rule spec (`skipped_duplicate`) —
   `similarity.is_duplicate()`, unmodified.
4. Checks the **existing, unmodified** research budget
   (`hypothesis_intake.check_research_budget()`, 5 locks / 7 days by
   default) — over budget → `skipped_budget`, and `approve_and_lock()` is
   never even called.
5. Calls `hypothesis_intake.approve_and_lock(store, contract_id,
   approved_by=SYSTEM_APPROVER, ...)` — the **exact same, byte-for-byte
   unmodified** function a human calls. `SYSTEM_APPROVER =
   "autonomous_control_plane.v1"` — an explicit, named, greppable,
   versioned identity, never blank, never a forged human name. The audit
   note `approve_and_lock()` itself already writes records exactly this
   value, so "who approved this" is never ambiguous.

`hypothesis_intake.py` is **not modified by this slice** — same signature,
same mandatory-approver check, same budget gate, same model-firewall check
inside `Contract.check()`. The only new thing is a second, honest, always-
distinguishable caller.

## 7. Portfolio relevance — deliberately inert

`Opportunity.portfolio_relevance` exists as a field so a future Autonomous
Portfolio Engine slice can populate it (diversification opportunity, overlap,
missing exposure, capacity, hedge need). It is **always `None`** in v1 — no
fake portfolio engine was built to populate it (Master Vision §10 explicit
instruction).

## 8. User override authority — first-class, audited, always wins

```python
from research.brain import opportunity as opp
opp.freeze(store, "OPP-<hid>", by="vaibhav", reason="...")     # suspend
opp.reopen(store, "OPP-<hid>", by="vaibhav", reason="...")     # clears frozen AND retired
opp.retire(store, "OPP-<hid>", by="vaibhav", reason="...")     # required, non-empty reason
opp.force_reassess(store, "OPP-<hid>", by="vaibhav", reason="...")  # one-shot, self-consuming
```

A frozen or retired opportunity scores `priority_score = 0.0` and
`attempt_autonomous_promotion()` refuses it outright — the AI never silently
overwrites an active freeze (mechanically proven,
`tests/test_research_opportunity.py` §I, and at the worker-integration
level, `tests/test_research_worker.py` §R6). `reopen()` is the one override
that always wins over everything, including retirement — matching the
Master Vision's own example: *AI:REJECTED → User:REOPENED → AI:REASSESSED →
AI:PROMOTED → User:FROZEN*.

If the user does nothing, the autonomous lifecycle continues exactly as
described above — no UI dependency, no waiting state.

## 9. Audit trail

Every lifecycle transition, override, and promotion attempt is one row in
`research.memory`'s **existing** `observations` table, under the new
`research_opportunity_event` dataset — no new SQL table, no new file, the
same append-only, typed-wrapper convention every other research-memory
dataset already uses. `opportunity.override_history(store, opportunity_id)`
returns the full ordered trail (timestamp, actor, event_type, reason,
previous_state/new_state, priority/confidence before/after) — the backing
data for a future hypothesis dossier UI (§13 of the Master Vision; not built
in this slice — see §11).

Current state (frozen? retired? last known stage/signature?) is always a
**fold over this history**, never a separately-maintained field — the exact
same "last write wins over an append-only log" pattern
`research_areas.py`'s own `_latest_tags()` already uses.

## 10. Worker integration — the unified action loop (v2)

**v1** ran three FIXED phases every heartbeat: experiments, then discovery,
then (new that slice) up to `max_promotions` autonomous promotions — always
in that order, regardless of which one was actually most valuable. A
brand-new discovery attempt always ran; a rejected hypothesis whose evidence
just turned in its favour had no way to ever outrank it.

**v2** (Unified Opportunity-Driven Worker Selection) replaces the fixed
order with one iterative, priority-driven loop, backed by a new function,
`opportunity.build_action_queue(store, pool, registry_dir=..., discovery_available=...)`:

```
while budget_left() > 0:
    pool  = opportunity.build_opportunity_pool(...)          # refresh
    queue = opportunity.build_action_queue(store, pool, ...) # RUN_EXPERIMENT / PROMOTE / DISCOVER,
                                                              # ALL scored via the SAME
                                                              # priority_score()/_priority_components()
    eligible = [a for a in queue if that kind still has room under its own cap]
    if not eligible: break
    action = eligible[0]                                     # the single highest-priority one
    execute action.kind:
        RUN_EXPERIMENT -> scheduler.run_one_experiment(contract_id)   # NEW — single-contract
                                                                       # entry point, same runner
        PROMOTE        -> opportunity.attempt_autonomous_promotion()  # unchanged
        DISCOVER       -> investigator.investigate()                  # unchanged
    record the outcome in action_log; loop
```

Three action kinds, one scale:

- **RUN_EXPERIMENT** — one per contract in `scheduler.eligible_contracts()`
  (the same priority-ordered, locked-and-not-yet-run list `run_scheduler()`
  already consumed), scored via the pool `Opportunity` for that contract's
  hypothesis. A contract with no linked hypothesis row at all (a fixture, or
  a lock made outside the normal intake path) still gets a fallback score
  computed with the exact same formula — a genuinely runnable, already-
  approved experiment must never silently disappear from consideration.
- **PROMOTE** — one per pool `Opportunity` that is `DISCOVERED`/`TRIAGED`,
  has a `contract_id`, and is not frozen/retired — unchanged eligibility
  from v1, just RANKED here instead of always-attempted.
- **DISCOVER** — at most one synthetic action (there is only one "run the
  Research AI" step per heartbeat), scored by handing
  `_priority_components()` a neutral, synthetic input
  (`_discovery_priority_components()`) — the exact same formula, never a
  second one.

Frozen/retired opportunities are excluded from the queue outright, not
merely zero-scored — a user override must be unselectable, never just
outranked. `scheduler.run_one_experiment()` is new (alongside the existing
batch `run_scheduler()`, unchanged) so the worker can interleave a single
experiment with other kinds of work between calls; `run_scheduler()` itself
now calls it internally too, so there is one execution path, not two.

**The key consequence** (Master Vision's own illustration): a new discovery
attempt (baseline priority ≈ 3.0 in this scale), a robustness/validation
test against an already-PROMISING parent (≈ 7.5, thanks to the
`confirmation_bonus` component), and a rejected hypothesis whose evidence
just turned in its favour (≈ 3.9, thanks to `reassessment_rising_value`) all
compete directly — the worker picks the robustness test, then the rejected
hypothesis, before ever running discovery, without any special-cased phase
order. See `tests/test_research_worker_selection.py` §B/§C for the
mechanical proof with real evidence-bearing fixtures.

**Re-ranking mid-heartbeat**: because the pool and queue are rebuilt every
iteration, a result from one action can change the very next selection
within the SAME heartbeat — e.g. discovery creates a draft, which one
iteration later becomes the highest-priority PROMOTE candidate, which once
promoted becomes the highest-priority RUN_EXPERIMENT candidate, all before
the runtime budget is spent (`tests/test_research_worker_selection.py` §D/§E).

**Resource limits are unchanged in kind** — `max_experiments`,
`max_discovery_attempts`, and `max_promotions` are still hard per-kind caps
(never removed or raised by this slice), plus the overall
`max_runtime_seconds` and the single-worker `flock`. A purely defensive
iteration-count safety net (`max_iterations`, generous, never reached in
ordinary operation) guards against a hypothetical bug turning the loop
unbounded — the REAL bounding mechanism is that every executed action
increments its own capped counter.

`WorkerRunResult` gains one new field over v1: `action_log` — a tuple of
per-selected-action records (kind, opportunity/contract id, priority_score,
priority_components, compute_cost_estimate, relevance, confidence/lifecycle
before and after, the runner-up's priority at selection time, outcome,
runtime consumed) — outcome-aware telemetry for a future priority-model
calibration pass, not a dashboard payload. `opportunities_considered`,
`promotions_attempted`, `promoted_hypothesis_ids`, `promotion_outcomes`,
`reassessment_eligible_count` from v1 are unchanged in meaning.

### New config

| Limit | Default | Env var | Flag |
|---|---|---|---|
| `max_promotions` | 1 | `RESEARCH_WORKER_MAX_PROMOTIONS` | `--max-promotions` |

`venv/bin/python -m research.brain.worker --max-promotions 0 ...` disables
autonomous promotion for a run while leaving discovery/experiments
untouched. No cron change is required to adopt this — the existing
`deploy/research.cron` heartbeat lines already invoke the worker with
defaults, and `max_promotions=1` is exactly that: a default, not a
requirement to edit the cron.

## 11. Safety — what this slice cannot do (true by absence, not by convention)

- **No second locking path.** `research/brain/worker.py` never imports or
  calls `hypothesis_intake.approve_and_lock` or `Contract.lock()` directly —
  the only call site in the entire control plane is inside
  `opportunity.attempt_autonomous_promotion()`, and it is exactly one call
  (`tests/test_research_worker.py` §J mechanically counts it).
- **No second substrate-creation path either (v3).** The only call site for
  `hypothesis_intake.derive_split_contract()` in the entire control plane is
  inside `opportunity.attempt_create_experiment()` — exactly one call
  (`tests/test_research_action_expansion.py` §M mechanically counts it).
  `attempt_create_experiment()` produces a DRAFT only; it never locks
  anything itself — locking still goes exclusively through
  `attempt_autonomous_promotion()` above.
- **No engine/broker/paper import anywhere.** `research/brain/opportunity.py`
  imports no `engine.*`, no `paper.*`, no broker module, never reads
  `memory/state.json`. `engine/` still imports nothing from `research/`
  (kernel isolation re-proven, `tests/test_research_opportunity.py` §K,
  `tests/test_kernel_isolation.py`).
- **No path to `run_experiment()`/`simulate()` from this module.** Running
  an experiment stays entirely `scheduler.py`'s job, unmodified.
- **No new source of truth.** The only durable writes are the
  `research_opportunity_event` rows described in §9 — the research Store
  remains the sole authority.
- **Autonomous promotion never reaches capital or execution.** It moves a
  hypothesis from DISCOVERED/TRIAGED to TESTING (a locked, about-to-be-
  historically-simulated Contract) — nowhere near a broker, an order, or
  `memory/state.json`.
- **The research budget is never bypassed or overridden by this slice.**
  `hypothesis_intake.check_research_budget()` (5 locks / 7 days, unchanged)
  gates every autonomous promotion exactly as it gates a human's own
  `approve_and_lock()` call; the existing `budget_override_by` escalation
  path (a second, distinct human name) is never auto-supplied here.

## 12. Remaining gaps vs. the Master Vision (honest, not implemented here)

- **True compute-aware resource allocation.** §10's unified loop ranks
  actions by research VALUE, not by a live measurement of CPU/memory/API
  budget consumed — `compute_cost_estimate` is still the same static,
  per-type table from §5, not a profiler. The per-kind caps
  (`max_experiments`/`max_discovery_attempts`/`max_promotions`) remain hard
  ceilings, not a true shared resource budget a scheduler could trade off
  against each other (e.g. "spend the discovery budget I didn't use on one
  more experiment instead"). Explicitly deferred, per this slice's own
  instructions, until production telemetry (`action_log`, §10) shows how
  compute is actually consumed.
- **Reassessment triggering new work is now PARTIALLY closed (v3, §14).**
  A REASSESS_REJECTED (or PROMISING-but-unconfirmed) opportunity with no
  runnable substrate can now cause the worker to create one itself — but
  only via ONE strategy (deriving an unused, pre-declared validation/
  holdout split from an already-locked contract of the SAME hypothesis).
  A hypothesis that never pre-declared such a split, on any of its
  ever-locked contracts, still produces no Action — see §14's own
  "remaining gaps" for exactly what is and isn't covered now.
- **No Strategy Factory, Portfolio Engine, or Capital Allocation.** Exactly
  as the roadmap orders them — this slice is "Autonomous Research Control
  Plane" only. `portfolio_relevance` is a placeholder field, nothing more.
- **Priority weights are v1 placeholders**, not calibrated against real
  outcomes.
- **No hypothesis-dossier UI.** The backing data (`override_history`, every
  `Opportunity` field) exists; no frontend/API surface was built (explicitly
  out of scope — "UI implementation may be limited in this slice to the
  minimum necessary API/domain support... do not turn this into a large
  frontend redesign").

## 13. Tests

`tests/test_research_opportunity.py` (75 checks) + `tests/test_research_worker.py`
§R (10 checks, worker-integration level) + `tests/test_research_worker.py`
§C/§J updates for the v1 capability. See each file's own section headers
for exact coverage: lifecycle stage computation (all 9 reachable), end-to-end
autonomous promotion, priority explainability/sensitivity, non-terminal
rejection + reassessment (no calendar dependency), confidence, budget
bounding, duplicate respect, full user-override model, audit trail,
isolation, determinism.

**v2 (Unified Opportunity-Driven Worker Selection)** —
`tests/test_research_worker_selection.py` (40 checks): discovery competing
directly with an already-locked experiment on one scale (§A); a real,
evidence-bearing robustness test outranking discovery (§B); a real,
evidence-bearing rejected hypothesis re-entering and outranking discovery
once a sibling turns PROMISING (§C); an executed action's result changing
the very next selection within the same heartbeat, and multiple action
kinds running within one runtime envelope (§D/§E); a deterministic
fake-clock proof that the worker stops at its runtime budget with eligible
work still left (§F); an abundant-work proof against an infinite loop
(§G); duplicate/low-information deprioritization via the `novelty`
component (§I); frozen/retired opportunities excluded from the action queue
outright, and `reopen()` restoring eligibility (§J/§K); `portfolio_relevance`
still unfabricated (§L); isolation re-proven for the new call graph (§M).
Plus targeted updates to `tests/test_research_worker.py` (still 97/97): §L1's
mocked failure moved from the old batch `run_scheduler()` (no longer called
by the worker) to the new `scheduler.run_one_experiment()`; §R2 updated to
assert "no longer a draft" rather than exactly "locked", since the unified
loop may legitimately also run the just-promoted contract's own experiment
within the same heartbeat once nothing else outranks it.

**v3 (Autonomous Research Action Expansion)** —
`tests/test_research_action_expansion.py` (55 checks): a reassessed
opportunity WITH an existing runnable contract still selects RUN_EXPERIMENT,
never CREATE_EXPERIMENT (§A); one WITHOUT a runnable contract, but with an
unused pre-declared split, selects CREATE_EXPERIMENT instead (§B); the
created substrate is correctly linked — hypothesis-claim row, `split_of`/
`split_key`, and a `SUBSTRATE_CREATED` audit event carrying full lineage
(§C); it becomes an ordinary PROMOTE candidate on the very next queue
rebuild, sourced via `hi.pending_drafts()` (§D); a second attempt for the
same, already-used split is refused, not silently repeated (§E); a frozen
(§F) or retired-unless-reopened (§G) opportunity cannot generate substrate;
new evidence carries a previously-REJECTED (not just REASSESSING)
opportunity all the way to an executable, discovery-beating priority (§H);
a substantially higher-value existing experiment is never outranked by a
lower-value CREATE_EXPERIMENT (§I); `max_substrate_creations` bounds
attempts exactly like every other per-kind cap, across two independently-
eligible opportunities (§J); the full worker loop can create substrate,
promote it, AND run it, all in one heartbeat (§K); isolation (§L) and every
existing safety boundary (§M, including the single `derive_split_contract`
call site and the research budget continuing to gate ONLY locking, never
draft creation) are re-proven unchanged. Also: `test_research_opportunity`
(75/75), `test_research_worker` (97/97), `test_research_worker_selection`
(40/40), all isolation suites, the full Python suite (2577/0 across 50
modules), and the frontend suite (60/0) all stay green.

## 14. Autonomous Research Action Expansion (v3) — substrate creation

### Why an opportunity can require substrate creation

§10's unified queue only ever offers RUN_EXPERIMENT for a contract
`scheduler.eligible_contracts()` says is genuinely locked-and-runnable, and
PROMOTE for a genuinely pending draft. A hypothesis that already ran to
completion — the ordinary shape of a REJECTED/REASSESSING opportunity, and
also a PROMISING-but-not-yet-ROBUST one with no confirmation sibling ever
derived — has NEITHER. Its priority score could rise arbitrarily high
(§3's reassessment trigger, or simply staying PROMISING) with nothing in
§10's queue for the worker to actually DO about it. v3 closes most of that
gap: when a high-priority opportunity has no executable path, the worker
can create the smallest one available itself.

### The one substrate strategy implemented: split derivation

`research.brain.opportunity._available_split_key()` looks, read-only, for
the first `(parent_contract_id, split_key)` pair the hypothesis could still
derive a sibling from — a contract that has EVER been locked (any
post-lock status: locked/running/reported/abandoned), that pre-declared a
`"validation"` or `"holdout"` split in its own `splits` dict, and where
that exact split has never been derived before. `attempt_create_experiment()`
then calls the EXISTING, UNMODIFIED `hypothesis_intake.derive_split_contract()`
on it — the same primitive a human research reviewer would use by hand —
producing a new DRAFT contract that tests the SAME rule spec on a
DIFFERENT, pre-committed evaluation window. No new rule is ever invented,
and no Research AI subprocess is ever called: this is a fully deterministic
action.

This is deliberately the ONLY strategy in v3. A hypothesis that never
pre-declared a validation/holdout split on any of its ever-locked contracts
has no substrate for this strategy to create — it stays fully visible and
correctly scored in the pool, but produces no CREATE_EXPERIMENT action (see
this section's own "What remains deferred" below). `Action.substrate_type` names the strategy
explicitly (`"split_derivation"` today) so a future second strategy adds a
new value there, never a new action kind — the priority scale stays flat.

### How duplicate prevention works

Two layers, both reused rather than reimplemented:

1. `_available_split_key()` itself will not return a `(parent, split_key)`
   pair that a hypothesis-claim row already records as derived (reading the
   SAME `split_of`/`split` metadata `derive_split_contract()` has always
   written) — so `build_action_queue()` never even OFFERS a CREATE_EXPERIMENT
   action for an already-exhausted split.
2. `hypothesis_intake.derive_split_contract()`'s own existing single-use
   guard enforces the same rule authoritatively at execution time, for the
   rare case something else derived it between selection and execution.

A hypothesis with BOTH `"validation"` and `"holdout"` pre-declared can
legitimately receive CREATE_EXPERIMENT twice — once each — never more; this
is a second genuine research question (a holdout test after a validation
one), not a duplicate.

### How reassessed/rejected work becomes actionable

CREATE_EXPERIMENT is offered for any opportunity, of any type, that (a) has
no other executable path this iteration (no RUN_EXPERIMENT/PROMOTE
candidate — tracked via `hids_with_a_path` in `build_action_queue()`), (b)
is not frozen/retired, (c) is NOT plain `REJECTED` (see below), (d) has a
recorded evidence verdict (an untested idea is DISCOVER/PROMOTE's job), and
(e) has an available split per `_available_split_key()`. This is a
mechanical condition, not a `REASSESS_REJECTED`-specific branch — it
naturally covers the motivating case (a rejected hypothesis whose
reassessment value just rose) and, on equal footing, a PROMISING-but-not-
yet-ROBUST hypothesis that never had a confirmation sibling derived.

**The one explicit stage exclusion: plain `REJECTED`.** A REJECTED
opportunity with `reassessment_eligible=False` has, by construction,
nothing about its evidence picture that has changed since it was last
looked at — offering it a substrate anyway would be an unconditional retry,
exactly the "arbitrary retry period" this slice's own instructions forbid.
The moment a sibling turns PROMISING, a new variant is scored elsewhere, or
a human calls `force_reassess()`, `build_opportunity_pool()` itself
reclassifies it `REASSESSING` — and only then does this loop offer it a
substrate. The trigger is the SAME evidence-signature change §3 already
defined; v3 adds no new trigger of its own, only a new thing to DO once
triggered. No six-month timer, no fixed retry count, anywhere.

### How CREATE_EXPERIMENT participates in the unified queue

Scored with `opportunity.priority_score`/`priority_components` directly —
the SAME value the opportunity already earned, no second formula. This
guarantees a substantially-higher-value existing RUN_EXPERIMENT (e.g. a
robustness test, §10's ≈7.5 example) is never outranked by a lower-value
CREATE_EXPERIMENT (e.g. a merely-reassessed idea, ≈3.9) — they compete on
the identical scale everything else does (`tests/test_research_action_
expansion.py` §I). `Action` exposes `opportunity_id`, `hypothesis_id`,
`contract_id` (the PARENT it would derive from), `priority_score`,
`priority_components`, `compute_cost_estimate`, `confidence`, `novelty`,
`evidence_value`, `relevance`, and `substrate_type` — the same explainable
shape every other action kind already has.

`research.brain.worker`'s unified loop dispatches it through
`opportunity.attempt_create_experiment()`, bounded by a new
`WorkerLimits.max_substrate_creations` (default 1, env
`RESEARCH_WORKER_MAX_SUBSTRATE_CREATIONS`, flag `--max-substrate-creations`)
— the same conservative-default posture as `max_promotions`, enforced the
same way, never gated by the research budget (creating a draft stays as
unlimited as `create_draft()` itself always was; only LOCKING it is
budget-gated). Because the created draft becomes an ordinary PROMOTE
candidate on the very next pool/queue rebuild — sourced from
`hypothesis_intake.pending_drafts()` directly, the same "authoritative
external state" pattern RUN_EXPERIMENT already used via
`scheduler.eligible_contracts()`, precisely because a hypothesis with more
than one contract has a pool `Opportunity` whose own `contract_id`/
`lifecycle_stage` describe its OLDER, already-scored representative
contract, never a fresh sibling — the worker can create a substrate,
promote it, and run it, all in the same heartbeat, when priority and budget
allow (`tests/test_research_action_expansion.py` §K).

### Lineage

Every generated experiment is traceable: the hypothesis-claim row
`derive_split_contract()` itself writes carries `split_of` (the parent) and
`split` (the key); `attempt_create_experiment()` additionally records a
`SUBSTRATE_CREATED` audit event (the SAME `research_opportunity_event`
dataset every other control-plane event lives in) whose `extra` payload
carries `parent_contract_id`, `created_contract_id`, `substrate_type`, and
`split_key`, with `reason` naming the split derived. The full chain —
opportunity → hypothesis → reassessment reason → generated substrate →
experiment → evidence — is reconstructable from `opportunity.override_history()`
plus the hypothesis-claim log, with no new store, no new table.

### What remains deferred

- **Only one substrate strategy.** A hypothesis with no pre-declared
  validation/holdout split on any ever-locked contract still gets no
  CREATE_EXPERIMENT action. A second strategy (e.g. asking the Research AI
  for a targeted retest variant of a SPECIFIC rejected hypothesis, rather
  than an arbitrary new idea) is future work — it would add a new
  `substrate_type` value, not a new action kind.
- **No true shared compute allocator.** `max_substrate_creations` is a hard
  per-kind ceiling, exactly like every other cap — not yet traded off
  against `max_experiments`/`max_discovery_attempts`/`max_promotions` by a
  resource-aware scheduler. `attempt_create_experiment()`'s own compute
  cost is inherited from the opportunity's `compute_cost_estimate`, the same
  convention PROMOTE already used, not a fresh profiler measurement.
- **No portfolio-relevance-driven substrate priority.** `portfolio_relevance`
  stays the same inert placeholder field it has always been (§7) — this
  slice does not let a portfolio-level gap raise a substrate's priority.
- **No dashboard/dossier UI change.** The lineage above is fully queryable
  today; no new frontend or API surface was built for it.
