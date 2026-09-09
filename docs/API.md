# Dashboard API (v1, read-only)

Covers: running `api/` (the HTTP API) and `frontend/` (the dashboard) locally, how
authentication and CORS are configured, the full endpoint list, what this API does and does
not protect against, and how the pieces are meant to separate later onto a VPS + Netlify.
**Nothing in this document has been deployed** — see "Future deployment" at the end.

**v1 is read-only, structurally.** There is no endpoint anywhere in `api/` that writes,
places an order, modifies risk, locks or approves a research Contract, promotes a
StrategyVersion, or touches broker/engine state. `api/data.py`'s module docstring lists
every write-capable function it deliberately never imports; `tests/test_api.py` section D
proves this empirically by hashing every real data file/directory before and after hitting
every endpoint, and section I proves POST/PUT/PATCH/DELETE against every route return 404
or 405, never 200.

## 1. Running the API locally

```bash
cd trading-agent
pip install -r requirements.txt   # flask + python-dotenv are already in it
cp .env.example .env
```

Edit `.env` and set at minimum:

```bash
DASHBOARD_API_TOKEN=some-long-random-string
DASHBOARD_CORS_ORIGINS=http://localhost:5173
```

Then:

```bash
python -m api.app
```

This binds to `127.0.0.1:8787` by default (see `DASHBOARD_API_HOST` / `DASHBOARD_API_PORT`
in `.env.example` to change it) and logs a warning on startup if `DASHBOARD_API_TOKEN` is
unset — every protected endpoint returns 401 until it's set, there is no "auth disabled"
mode.

**`python -m api.app` is local development only.** It runs Flask's own development server
(its startup banner says so, every time) — never run it as the production process. The VPS
runs `api/wsgi.py` under gunicorn instead, launched by systemd; see §7 below and
`docs/DEPLOYMENT.md` for the full production setup. You can exercise the exact same
production entrypoint locally too, if you want to test it before deploying:

```bash
gunicorn --bind 127.0.0.1:8787 --workers 2 api.wsgi:app
```

Quick check:

```bash
curl http://127.0.0.1:8787/health
curl -H "Authorization: Bearer some-long-random-string" http://127.0.0.1:8787/account
```

## 2. Running the dashboard locally

The frontend is plain HTML/CSS/JS — no build step, no framework, no npm dependency at all
(see "Why no build tool" below).

```bash
cd trading-agent/frontend
cp config.example.js config.js   # gitignored — edit it, don't commit it
```

Edit `config.js`:

```js
window.DASHBOARD_CONFIG = {
  apiBaseUrl: "http://127.0.0.1:8787",   // must match the API's host/port above
  apiToken: "some-long-random-string",   // must match DASHBOARD_API_TOKEN above
  pollIntervalMs: 15000,
};
```

Then serve the directory with any static file server — opening `index.html` directly as a
`file://` URL will not work (the browser blocks `fetch()` from a `file://` origin to
`http://`). The simplest option needs nothing extra:

```bash
python3 -m http.server 5173
```

Open `http://localhost:5173/index.html`. The dashboard polls the API every
`pollIntervalMs` (default 15s) and shows a banner if the API is unreachable, the token is
wrong, or an individual endpoint fails — it never crashes outright, and it never assumes
`localhost` in its own code (only in `config.js`, which is exactly what's meant to change
per deployment).

### Why no build tool

The dashboard is four tabs of tables and cards polling a dozen GET endpoints — no routing
library, no state management, no bundler-dependent feature is needed. `frontend/js/api.js`
is the only file that calls `fetch()`; `frontend/js/views.js` renders each tab from what
that returns; `frontend/js/app.js` just does tab switching and the poll loop. Zero npm
dependencies means zero install step, zero `node_modules`, and a Netlify deploy with no
build command at all — the smallest footprint that satisfies "runs locally" and
"independently deployable."

### Frontend tests

The pure logic worth testing outside a browser — `js/format.js`'s rendering helpers and
`js/api.js`'s response classification (network failure / 401 / other non-2xx / malformed
JSON / success) — has a small Node-only test file, no test runner or npm dependency added:

```bash
node frontend/tests/test_frontend.mjs
```

