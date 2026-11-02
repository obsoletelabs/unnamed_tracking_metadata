"""Server-rendered website: browse, contribute, review, administer. No build step, no inline scripts."""

from __future__ import annotations

from pathlib import Path

from fastapi.templating import Jinja2Templates

from .. import dates, integrations

HERE = Path(__file__).parent
templates = Jinja2Templates(directory=str(HERE / "templates"))
templates.env.globals.update(store_link=integrations.store_link, store_name=integrations.store_name)


def release_year(view: dict | None) -> str:
    """The year shown next to a title in lists: the first release's year, which a year-only date also has."""
    view = view or {}
    year = view.get("release_year") or (view.get("release_date") or "")[:4]
    return str(year or "")


templates.env.globals.update(release_text=dates.display, release_year=release_year, region_name=dates.region_name,
                             category_name=dates.category_name, release_categories=dates.CATEGORIES,
                             region_groups=dates.REGION_GROUPS, max_releases=dates.MAX_RELEASES)
STATIC_DIR = HERE / "static"

CSP = ("default-src 'none'; img-src 'self'; style-src 'self'; script-src 'self'; form-action 'self'; "
       "base-uri 'none'; frame-ancestors 'none'; connect-src 'self'")
