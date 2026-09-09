# Deployment (Slice AA-prep: read-only dashboard API + frontend)

Full procedure for putting the **read-only dashboard API** (`api/`) on a VPS behind Caddy,
and the **dashboard frontend** (`frontend/`) on Netlify. Covers a clean Ubuntu/Debian-style
VPS from a bare SSH connection through a working, authenticated, HTTPS dashboard.

**This document does not deploy anything by itself.** Every command below is meant to be run
by you, on your VPS / GitHub / Netlify accounts. Nothing here was executed against real
infrastructure while preparing this repo.

**Scope.** This is deployment plumbing only — it changes nothing about how the trading agent
decides or executes trades. `engine/guardrails.py`, `engine/execute.py`, and every other
protected file are untouched (see `CLAUDE.md`'s own table and §28 "Operational safety"
below). The API this document deploys is the same read-only surface built in the Slice Z
work (`docs/API.md`): it cannot place, modify, or cancel an order, and taking it down or
bringing it up has zero effect on `run_cycle.sh`'s existing cron-driven trading cycles.

If you're deploying for the very first time, read `docs/VPS_DEPLOY.md` and
`docs/RESEARCH_DEPLOY.md` too — those cover the trading engine itself (Kite callback, cron
cycles, the research recorder) and are unaffected by anything in this document. This
document only adds the dashboard API/frontend alongside what's already there.

---

## 1. SSH connection

```bash
ssh root@YOUR_VPS_IP
```

(Matches this project's existing convention — `docs/VPS_DEPLOY.md` and `scripts/vps_setup.sh`
both assume you're logged in as `root`, no separate admin user. That convention is unchanged
here; only the new dashboard API service itself runs as a dedicated unprivileged user — see
§4 and §13.)

## 2. OS update

```bash
sudo apt update && sudo apt upgrade -y
```

## 3. Required system packages

```bash
sudo apt install -y python3-pip python3-venv git unzip curl
```

(`git` is new to this list — the rest matches `docs/VPS_DEPLOY.md` §1. Caddy's own install is
§18 below, same as `docs/VPS_DEPLOY.md` §1.)

## 4. Create a dedicated application user for the API service

The existing trading-cycle cron jobs stay on the established all-`root` convention
(`docs/VPS_DEPLOY.md`, `docs/RESEARCH_DEPLOY.md`) — changing that is out of scope here. The
**new dashboard API** is a different case: it's a network-facing HTTP service, so it gets its
own unprivileged system user with the minimum filesystem access it actually needs, rather
than inheriting root's full access to everything (including `.env`'s broker/Telegram
secrets, which this API never uses at all).

```bash
sudo useradd --system --no-create-home --shell /usr/sbin/nologin tradingapi
```

Filesystem permissions for this user are set in §13, after the repo is cloned.

## 5. Application directory

```bash
mkdir -p /root/trading-agent
```

(Same path `docs/VPS_DEPLOY.md` / `scripts/vps_setup.sh` already assume — `PROJECT_DIR` in
both.)

## 6. Git installation

Already done in §3 (`git` is in that `apt install` line). Confirm:

```bash
git --version
```

## 7. Python installation/check

```bash
python3 --version   # this repo assumes Python 3.11+ (developed/tested on 3.11.15)
```

If your VPS image ships an older Python 3, `sudo apt install -y python3.11 python3.11-venv`
(or whatever version is current) and use that explicitly in §9 below.

## 8. Cloning the GitHub repository

`docs/VPS_DEPLOY.md` §0 already documents this as the intended install method (it just
hadn't been set up yet — this repository had no git history until this deployment-prep
pass). See §22 "GitHub deployment model" below for the full push/pull workflow this
supports going forward; `docs/INSTALL.md`'s older zip-upload method still works too and
isn't being removed, but git is the path this document assumes from here on.

```bash
cd /root
git clone YOUR_GITHUB_REPO trading-agent
cd trading-agent
```

## 9. Virtual environment

```bash
python3 -m venv venv
```

## 10. Installing requirements

```bash
venv/bin/pip install -r requirements.txt
```

This now includes `gunicorn` (added for this deployment slice — see `requirements.txt`'s
comment) alongside the existing `flask`, `pandas`, `yfinance`, etc.