`js/views.js`/`js/app.js` are DOM-orchestration glue over those two files (tab switching,
polling, wiring fetched data into `<table>` markup) and are covered by manual browser
verification instead (Chromium via Playwright during this slice's own build, and ad hoc
against a running API + `python3 -m http.server` — see sections 1-2 above) rather than a
headless-DOM test harness, which would be the first real dependency this frontend picks up
for a four-tab dashboard.

## 3. Authentication

A single shared bearer token, not a login system — this is a single-operator tool, and
`api/auth.py`'s docstring explains the reasoning in full. Every protected endpoint requires:

```
Authorization: Bearer <DASHBOARD_API_TOKEN>
```

- Missing or wrong token → `401 {"error": "unauthorized", ...}`. The response never echoes
  back what token was expected.
- `DASHBOARD_API_TOKEN` unset on the server → every protected request is refused, always —
  there is no "auth disabled" fallback.
- Token comparison uses `hmac.compare_digest` (constant-time), so a network observer can't
  learn it a character at a time from response timing.
- The token is never written to a log line, an error message, or a response body.
- `/health` is the one public endpoint (see `api/app.py`'s docstring for why: it must not
  imply the broker or research factory are healthy, only that the API process is up).

## 4. CORS

`DASHBOARD_CORS_ORIGINS` is a comma-separated allow-list, e.g.:

```
DASHBOARD_CORS_ORIGINS=http://localhost:5173,https://tradingbot.bhardwajvaibhav.com
```

An origin **not** on the list gets no `Access-Control-Allow-Origin` header at all — which
every browser treats as blocked. There is no wildcard (`*`) mode, ever, by design.

## 5. Endpoints

All protected endpoints require the bearer token above. All are `GET` only.

| Endpoint | Reads through | Notes |
|---|---|---|
| `GET /health` | — (public) | API liveness only, not broker/research health |
| `GET /account` | `engine.guardrails.load_state()` | cash, portfolio_value, total_value, pnl_today |
| `GET /positions` | `engine.guardrails.load_state()` | open positions; no live mark price (see below) |
| `GET /orders` | `engine.journal.TRADES_JSONL` | ENTRY + REJECTED journal rows, `?limit=` |
| `GET /trades` | `engine.journal.TRADES_JSONL` | EXIT journal rows, `?limit=` |
| `GET /risk` | `engine.guardrails.status_summary()` | the exact snapshot the agent itself checks before trading |
| `GET /regime` | `engine.journal.REGIME_JSONL` | the **last logged** classification — never a live reclassification |
| `GET /research/drafts` | `research.brain.draft_backlog.build_backlog()` | pending-review DRAFT Contracts |
| `GET /research/evidence` | `research.brain.digest.build_digest()`'s evidence section | PROMISING/WEAK/INCONCLUSIVE/CONTRADICTED verdicts |
| `GET /research/areas` | `research.brain.research_areas.groups_as_dicts()` | hypothesis-to-area tags |
| `GET /strategies` | `strategies.registry.list_versions()` | registered StrategyVersions, `?limit=` |
| `GET /backtests` | `research.memory`'s research-note log | backtest **completion notes** only, see below |

Every response is deterministic JSON built from already-authoritative state; nothing here
computes a statistic, a risk figure, or a verdict that doesn't already exist somewhere else
in the codebase — see each function's docstring in `api/data.py`.

### Known v1 limitations (by design, not oversights)

- **`/positions` has no `current_price`/`unrealized_pnl`.** The agent's own state doesn't
  track a live mark price, and fetching one on every dashboard poll would mean this
  read-only API calling a live market-data feed — out of scope for v1.
- **`/regime` is the last logged classification, not a live one.** A live reclassification
  calls `engine.regime.classify_thresholds()`, which fetches from yfinance — exactly the
  "expensive work merely because requested" this API must not do.
- **`/backtests` only has completion-note summaries** (strategy_id, version_id, n_trades, a
  one-line note) — `research/experiments/strategy_backtest.py` doesn't persist full
  signal/trade/stat detail anywhere yet (see that module's own "Evidence integration"
  docstring section), so there's nothing richer to serve here yet.

## 6. Security assumptions

Be plain-spoken about what this actually protects, since it's easy to over-trust a bearer
token:

