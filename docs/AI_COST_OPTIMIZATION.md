# AI cost optimization

Production AI calls stopped on 17 September, so current calls/day and cost/day are zero. Token
cost cannot be reconstructed for the legacy Claude CLI because it did not report usage. This is
reported as unknown, not estimated money.

| Workflow | Current calls/day | Model role | Cacheability | Safe optimization |
|---|---:|---|---|---|
| Classification | 0 | economical | high | deterministic filters first; `gpt-5.6-luna` only for ambiguity |
| News extraction | 0 | economical | high by source hash | store structured extraction and deduplicate URLs |
| Observation summary | 0 | economical | high by snapshot | pass deltas and artifact references, not complete history |
| Hypothesis generation | 0 | capable reasoning | medium | `gpt-5.6-terra`, bounded digest and JSON output |
| Experiment design | 0 | capable reasoning | medium | reuse locked hypothesis artifact rather than replaying logs |
| Evidence critique | 0 | strong reasoning | low | invoke only after deterministic evaluation is complete |
| Strategy review | 0 | strong reasoning | low | invoke only when evidence passes promotion rules |
| Daily digest | 0 AI calls | deterministic | complete | format directly from telemetry |

The gateway now uses the Responses API, caps output tokens, records provider/model/prompt version,
and centralizes role mapping. Official OpenAI guidance recommends the Responses API for reasoning
models and reducing requests, input tokens, and model size where quality allows. Pricing is not
hard-coded: deployment access and prices can vary, so monetary cost remains `unknown` until a
versioned price table and real API usage exist.
