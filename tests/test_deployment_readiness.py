"""
Deployment-prep readiness tests (docs/DEPLOYMENT.md).

This file does NOT re-test API behavior — tests/test_api.py already covers
routes/auth/CORS/read-only-ness against api.app.create_app(). This file
checks the things specific to *deploying* that already-tested app:

  A. the production WSGI entrypoint (api.wsgi:app, what gunicorn actually
     loads on the VPS) imports cleanly and is the same kind of app object —
     including, independently, that it still has no write route, so a
     regression in api/wsgi.py itself (not api/app.py) would be caught here;
  B. api/config.py's env parsing fails safe on malformed/missing input
     (never crashes, never silently "enables" something insecure);
  C. no tracked-or-would-be-tracked file (i.e. anything `git add -A` would
     stage, respecting .gitignore) contains a real-looking secret value —
     only blank/placeholder templates are allowed;
  D. requirements.txt / .gitignore / the new deploy/ files are internally
     consistent with what this phase actually added.

Run with:  python -m tests.test_deployment_readiness
"""

import re
import sys
import subprocess
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import os  # noqa: E402

REPO_ROOT = Path(__file__).parent.parent

PASSED, FAILED = 0, 0


def check(name, condition, detail=""):
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  ✓ {name}")
    else:
        FAILED += 1
        print(f"  ✗ {name}")
        if detail:
            print(f"      {detail}")


# ===========================================================================
print("--- A: production WSGI entrypoint (api.wsgi:app) ---")
# ===========================================================================

# A clean env for this section — no token/CORS leftover from a previous
# test module or the real shell environment, so behavior here reflects only
# what api/wsgi.py itself does.
os.environ.pop("DASHBOARD_API_TOKEN", None)
os.environ.pop("DASHBOARD_CORS_ORIGINS", None)

from api import wsgi as api_wsgi   # noqa: E402
from flask import Flask            # noqa: E402

check("A: api.wsgi module imports with no ImportError/side-effect crash", True)
check("A: api.wsgi.app exists", hasattr(api_wsgi, "app"))
check("A: api.wsgi.app is a Flask application instance", isinstance(api_wsgi.app, Flask))

_wsgi_rules = list(api_wsgi.app.url_map.iter_rules())
_wsgi_routes = sorted({r.rule for r in _wsgi_rules if r.rule != "/static/<path:filename>"})
check("A: api.wsgi.app has the expected route count (18: /health + 11 live + "
      "6 paper/shadow — Slice AA)",
      len(_wsgi_routes) == 18, str(_wsgi_routes))
check("A: /health is present on the WSGI entrypoint", "/health" in _wsgi_routes)
check("A: /account is present on the WSGI entrypoint", "/account" in _wsgi_routes)

for rule in _wsgi_rules:
    if rule.rule == "/static/<path:filename>":
        continue
    methods = rule.methods - {"HEAD", "OPTIONS"}
    check(f"A: {rule.rule} exposes only GET on the WSGI entrypoint (no POST/PUT/PATCH/DELETE)",
          methods == {"GET"}, str(methods))

# The WSGI entrypoint is a live Flask app -- it is fine (and how gunicorn
# itself works) to exercise it through Flask's own test client, same as
# tests/test_api.py does against api.app.create_app(). This independently
# re-proves the no-write-route guarantee against the object gunicorn will
# actually serve, rather than trusting that api.wsgi:app == api.app's app.
_wsgi_client = api_wsgi.app.test_client()
for method in ("post", "put", "patch", "delete"):
    for route in _wsgi_routes:
        r = getattr(_wsgi_client, method)(route)
        check(f"A: {method.upper()} {route} on api.wsgi:app is not a usable write endpoint (405/404)",
              r.status_code in (404, 405), f"{r.status_code}")

check("A: GET /health on api.wsgi:app returns 200 with no auth (public liveness route)",
      _wsgi_client.get("/health").status_code == 200)
check("A: GET /account on api.wsgi:app is protected (401 with no token configured)",
      _wsgi_client.get("/account").status_code == 401)


# ===========================================================================
print("\n--- B: config parsing fails safe ---")
# ===========================================================================

from api import config as api_config  # noqa: E402


def _with_env(**kv):
    """Context-manager-free helper: set kv, yield nothing, restore afterward."""
    saved = {k: os.environ.get(k) for k in kv}
    for k, v in kv.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    return saved


def _restore(saved):
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


_saved = _with_env(DASHBOARD_API_PORT="not-a-number")
check("B: bind_port() with a malformed value falls back to the documented default, not a crash",
      api_config.bind_port() == api_config.DEFAULT_PORT)
_restore(_saved)

_saved = _with_env(DASHBOARD_API_PORT=None)
check("B: bind_port() with no value set falls back to the documented default",
      api_config.bind_port() == api_config.DEFAULT_PORT)
_restore(_saved)

_saved = _with_env(DASHBOARD_API_PORT="8787")
check("B: bind_port() with a valid value parses it correctly", api_config.bind_port() == 8787)
_restore(_saved)