## 11. Environment file (existing trading system)

```bash
cp .env.example .env
nano .env   # KITE_API_KEY, KITE_API_SECRET, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, etc.
chmod 600 .env
```

Unchanged from `docs/VPS_DEPLOY.md` §2. The dashboard API does **not** read this file — see
§12.

## 12. Secrets configuration (dashboard API)

The dashboard API uses its **own**, separate, minimal secrets file — not `.env` above (see
`deploy/trading-api.service`'s comment for why: least privilege, this process should never
even have KITE/INDSTOCKS/TELEGRAM secrets in its environment).

```bash
cp deploy/api.env.example deploy/api.env
nano deploy/api.env
```

Set:

```bash
DASHBOARD_API_TOKEN=YOUR_API_TOKEN        # generate: python3 -c "import secrets; print(secrets.token_urlsafe(32))"
DASHBOARD_CORS_ORIGINS=https://your-dashboard-site.netlify.app
DASHBOARD_API_HOST=127.0.0.1
DASHBOARD_API_PORT=8787
```

```bash
chown root:tradingapi deploy/api.env
chmod 640 deploy/api.env
```

(`640` — the service reads it as group `tradingapi`; only root can write it. `docs/API.md`
§8 has the exact Netlify environment variable this same token needs to match.)

## 13. Filesystem permissions

```bash
# tradingapi (§4) needs to: import the Python package (read the repo tree),
# read the existing state files api/data.py serves over HTTP (memory/state.json,
# memory/trades.jsonl, memory/regime_log.jsonl, strategies/registry/,
# research/registry/), and write to research/ (Store.open() does a small
# idempotent meta upsert even for read-only callers — see api/data.py's
# module docstring) and logs/ (gunicorn's access/error log files).

sudo chgrp -R tradingapi /root/trading-agent
sudo chmod -R g+rX /root/trading-agent

# Re-lock everything secret back down — the two lines above are broad on
# purpose (simplest correct grant), these narrow it back for anything
# tradingapi must NOT be able to read:
sudo chmod 640 /root/trading-agent/.env
sudo chown root:root /root/trading-agent/.env
for f in memory/.kite_session.json memory/.indstocks_session.json; do
    [ -f "/root/trading-agent/$f" ] && sudo chmod 600 "/root/trading-agent/$f" && sudo chown root:root "/root/trading-agent/$f"
done
# deploy/api.env was already set to 640 root:tradingapi in §12 — leave it.

# Write access, only where actually needed:
sudo chmod -R g+w /root/trading-agent/research
sudo mkdir -p /root/trading-agent/logs
sudo chgrp tradingapi /root/trading-agent/logs
sudo chmod g+w /root/trading-agent/logs
```

Verify tradingapi genuinely cannot read the trading secrets:

```bash
sudo -u tradingapi cat /root/trading-agent/.env
# expect: Permission denied
sudo -u tradingapi python3 -c "print(open('/root/trading-agent/memory/state.json').read()[:40])"
# expect: this DOES succeed — state.json is meant to be readable (it's the
# same data /account and /positions already serve over HTTP given a valid
# bearer token; see docs/API.md)
```

## 14. API startup (manual check, before wiring up systemd)

```bash
cd /root/trading-agent
sudo -u tradingapi env $(cat deploy/api.env | grep -v '^#' | xargs) \
    venv/bin/gunicorn --bind 127.0.0.1:8787 --workers 2 api.wsgi:app
```

In another terminal:

```bash
curl http://127.0.0.1:8787/health
curl -H "Authorization: Bearer YOUR_API_TOKEN" http://127.0.0.1:8787/account
```

Ctrl+C the foreground gunicorn once both look right — §16 makes this permanent.

## 15. Production WSGI process

Already gunicorn, already tested in §14. `deploy/trading-api.service` wraps exactly that
command — see `api/wsgi.py`'s own docstring for why this file exists (a plain `app` object a
WSGI server can point at, no dev-server code path involved).

## 16. systemd service

```bash
sudo cp /root/trading-agent/deploy/trading-api.service /etc/systemd/system/trading-api.service
sudo systemctl daemon-reload
sudo systemctl enable --now trading-api
```

## 17. Service status/logs

