# Deploying to the VPS

One folder. Unzipping it over an existing install is safe: **every file that
holds live state or secrets is deliberately excluded from this archive.**

Not in this zip, and therefore never overwritten:

```
.env                              your keys
memory/state.json                 LIVE CAPITAL STATE
memory/trades.jsonl               your trade history
memory/regime_log.jsonl           your regime history
memory/trade_log.md               your logs
memory/research_log.md
memory/portfolio_state.md         regenerated automatically anyway
memory/.indstocks_session.json    broker session
memory/.instruments.json
logs/  venv/  research/*.db
```

`memory/state.json.template` is included for a fresh install only.

---

## A. Upgrading an existing install (your case)

```bash
# 1. Back up first. Takes two seconds and removes all doubt.
cd /root
tar czf trading-agent-backup-$(date +%Y%m%d-%H%M).tar.gz trading-agent

# 2. Unpack over the top
unzip -o trading-agent-vulture.zip -d /root/

# 3. Confirm your live state survived
cd /root/trading-agent
cat memory/state.json | head -6        # should still show YOUR capital
ls -la .env                            # should still be there

# 4. No new dependencies, but re-sync anyway
venv/bin/pip install -r requirements.txt

# 5. Prove nothing in the trading system moved
venv/bin/python -m tests.test_guardrails        # must be 62 passed, 0 failed
venv/bin/python -m tests.test_indicators        # must be 41 passed, 0 failed

# 6. Prove the research layer works
venv/bin/python -m tests.test_research_store        # 43
venv/bin/python -m tests.test_research_sources      # 51
venv/bin/python -m tests.test_kernel_isolation      # 12
venv/bin/python -m tests.test_replay_leakage        # 40

# 7. One command that checks everything and demonstrates the time machine
venv/bin/python -m research.verify_install
```

**Step 5 is the important one.** Those two suites test the untouched trading
kernel. If either number changed, something moved in `engine/` that should not
have — restore the backup and tell me.

---

## B. Fresh install

```bash
unzip trading-agent-vulture.zip -d /root/
cd /root/trading-agent

python3 -m venv venv
venv/bin/pip install -r requirements.txt

cp .env.example .env && nano .env            # fill in your keys
cp memory/state.json.template memory/state.json
nano memory/state.json                       # set allocated_capital

venv/bin/python -m research.verify_install
```

Then follow `docs/VPS_DEPLOY.md` and `docs/ENGINE_DEPLOY.md` for the trading
side.

---

## After install — the research layer, in order

Full detail in `docs/RESEARCH_DEPLOY.md`. The short version:

```bash
cd /root/trading-agent

# STEP 0 — the gate. ~4 min. Decides whether EXP-B1 is buildable as designed.
venv/bin/python research/probe/probe.py | tee logs/probe-$(date +%Y%m%d).txt

# STEP 3 — backfill. Start it now; it does not wait for anything.
venv/bin/python -m research.backfill membership          # seconds
tmux new -s backfill                                     # then inside tmux:
venv/bin/python -m research.backfill prices --from 2019-01-01 --to 2026-08-31

# STEP 2 — the recorder. Run once by hand before trusting cron.
venv/bin/python -m research.recorder --cycle intraday
venv/bin/python -m research.backfill status
```

Then add the recorder cron block from `docs/RESEARCH_DEPLOY.md` §3. It is a
**separate** set of crontab lines from the trading cycles — never edit
`run_cycle.sh` to call it.

---

## What is in this folder

```
engine/          the trading system. guardrails.py and execute.py are FROZEN.
                 market_data / regime / screener gained an optional as_of
                 parameter that defaults to None — live behaviour is unchanged,
                 and the 103 existing tests are the proof.

research/        the laboratory. Imports from engine; engine never imports it.
  store.py       bitemporal append-only store (SQLite, triggers enforce it)
  replay.py      the time machine + leak_check + the clock ban
  contracts.py   experiment contracts, the data firewall and the model firewall
  recorder.py    STEP 2 — perishable feeds, own cron, no broker dependency
  backfill.py    STEP 3 — the unhurried historical loads
  probe/         STEP 0 — the gate
  sources/       one module per feed, each split into fetch() and parse()
  registry/      locked experiment contracts (the multiple-comparisons ledger)
  verify_install.py

tests/           249 tests. 103 of them are the untouched trading kernel.
docs/            deployment guides, including RESEARCH_DEPLOY.md
```

## The invariant this whole structure exists to protect

```
engine  ──X──>  research          enforced by tests/test_kernel_isolation.py
research ────>  engine
```

The trading system does not know the laboratory exists. It will not know until
a hypothesis has survived a pre-registered holdout and you decide, deliberately,
to connect them.

Nothing in the research layer places an order, reads a broker token, or touches
`memory/state.json`.