- This is a **single-operator, low-sensitivity read surface**, not a hardened multi-tenant
  API. The threat model is "keep casual/automated access out," not "resist a targeted
  attacker with the token."
- The frontend embeds the token in `config.js`, which ships to and runs in the browser.
  **Anyone who can view that page's JS can read the token.** Treat the dashboard's URL, once
  deployed, as roughly equivalent to the token itself — don't post it publicly, and rotate
  the token (`DASHBOARD_API_TOKEN`) if you ever suspect it leaked.
- There is deliberately no write path for a leaked token to abuse — the worst a leaked token
  exposes is read access to the same account/position/research summaries described above,
  never an ability to trade, close a position, or touch research state.
- CORS is an allow-list, never a wildcard (see above). The API never leaks an internal
  exception message, stack trace, or filesystem path in a response body — `api/app.py`'s
  error handlers return generic messages and log the real detail server-side only
  (`tests/test_api.py` section H proves this for both a forced 500 and a forced 503).
- Errors always use HTTP status honestly: `401` for auth, `503` when an underlying data
  source (a state file, the research store) is unavailable, `500` only for a genuinely
  unexpected error — never a bare `200` with an error buried in the body.

## 7. Production deployment

Deployment **configuration has been prepared** (systemd unit, Caddy template, gunicorn
entrypoint, Netlify build script) but **nothing has actually been deployed** — no VPS
changes, no DNS record, no Netlify site. The full step-by-step procedure lives in
**`docs/DEPLOYMENT.md`**; this section is the short version.

- **API**: gunicorn (`api/wsgi.py`) behind Caddy on the VPS, run by systemd
  (`deploy/trading-api.service`) as its own unprivileged `tradingapi` user — never
  `python -m api.app`'s dev server, and never exposed directly (gunicorn binds
  `127.0.0.1` only; Caddy, `deploy/Caddyfile.tradingbotapi`, is the sole public entry point
  at `https://tradingbotapi.bhardwajvaibhav.com`). Same HTTPS-via-Caddy pattern
  `docs/VPS_DEPLOY.md` already uses for the Kite callback server.
- **Frontend**: a static Netlify deploy of `frontend/` — still no bundler (see "Why no build
  tool" above); the one build command Netlify runs (`sh netlify-build.sh`) only generates
  `config.js` from two Netlify environment variables at deploy time, since `config.js` itself
  is gitignored (it carries the API token). See `docs/DEPLOYMENT.md` step 25 for exact
  Netlify site settings.
- Both are reachable independently once deployed; the frontend's only coupling to the API is
  still the one `apiBaseUrl` value in `config.js`, exactly as designed — Netlify's build just
  changes how that file gets written, not what it contains or how the app reads it.

## 8. Netlify preparation

Not yet created — this documents exactly what the Netlify project will need, verified
against the actual repository (there is no build system to invent settings for):

| Setting | Value |
|---|---|
| Repository | this repo |
| Base directory | `frontend` |
| Build command | `sh netlify-build.sh` |
| Publish directory | `frontend` (or `.` if base directory is already `frontend`) |
| Environment variables | `DASHBOARD_API_BASE_URL` = `https://tradingbotapi.bhardwajvaibhav.com`; `DASHBOARD_API_TOKEN` = the same value as the VPS's `deploy/api.env` |
| SPA routing | Not applicable — `frontend/index.html` is one static page with JS-toggled tab sections, no client-side router, no deep-linkable routes to redirect |
| API base URL | Configured via the two environment variables above, consumed by `netlify-build.sh` — never hardcoded in `frontend/js/*.js` |
| Future custom domain | Out of scope for this repo: the eventual public URL `https://bhardwajvaibhav.com/lab/autonomous-trading-agent` is a *path* on the existing portfolio site (`bhardwajvaibhav.com`, a separate project — see that project's own repo), not a Netlify custom (sub)domain on *this* site. Making that path serve this dashboard requires a change in the portfolio site (a proxy/rewrite rule, or embedding), which this repo cannot configure. Until that's done, this site's own Netlify subdomain (e.g. `something.netlify.app`) is the working URL. |

`DASHBOARD_CORS_ORIGINS` on the API (`deploy/api.env`, not Netlify) must include whichever
of those URLs the dashboard is actually served from, or the browser will block every request
with no CORS header — see `docs/API.md` §4.
