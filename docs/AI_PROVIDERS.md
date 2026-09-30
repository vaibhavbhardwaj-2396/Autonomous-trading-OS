# AI Gateway and provider operations

Living Quant research asks for a capability, never a brand/model name. The
durable, secret-free configuration is `control/ai_config.json`; credentials
remain server-side and are never returned by the API.

## Providers

| Provider ID | Protocol | Authentication | Required environment |
|---|---|---|---|
| `openai` | OpenAI Responses API | `API_KEY` | `OPENAI_API_KEY` |
| `anthropic` | Anthropic Messages API | `API_KEY` | `ANTHROPIC_API_KEY` |
| `openai_compatible` | OpenAI-compatible chat completions | `API_KEY` | `OPENAI_COMPATIBLE_BASE_URL`, `OPENAI_COMPATIBLE_API_KEY` |
| `local_openai` | OpenAI-compatible chat completions | `LOCAL_NO_AUTH` | `LOCAL_AI_BASE_URL` restricted to loopback |

Consumer ChatGPT/Claude browser sessions, browser cookies and subscription
entitlements are not supported authentication mechanisms. The old interactive
Claude CLI is not a production adapter.

## Routing

Roles map to `ECONOMY`, `BALANCED`, `REASONING`, or `STRONG_REASONING` (plus
optional specialized capabilities). Each capability has a primary and an
optional fallback provider/model. Fallback occurs only for timeout, rate limit,
provider-unavailable, or model-unavailable errors. Authentication, validation,
and malformed-response failures do not fall back.

Admin → AI can change routes without deployment, inspect the secret-free
provider registry, and run a minimal structured provider test. Every real call
records role, capability, provider, model, prompt version, bounded prompt and
response, reported tokens, latency, downstream-validation state, and fallback
reason. Monetary cost remains `unknown` until a versioned price table and real
reported usage exist.

When no route has a usable credential, the honest state is
`NO_PROVIDER_CONFIGURED`. Deterministic ingestion, experiment execution,
evidence evaluation, Strategy Factory and paper operation remain independent.

## First production proof

Install supported credentials in the dedicated least-privilege environment:

```bash
cd /root/trading-agent
cp deploy/ai.env.example deploy/ai.env
nano deploy/ai.env
chown root:tradingapi deploy/ai.env
chmod 640 deploy/ai.env
systemctl daemon-reload
systemctl restart trading-api.service
```

The root research worker and the restricted API provider test both load this
file. Do not put broker, Telegram, dashboard, TOTP or MPIN secrets in it. The
root `.env` remains supported for root-run workers, but the API intentionally
cannot read that broader secret file.

Then:

1. Unlock Admin and save the relevant capability route.
2. Use **Test provider** once; confirm success, latency, and reported tokens.
3. Resume `RESEARCH_AI` only after the bounded test succeeds.
4. Confirm the first legitimate trace in Artifacts and `/ai/status`.

The Admin screen refreshes automatically when opened and after completed
actions, but not on the background polling timer while it is open. This keeps
unsaved model/reason fields intact. Use the top-right **Refresh** button for an
explicit status refresh.

No credential is committed, sent to the browser, or accepted by the routing
configuration endpoint.

## Bounded provider benchmark

After at least one significant ResearchPacket exists, an operator can compare
up to four configured targets over at most five historical packets:

```bash
venv/bin/python scripts/benchmark_ai_providers.py \
  --target openai:MODEL --target anthropic:MODEL --cases 3
```

The benchmark scores schema validity, specificity, falsifiability, experiment
constructibility, critic usefulness, duplicate rate, latency, tokens and cost
when known. It records normal model-interaction traces, consumes the normal AI
budget, never uses paper/live P&L as the quality score, and never changes a
route automatically. An operator reviews the evidence before changing routing.
