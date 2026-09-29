# Experiment engine performance and scaling report

Measured on the production one-vCPU VPS on 26 September 2026. The optimization keeps the
existing 180-second hard deadline, point-in-time membership, as-of price visibility,
transaction costs, position lifecycle and locked Contract semantics unchanged.

## Outcome

The representative three-year Nifty 500 replay now finishes in **16.359 seconds**, about
17.7 times faster than the approximately 290-second legacy projection and comfortably inside
the 180-second production limit. A governed production Contract subsequently completed in
10.064 seconds, persisted 2,061 simulated trades, reached `REPORTED`, and created its first
legitimate evidence update. This work did not submit or enable live orders.

## Root cause

The legacy engine performed one SQLite price-window query for nearly every symbol/session
combination. A one-month profile executed 10,009 SELECTs and spent 5.368 of 7.403 seconds in
price access. A three-month profile executed 30,527 SELECTs and spent 17.072 of 23.821 seconds
there. CPU and memory were stable; the failure was a bounded but excessive N+1 data-access
pattern, not a deadlock, runaway loop or memory leak.

## Before and after

| Workload | Legacy wall | Bulk wall | Rows | Symbols | Sessions | Queries | Peak RSS | Result |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| SMALL — Jan 2022 | 7.403 s | 0.603 s | 38,431 | 499 | 20 | 5 | 73.4 MiB | 8 trades; exact P&L match |
| Equivalence — Q1 2022 | 23.821 s | 1.280 s | 60,977 | 531 | 61 | 5 | 91.0 MiB | 28 trades; exact P&L match |
| MEDIUM — 2022 | not rerun | 2.881 s | 161,040 | 555 | 248 | 5 | 167.8 MiB | 128 trades |
| LARGE — 2022–2024 | ~290 s projected | 16.359 s | 459,325 | 630 | 739 | 5 | 392.2 MiB | 294 trades |

The Q1 equivalence run produced the same ordered trade records and exact net P&L
(`-32576.69584436`) as the legacy engine. The January comparison also matched exactly
(`25883.09086117`). Automated fixtures additionally compare every trade field and verify
point-in-time membership behavior.

## Implementation

`research/experiments/bulk_replay.py` resolves each evaluation session's historical universe
from membership observations, retrieves the bounded price range in symbol batches, computes
supported price features in arrays, and then applies the same path-dependent portfolio rules.
It preserves universe order because that order determines deterministic trade persistence.

The fast path refuses unsafe equivalence assumptions. Duplicate symbol/session rows,
late-arriving prices, unsupported operators, and non-empty corporate-announcement event history
fall back to the exact legacy bitemporal replay. An empty announcement dataset is safe: the
event-frequency z-score is undefined in both implementations and therefore cannot create a
signal.

## Production stage profile

| Stage | Seconds | Share of wall time |
|---|---:|---:|
| Trading calendar | 0.595 | 3.6% |
| PIT universe resolution | 0.337 | 2.1% |
| Bulk SQLite retrieval | 13.053 | 79.8% |
| Feature construction | 2.250 | 13.8% |
| Signal generation | 0.057 | 0.3% |
| Portfolio simulation | 0.054 | 0.3% |
| Transaction costs | 0.001 | <0.1% |

The remaining bottleneck is the bounded SQLite retrieval, not the simulation. The full run used
8.693 CPU seconds during 16.359 wall seconds, so adding CPU alone is unlikely to produce a
proportional improvement. More RAM would add operating headroom but is not necessary for the
representative workload; the measured peak was about 392 MiB on the current roughly 1 GiB VPS.

## Admission, cancellation and recovery

The preflight estimator uses the measured production calibration points and classifies work as
`SMALL`, `MEDIUM`, `LARGE` or `OVERSIZED`. Work predicted to finish inside the configured
deadline enters the normal single-worker lane. Compatible work predicted to exceed it is marked
`PARTITION`; incompatible heavy work is marked `DEFER_TO_HEAVY_QUEUE`. Deferral is operational,
not scientific evidence, and does not mutate the locked Contract.

The process-group supervisor remains the final enforcement boundary. A timeout terminates the
whole child group, releases its files and database connection, abandons the Contract with an
explicit non-scientific `TIMEOUT`, and never retries it silently. Two consecutive production
timeouts without an intervening completion automatically pause `EXPERIMENTS` and send one
exception alert. A successful completion resets that timeout streak.

## Telemetry and operator workflow

The authenticated `GET /research/experiment-telemetry` endpoint and the existing Overview,
Research and System screens expose:

- desired experiment state and reason;
- queue/status counts, current Contract, PID, stage and elapsed time;
- last completion, last timeout and 24-hour timeout count;
- median completed runtime;
- engine, compute class, rows, symbols, query count, peak memory and stage timings.

Run a read-only benchmark without changing Contract lifecycle state:

```bash
venv/bin/python scripts/benchmark_experiment.py --contract-id EXP-162BB2-F \
  --start 2022-01-01 --end 2024-12-31 --engine auto
```

Use `--engine legacy` only for a deliberately bounded equivalence sample. Never raise the hard
deadline merely to make an unmeasured workload pass.

## Scaling decision

The current single-worker topology is sufficient for the measured three-year Nifty 500 lane.
There is no evidence that a larger VPS is required for correctness or deadline compliance.
Before increasing infrastructure, the next optimization should reduce SQLite materialization
cost (for example, a versioned columnar research snapshot) while preserving the same bitemporal
contract and byte-equivalence tests. Partitioning is reserved for a future workload whose
measured estimate exceeds 180 seconds; it is not needed for the current production backlog.
