"""Server-rendered website: browse, contribute, review, administer. No build step, no inline scripts."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.templating import Jinja2Templates
from jinja2 import pass_context
from markupsafe import Markup

from .. import dates, integrations

HERE = Path(__file__).parent
templates = Jinja2Templates(directory=str(HERE / "templates"))
templates.env.globals.update(store_link=integrations.store_link, store_name=integrations.store_name)


def release_year(view: dict | None) -> str:
    """The year shown next to a title in lists: the first release's year, which a year-only date also has."""
    view = view or {}
    year = view.get("release_year") or (view.get("release_date") or "")[:4]
    return str(year or "")


# ---- dates: every template formats through atlas.dates (SPEC-UI-POLISH-PASS §6.4) --------------------------
# `when`/`day` emit `<time datetime="ISO">text</time>`; `when_text`/`day_text`/`release_text` return plain text for
# attributes and for places that already sit inside their own `<time>`. All read the viewer's `prefs` from the
# render context (signed-in profile values, or the anonymous `atlas_date` cookie), so no route has to pass them.

def time_tag(value: Any, text: str, precision: str | None = None) -> Markup:
    if not text:
        return Markup("")
    extra = Markup(' data-precision="{}"').format(precision) if precision else Markup("")
    return Markup('<time datetime="{}"{}>{}</time>').format(dates.iso_of(value), extra, text)


def _prefs(ctx: Any) -> dict:
    prefs = ctx.get("prefs")
    return prefs if isinstance(prefs, dict) else {}


@pass_context
def when(ctx: Any, dt: Any, relative: bool = False) -> Markup:
    """A moment as `<time>`; with `relative`, "5 min ago" and the absolute value in `title`."""
    if dt is None or dt == "":
        return Markup("")
    prefs = _prefs(ctx)
    text = dates.format_instant(dt, prefs, relative=relative)
    if relative:
        exact = dates.format_instant(dt, prefs)
        if exact != text:
            return Markup('<time datetime="{}" title="{}">{}</time>').format(dates.iso_of(dt), exact, text)
    return time_tag(dt, text)


@pass_context
def when_text(ctx: Any, dt: Any, relative: bool = False) -> str:
    return dates.format_instant(dt, _prefs(ctx), relative=relative)


@pass_context
def day(ctx: Any, value: Any, precision: str | None = None) -> Markup:
    if value is None or value == "":
        return Markup("")
    text = dates.format_day(value, precision, _prefs(ctx))
    if isinstance(value, str):
        shown = dates.try_parse(value, precision, any_year=True)
        if shown is None:  # a provider's free-form date ("Q3 2020"): text, never a false machine value
            return Markup("{}").format(text)
        return time_tag(value, text, shown.precision)
    return time_tag(value, text)


@pass_context
def day_text(ctx: Any, value: Any, precision: str | None = None) -> str:
    return dates.format_day(value, precision, _prefs(ctx))


@pass_context
def release_text(ctx: Any, value: Any, precision: Any = None, _ignored_prefs: Any = None) -> str:
    """Text only: every caller already sits inside its own `<time>` (a tag here would nest them)."""
    return dates.format_day(value, precision, _prefs(ctx))


@pass_context
def date_placeholder(ctx: Any, partial: bool = True) -> str:
    return dates.placeholder(_prefs(ctx), partial=partial)


templates.env.globals.update(when=when, when_text=when_text, day=day, day_text=day_text, release_text=release_text,
                             date_placeholder=date_placeholder, release_year=release_year,
                             region_name=dates.region_name, category_name=dates.category_name,
                             release_categories=dates.CATEGORIES, region_groups=dates.REGION_GROUPS,
                             max_releases=dates.MAX_RELEASES)
templates.env.filters.update(when=when, day=day, when_text=when_text, day_text=day_text)
STATIC_DIR = HERE / "static"

CSP = ("default-src 'none'; img-src 'self'; style-src 'self'; script-src 'self'; form-action 'self'; "
       "base-uri 'none'; frame-ancestors 'none'; connect-src 'self'")

__all__ = ["CSP", "STATIC_DIR", "release_year", "templates", "time_tag"]