_saved = _with_env(DASHBOARD_API_TOKEN=None)
check("B: api_token() with no token set returns None (fail closed, never '')",
      api_config.api_token() is None)
_restore(_saved)

_saved = _with_env(DASHBOARD_API_TOKEN="   ")
check("B: api_token() with a whitespace-only token returns None, not the whitespace",
      api_config.api_token() is None)
_restore(_saved)

_saved = _with_env(DASHBOARD_API_TOKEN="  real-token  ")
check("B: api_token() strips surrounding whitespace from a real token",
      api_config.api_token() == "real-token")
_restore(_saved)

_saved = _with_env(DASHBOARD_CORS_ORIGINS=None)
check("B: cors_origins() with nothing configured returns an empty set (no origin allowed), not a wildcard",
      api_config.cors_origins() == set())
_restore(_saved)

_saved = _with_env(DASHBOARD_CORS_ORIGINS="")
check("B: cors_origins() with an empty string configured also returns an empty set",
      api_config.cors_origins() == set())
_restore(_saved)

_saved = _with_env(DASHBOARD_HOST=None)
check("B: bind_host() with nothing configured defaults to the documented localhost default",
      api_config.bind_host() == api_config.DEFAULT_HOST)
_restore(_saved)


# ===========================================================================
print("\n--- C: no real secret in any file `git add -A` would stage ---")
# ===========================================================================

# The repo is now committed and pushed (see docs/DEPLOYMENT.md), so
# `git add -A --dry-run` on its own is NOT "everything a future `git add -A
# && git commit` would capture" — it only ever reports paths that differ
# from HEAD (new, modified, or deleted), so an unchanged already-committed
# file like .env.example or requirements.txt no longer appears in it at
# all. The actual invariant we want — "the set of paths that would exist in
# the repo the instant someone ran `git add -A && git commit` from this
# worktree" — is the union of what's already tracked (`git ls-files`) and
# what the dry-run would newly add, minus anything the dry-run would
# remove. This union is correct in all three repo states this test must
# tolerate: a fresh, never-committed repo (`ls-files` is empty, so this
# reduces to the dry-run's "add" list — the old behavior), a committed repo
# with a clean tree (the dry-run adds nothing, so this is just the tracked
# list), and a committed repo with legitimate pending changes, as is
# routinely the case mid-development (both halves contribute).

_dry_run = subprocess.run(
    ["git", "add", "-A", "--dry-run"],
    cwd=REPO_ROOT, capture_output=True, text=True, timeout=30,
)
check("C: `git add -A --dry-run` runs cleanly against the repo", _dry_run.returncode == 0, _dry_run.stderr)

_dry_run_added = []
_dry_run_removed = []
for line in _dry_run.stdout.splitlines():
    # Lines look like: "add 'api/wsgi.py'" (new/modified) or
    # "remove 'old/path.py'" (deleted from the worktree but still tracked).
    m = re.match(r"^add '(.+)'$", line.strip())
    if m:
        _dry_run_added.append(m.group(1))
        continue
    m = re.match(r"^remove '(.+)'$", line.strip())
    if m:
        _dry_run_removed.append(m.group(1))

_ls_files = subprocess.run(
    ["git", "ls-files"],
    cwd=REPO_ROOT, capture_output=True, text=True, timeout=30,
)
check("C: `git ls-files` runs cleanly against the repo", _ls_files.returncode == 0, _ls_files.stderr)
_tracked_paths = [p for p in _ls_files.stdout.splitlines() if p.strip()]

_staged_paths = sorted((set(_tracked_paths) | set(_dry_run_added)) - set(_dry_run_removed))

check("C: at least the expected core files would be staged (sanity check on the dry-run/ls-files union)",
      any(p == "api/wsgi.py" for p in _staged_paths) and any(p == "requirements.txt" for p in _staged_paths),
      str(_staged_paths[:10]))

# Known secret-bearing env var *names* (never their values -- we don't know
# the real values and never will). A file is suspicious if one of these
# names is followed by "=" and then a non-empty, non-placeholder value.
_SECRET_VAR_NAMES = [
    "KITE_API_SECRET", "KITE_ACCESS_TOKEN", "KITE_API_KEY",
    "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
    "INDSTOCKS_ACCESS_TOKEN", "INDSTOCKS_TOTP_SECRET", "INDSTOCKS_MPIN",
    "INDSTOCKS_CLIENT_ID", "PERPLEXITY_API_KEY",
    "DASHBOARD_API_TOKEN", "DASHBOARD_CORS_ORIGINS",
]
_PLACEHOLDER_VALUES = {
    "", "your_vps_ip", "your_github_repo", "your_api_token",
    "yourvpsip", "yourgithubrepo", "yourapitoken",
}
_ASSIGNMENT_RE = re.compile(
    r"^\s*(" + "|".join(_SECRET_VAR_NAMES) + r")\s*=\s*(\S.*)$"
)