```bash
sudo systemctl status trading-api        # expect: active (running)
tail -f /root/trading-agent/logs/api-access.log
tail -f /root/trading-agent/logs/api-error.log
journalctl -u trading-api -f             # service-lifecycle messages (start/stop/restart)
```

## 18. Caddy installation

Identical to `docs/VPS_DEPLOY.md` §1 (skip if already installed for the Kite callback
server):

```bash
sudo apt install -y debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
  | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
  | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt update && sudo apt install -y caddy
```

## 19. Caddy configuration

```bash
sudo cp /root/trading-agent/deploy/Caddyfile.tradingbotapi /etc/caddy/conf.d/tradingbotapi.caddy
```

If your Caddy install doesn't auto-include `conf.d/*` (check `/etc/caddy/Caddyfile` for an
`import conf.d/*` line — add one if it's missing), append
`deploy/Caddyfile.tradingbotapi`'s block directly into `/etc/caddy/Caddyfile` instead,
alongside the existing `tradingagent.bhardwajvaibhav.com` block.

```bash
sudo systemctl reload caddy
```

## 20. DNS prerequisites

An A (and/or AAAA) record for `tradingbotapi.bhardwajvaibhav.com` must point at this VPS's
public IP **before** Caddy can get a certificate — same requirement `docs/VPS_DEPLOY.md` §3
already documents for `tradingagent.bhardwajvaibhav.com`. This domain's DNS is managed in
**Netlify DNS** (per `docs/SETUP.md` §4, where the existing `tradingagent` record lives) —
add the new `tradingbotapi` record there the same way.

Ports 80 and 443 must also be reachable from the public internet on this VPS (80 for the
ACME HTTP-01 challenge, 443 to serve) — check your cloud provider's firewall/security-group
rules, not just `ufw`/`iptables` on the box itself.

```bash
ping tradingbotapi.bhardwajvaibhav.com   # should resolve to YOUR_VPS_IP before continuing
```

## 21. HTTPS verification

```bash
curl -v https://tradingbotapi.bhardwajvaibhav.com/health
```

Expect a valid certificate (no `-k` needed) and `{"status":"ok","service":"living-quant-api","version":"1.0.0"}`.
If DNS hasn't propagated yet this fails with a connection or cert error — wait and retry,
exactly as `docs/VPS_DEPLOY.md` §3 notes for the same situation.

## 22. API health test

```bash
curl https://tradingbotapi.bhardwajvaibhav.com/health
```

## 23. Authentication test

```bash
# No token -> 401
curl -i https://tradingbotapi.bhardwajvaibhav.com/account
# Wrong token -> 401
curl -i -H "Authorization: Bearer wrong-value" https://tradingbotapi.bhardwajvaibhav.com/account
# Real token -> 200
curl -i -H "Authorization: Bearer YOUR_API_TOKEN" https://tradingbotapi.bhardwajvaibhav.com/account
```

## 24. Frontend deployment

Push `frontend/` to Netlify (via the GitHub repo — Netlify builds directly from the pushed
branch, no separate upload step). See §25 for the exact site settings; `docs/API.md` §8 has
the same table with the reasoning behind each value.

## 25. Netlify configuration

| Setting | Value |
|---|---|
| Repository | `YOUR_GITHUB_REPO` |
| Base directory | `frontend` |
| Build command | `sh netlify-build.sh` |
| Publish directory | `frontend` (or `.` if base directory is already set to `frontend`) |
| Environment variables | `DASHBOARD_API_BASE_URL=https://tradingbotapi.bhardwajvaibhav.com`, `DASHBOARD_API_TOKEN=YOUR_API_TOKEN` (same value as `deploy/api.env` on the VPS) |

`frontend/netlify-build.sh` generates `config.js` from those two environment variables at
deploy time — `config.js` itself is gitignored and never committed (it would otherwise carry
the token). Local development still uses the `cp config.example.js config.js` flow
(`docs/API.md` §2), untouched by any of this.

## 26. CORS configuration

Once you know your Netlify site's actual URL (its `*.netlify.app` default, or a custom
domain once attached), set it as `DASHBOARD_CORS_ORIGINS` in `deploy/api.env` on the VPS
(§12), comma-separated if there's more than one origin (e.g. a `*.netlify.app` preview URL
and a custom domain both need to be listed):

