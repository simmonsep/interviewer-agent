"""Serve the built desktop frontend from the same origin as the API.

The desktop window has to talk to the local API without weakening its checks, so the
built frontend is served by that API process rather than by a separate static server:

* ``Origin`` on writes is naturally ``http://127.0.0.1:3100``, the only value the
  boundary accepts, so no rewrite or extra allowance is introduced;
* the session cookie stays same-origin, so ``SameSite`` and ``credentials:
  'same-origin'`` behave as they do in the browser build;
* there is no second port to validate, occupy or leak.

``/api/*`` and ``/health`` are registered before the mount, so they keep priority.
"""

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles


def attach_static_site(app: FastAPI, directory: Path) -> None:
    """Mount a built SPA at ``/``; raises when the directory or entry file is absent."""
    root = Path(directory)
    if not (root / "index.html").is_file():
        raise ValueError("static_site_incomplete")
    # html=True maps "/" to index.html. There is no client-side router (the shell is a
    # query parameter), so a catch-all rewrite is unnecessary.
    app.mount("/", StaticFiles(directory=str(root), html=True), name="desktop-web")
