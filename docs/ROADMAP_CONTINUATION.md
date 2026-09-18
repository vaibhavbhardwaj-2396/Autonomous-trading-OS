# Scheduled-task prompt

Continue the approved Living Quant software implementation roadmap in this
project. Read AGENTS.md and its required memory files, then
docs/IMPLEMENTATION_ROADMAP.md and docs/DELIVERY_WORKFLOW.md. Missing trading
state blocks trading but does not block isolated software engineering.

Inspect git status, recent commits and outstanding CI/deployment state first.
Resume incomplete work before starting a new increment. Preserve unrelated
changes; never stage the user's untracked AGENTS.md. If another run owns work
on this checkout, defer rather than running concurrently.

Choose the next bounded, useful increment, implement it, review the diff and
run relevant isolated tests. Commit only task files, push to the authorized
repository, and wait for green CI on the exact commit before deployment.
Follow the deployment script's market-window and clean-worktree checks. Restart
the dashboard API when its code changes and verify the serving endpoint.
Verify frontend publication separately. Never claim that source copied to the
VPS is necessarily running in an existing process or hosted frontend.

Continue to the next useful increment while the run can make progress. Update
the roadmap with concrete evidence, remaining work and any blocker before
ending. Report commits, CI, deployment checks and the next action. Do not
repeat work marked complete without evidence it needs correction.

Routine engineering, GitHub changes and VPS deployment are authorized. Honor
tool approvals and sandbox controls. Do not change protected guardrails,
capital or broker credentials, resume trading, place orders, or enable live
strategy execution. Live readiness remains human-gated. Never work around an
approval rejection or fabricate completion when a tool is unavailable.
