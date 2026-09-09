"""
api/wsgi.py — the production WSGI entrypoint.

`python -m api.app` (see api/app.py's own `main()`) runs Flask's development
server — fine for local use, explicitly NOT fine for production (Flask prints
its own warning about this on every startup). A real deployment instead
points a WSGI server at the plain module-level `app` object this file
exposes, e.g.:

    gunicorn --bind 127.0.0.1:8787 --workers 2 api.wsgi:app

See docs/DEPLOYMENT.md for the full systemd unit that runs this on the VPS.
This file adds no behavior of its own — it only calls the same
`create_app()` factory api/app.py's own dev-server path uses, so local and
production runs are exercising identical route/auth/CORS/error-handling
code, never a second copy of it.
"""

from __future__ import annotations

from .app import create_app

app = create_app()
