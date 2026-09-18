# GitHub and VPS delivery workflow

## Boundaries

This workflow delivers code only. It never submits an order, changes broker
credentials, changes `memory/state.json`, resumes trading, or weakens
`memory/guardrails.md`. Those controls continue to govern live execution.

## GitHub

1. Make a focused branch for each change.
2. Run the relevant local test modules.
3. Push the branch and open a pull request.
4. GitHub Actions runs every executable `tests/test_*.py` module on Python 3.11.
5. Merge only after CI succeeds. `main` is the only deployable line.

The CI workflow intentionally runs test files as scripts rather than using
`unittest discover`: the repository's test modules call `sys.exit()` after
their own test harness completes, which makes discovery treat successful
modules as import errors.

## VPS access (one-time)

The deployer uses the SSH alias `trading-os-vps`. Create it in the machine
running deployments after installing a dedicated public key on the VPS:

```sshconfig
Host trading-os-vps
    HostName tradingagent.bhardwajvaibhav.com
    User root
    IdentityFile ~/.ssh/autonomous_trading_os_vps
    IdentitiesOnly yes
```

On the VPS, append the corresponding `.pub` key to `/root/.ssh/authorized_keys`
with permissions `700` on `/root/.ssh` and `600` on `authorized_keys`. Use a
dedicated key: do not copy broker, GitHub, or dashboard credentials into SSH
configuration.

## Deploying a merged commit

From a clean local checkout, after GitHub CI is green:

```bash
scripts/deploy_vps.sh --ref origin/main
```

The script refuses to deploy during NSE market hours (weekdays, 09:15–15:45
IST), unless an operator explicitly supplies `--allow-market-window` for an
assessed urgent fix. It also refuses a VPS with tracked local modifications,
checks that the exact SHA is on `origin/main`, runs guardrail/API tests on the
VPS, and returns to the prior SHA if either test fails.

For a dashboard/API change:

```bash
scripts/deploy_vps.sh --ref origin/main --restart-api
```

`--restart-api` restarts only `trading-api.service`; it has no order-placement
or trading-cycle effect. Use `--install-deps` only when `requirements.txt` has
changed.

## Rollback

The script rolls back automatically if its VPS test gate fails. For a verified
post-deploy regression, deploy the previously known-good merged SHA:

```bash
scripts/deploy_vps.sh --ref <known-good-commit-sha> --restart-api
```

The SHA must still be reachable from `origin/main`; this prevents deploying a
local or unreviewed commit by accident.