```bash
nano /root/trading-agent/deploy/api.env
# DASHBOARD_CORS_ORIGINS=https://your-dashboard-site.netlify.app
sudo systemctl restart trading-api
```

An origin **not** on this list gets no `Access-Control-Allow-Origin` header at all — the
dashboard will look "stuck" with a connection-error banner, not a clear CORS error, since
browsers don't surface CORS failures as readable page content. If the dashboard can't reach
the API after deploying, check this first.

## 27. Dashboard/API connectivity test

Open the deployed Netlify URL in a browser. Expect: the Overview tab loads with real account
data within one poll interval (`pollIntervalMs`, default 15s), no persistent red connection
banner. If the banner says "Cannot reach the API" — recheck §20/§21. If it says
"Unauthorized" — recheck that `DASHBOARD_API_TOKEN` matches on both sides (§12 and §25). If
data loads but the banner still shows a stale-data warning intermittently — check
`docs/API.md`'s per-endpoint notes; an individual endpoint erroring is expected to degrade
gracefully, not indicate an outage.

## 28. Operational safety

Restated plainly, because a deployment step is exactly where it's easiest to lose track of
this:

- **The API is read-only.** No endpoint anywhere in `api/` writes, places, modifies, or
  cancels anything — `tests/test_api.py` section D proves this empirically (hashes every
  real data file before and after hitting every endpoint) and section I proves every
  non-GET method returns 404/405, never 200. `deploy/trading-api.service`'s own
  `ProtectSystem=strict` + narrow `ReadWritePaths=` enforce the same boundary at the OS
  level, independent of the application code.
- **The dashboard cannot execute trades.** There is no order-entry control anywhere in
  `frontend/` — `docs/API.md`'s "STEP 6" requirements and this repo's own review of
  `frontend/js/views.js` confirm no such control was ever built.
- **This deployment does not activate live trading.** `trading-api.service` is a new,
  independent systemd unit — it does not touch `run_cycle.sh`, its cron entries, or any file
  `CLAUDE.md`'s table marks as agent-editable or protected. Nothing about this deployment
  changes what the trading agent does on its existing schedule.
- **The current live engine remains unchanged.** `engine/guardrails.py`, `engine/execute.py`,
  `research/store.py`, `research/contracts.py`, `research/experiments/runner.py`,
  `strategies/core.py`, `strategies/registry.py` were not modified anywhere in this
  deployment-prep pass — see §29 "protected files touched" in the final report for the
  explicit confirmation.
- **An API outage cannot place orders.** There is no path from "the dashboard API is down"
  to "an order gets placed" — the API has no dependency in that direction at all; it only
  ever reads state the trading engine already wrote.
- **A dashboard outage does not stop the trading engine.** `run_cycle.sh`'s cron-driven
  cycles have no dependency on `trading-api.service`, Caddy, or the Netlify site being up —
  stopping any or all of them changes nothing about whether the next scheduled trading cycle
  runs.
- **A `trading-api.service` restart cannot enable new trading functionality.** The unit's
  `ExecStart` is a fixed gunicorn invocation of `api.wsgi:app` — restarting it re-runs the
  same fixed, read-only route table; it cannot pick up new capabilities without a code change
  and a fresh review, exactly like any other restart of any other read-only service.

## 29. Rollback procedure

```bash
sudo systemctl stop trading-api
cd /root/trading-agent
git log --oneline -5                 # find the last known-good commit
git checkout <previous-commit-sha>
sudo systemctl start trading-api
sudo systemctl status trading-api
```

Rolling back the API/frontend has **no effect on the trading engine** (§28) — this is
strictly a rollback of the read-only dashboard, never of `engine/`. For a Netlify rollback,
use Netlify's own "Deploys" tab -> pick a previous deploy -> "Publish deploy" (no VPS/API
action needed, they're independent).

## 30. Update procedure

Normal path — see §31 "GitHub deployment model" below for the full picture:

