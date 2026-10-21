"""Server-rendered website: browse, contribute, review, administer. No build step, no inline scripts."""

from __future__ import annotations

from pathlib import Path

from fastapi.templating import Jinja2Templates

from .. import integrations

HERE = Path(__file__).parent
templates = Jinja2Templates(directory=str(HERE / "templates"))
templates.env.globals.update(store_link=integrations.store_link, store_name=integrations.store_name)
STATIC_DIR = HERE / "static"

CSP = ("default-src 'none'; img-src 'self'; style-src 'self'; script-src 'self'; form-action 'self'; "
       "base-uri 'none'; frame-ancestors 'none'; connect-src 'self'")