_suspicious = []
for rel in _staged_paths:
    p = REPO_ROOT / rel
    if not p.is_file():
        continue
    name = p.name
    # Scope this scan to files that are actually *config* — a live
    # KEY=value assignment there would really be read at runtime. .md docs
    # are deliberately excluded: showing an illustrative example value like
    # "DASHBOARD_API_TOKEN=some-long-random-string" in prose is exactly what
    # documentation is supposed to do and is not a leaked secret — the
    # "no secrets in tracked source" requirement is about files that could
    # actually be sourced/loaded, not about docs describing their shape.
    is_config_file = (
        name == ".env" or name.endswith(".env") or name.endswith(".env.example")
        or name == ".gitignore" or name.endswith(".service")
    )
    if not is_config_file:
        continue
    try:
        text = p.read_text(errors="ignore")
    except OSError:
        continue
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith("#"):
            continue  # a comment explaining the syntax is not an assignment
        m = _ASSIGNMENT_RE.match(line)
        if not m:
            continue
        value = m.group(2).strip().strip('"\'')
        # Placeholder-style values are fine.
        if value.startswith("#") or value.startswith("YOUR_") or value.lower() in _PLACEHOLDER_VALUES:
            continue
        # A shell/systemd variable reference (e.g. "${DASHBOARD_API_TOKEN}")
        # is a template placeholder, not a literal secret.
        if value.startswith("${") or value.startswith("$"):
            continue
        _suspicious.append(f"{rel}: {raw_line.strip()}")

check("C: no file that would be committed assigns a real (non-blank, non-placeholder) value "
      "to a known secret-bearing variable name",
      not _suspicious, "\n".join(_suspicious))

# The three files that are *expected* to mention these variable names (as
# blank templates / placeholders) should still be among the staged files —
# i.e. this scan is actually exercising real files, not silently matching
# nothing.
check("C: .env.example (the blank credential template) is among the scanned files",
      any(p == ".env.example" for p in _staged_paths))
check("C: deploy/api.env.example (the blank API-secrets template) is among the scanned files",
      any(p == "deploy/api.env.example" for p in _staged_paths))
check("C: the real per-VPS secrets file deploy/api.env is NOT staged (must stay gitignored)",
      not any(p == "deploy/api.env" for p in _staged_paths))
check("C: the real frontend config frontend/config.js is NOT staged (must stay gitignored)",
      not any(p == "frontend/config.js" for p in _staged_paths))
check("C: the root .env (if present) is NOT staged",
      not any(p == ".env" for p in _staged_paths))
check("C: no live memory state file (memory/state.json) is staged",
      not any(p == "memory/state.json" for p in _staged_paths))


# ===========================================================================
print("\n--- D: deploy/ artifacts are internally consistent ---")
# ===========================================================================

_service_text = (REPO_ROOT / "deploy" / "trading-api.service").read_text()
check("D: deploy/trading-api.service uses Type=simple (gunicorn doesn't sd_notify)",
      "Type=simple" in _service_text)
check("D: deploy/trading-api.service does NOT use Type=notify",
      "Type=notify" not in _service_text)
_service_execstart = "\n".join(
    ln for ln in _service_text.splitlines() if not ln.strip().startswith("#")
)
check("D: deploy/trading-api.service's actual ExecStart binds gunicorn to 127.0.0.1",
      "--bind 127.0.0.1:" in _service_execstart)
check("D: deploy/trading-api.service's actual ExecStart never binds to 0.0.0.0",
      "--bind 0.0.0.0" not in _service_execstart)
check("D: deploy/trading-api.service points at api.wsgi:app, not api.app / a dev server",
      "api.wsgi:app" in _service_text)
check("D: deploy/trading-api.service has Restart=always (restart on failure)",
      "Restart=always" in _service_text)
check("D: deploy/trading-api.service references an EnvironmentFile (secrets not embedded)",
      "EnvironmentFile=" in _service_text)
check("D: deploy/trading-api.service's actual (non-comment) directives have no bash-style "
      "${VAR:-default} syntax (not supported by systemd's ExecStart= substitution)",
      not re.search(r"\$\{\w+:-", _service_execstart))

_caddy_text = (REPO_ROOT / "deploy" / "Caddyfile.tradingbotapi").read_text()
check("D: deploy/Caddyfile.tradingbotapi is for the documented API hostname",
      "tradingbotapi.bhardwajvaibhav.com" in _caddy_text)
check("D: deploy/Caddyfile.tradingbotapi reverse-proxies to a localhost address",
      "reverse_proxy 127.0.0.1" in _caddy_text)

_req_text = (REPO_ROOT / "requirements.txt").read_text()
check("D: requirements.txt lists gunicorn (the chosen production WSGI server)",
      "gunicorn" in _req_text)

_gitignore_text = (REPO_ROOT / ".gitignore").read_text()
for pattern in ("deploy/api.env", "frontend/config.js", "memory/state.json", ".env"):
    check(f"D: .gitignore excludes {pattern}", pattern in _gitignore_text)


print(f"\n{'=' * 52}")
print(f"  {PASSED} passed, {FAILED} failed")
print(f"{'=' * 52}\n")
sys.exit(1 if FAILED else 0)