```bash
# 1. Locally, after review:
git status
git add <files>
git commit -m "..."
git push origin main

# 2. On the VPS:
cd /root/trading-agent
sudo systemctl stop trading-api
git pull origin main
venv/bin/pip install -r requirements.txt   # only if requirements.txt changed
sudo systemctl start trading-api
sudo systemctl status trading-api
curl https://tradingbotapi.bhardwajvaibhav.com/health

# 3. Netlify redeploys automatically on push (if connected to this repo's
#    branch) — or trigger manually from the Netlify dashboard.
```

Trading-engine files (`engine/`, `run_cycle.sh`, cron) are never part of this update
loop unless you're deliberately deploying a change to them, per the existing
`docs/VPS_DEPLOY.md` / `docs/RESEARCH_DEPLOY.md` procedures — this loop is scoped to the
dashboard API/frontend.

## 31. GitHub deployment model

```text
you, locally
    |
    v
git status              # see what changed
    |
    v
git add <files>          # stage deliberately, not `-a`/`-A` by habit —
    |                     # this matters here specifically because
    |                     # memory/state.json and friends must NEVER be
    |                     # staged (see .gitignore; they're excluded, but a
    |                     # forced `git add -f` could still override that)
    v
git commit -m "..."
    |
    v
git push origin main
    |
    v
GitHub (source of truth)
    |
    v
VPS: git pull origin main   # controlled, manual, reviewed before you run it
    |                       # (§30's update procedure)
    v
systemctl restart trading-api
```

No CI/CD is set up for this first deployment, deliberately (the task's own instruction: "for
this first deployment, do not implement automatic CI/CD unless it can be done without
unnecessary complexity — a simple controlled VPS update procedure is preferable"). A manual
`git pull` + `systemctl restart` on the VPS, run by you after reviewing what changed, is the
whole update loop. Netlify auto-deploys on push to the connected branch (that's Netlify's own
standard behavior, not custom CI/CD built for this).

## 32. Troubleshooting

| Symptom | Likely cause | Check |
|---|---|---|
| `systemctl status trading-api` shows `failed` | gunicorn crashed on import, or the venv path is wrong | `journalctl -u trading-api -n 50 --no-pager`; confirm `/root/trading-agent/venv/bin/gunicorn` exists |
| `curl http://127.0.0.1:8787/health` fails locally on the VPS | service not running, or wrong port | `systemctl status trading-api`; confirm `deploy/api.env`'s `DASHBOARD_API_PORT` matches the systemd unit and Caddyfile |
| `curl https://tradingbotapi.bhardwajvaibhav.com/health` fails but the 127.0.0.1 curl above works | Caddy/DNS/firewall, not the API itself | §20/§21; `sudo systemctl status caddy`; `sudo journalctl -u caddy -n 50` |
| Every request returns 401 even with the right-looking token | Trailing whitespace/newline in `deploy/api.env`, or the Netlify env var doesn't match | `cat -A deploy/api.env` to spot stray characters; re-diff both sides byte-for-byte |
| Dashboard shows a permanent "Cannot reach the API" banner | CORS, not connectivity — the browser silently drops the response | Browser devtools Network tab: a CORS failure shows the request completing with no `Access-Control-Allow-Origin` response header; fix §26 |
| `sudo -u tradingapi cat .env` unexpectedly succeeds | Filesystem permissions in §13 weren't applied, or were applied before `.env` existed | Re-run §13's re-lock commands after confirming `.env` exists |
| `research/market_memory.db` writes fail (`/backtests`, `/research/*` endpoints 503) | `tradingapi` lacks write access to `research/` | Re-run §13's `chmod -R g+w research/` line; confirm `ReadWritePaths=` in the systemd unit matches |
| Netlify build fails with "DASHBOARD_API_BASE_URL is not set" | Environment variables not set in Netlify site settings | §25 — set both `DASHBOARD_API_BASE_URL` and `DASHBOARD_API_TOKEN` in Netlify's UI, not just locally |
| `git pull` on the VPS fails with local changes | Someone edited a file directly on the VPS (e.g. `deploy/api.env` — which is fine, it's gitignored) but git still sees a *tracked* file changed | `git status` to see which tracked file changed and why; `git stash` only if you're sure, never `git checkout -- .` without checking first |

---

Everything in this document is preparation. Deploying it — running these commands against a
real VPS, a real GitHub remote, and a real Netlify project — is a manual step for you.
