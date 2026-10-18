"""Admin-written Markdown pages. Rendering is CommonMark with raw HTML disabled, so a page can never inject markup."""

from __future__ import annotations

from markdown_it import MarkdownIt
from markupsafe import Markup
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import Page

_md = MarkdownIt("commonmark", {"html": False, "linkify": False, "typographer": False}).disable(["image"])


def _link_open(self, tokens, idx, options, env):
    tokens[idx].attrSet("rel", "noopener noreferrer nofollow")
    return self.renderToken(tokens, idx, options, env)


_md.add_render_rule("link_open", _link_open)


def render_markdown(text: str) -> Markup:
    # `image` is disabled: the CSP only allows images from this server, and pages are text.
    return Markup(_md.render(text or ""))  # noqa: S704 - html input is disabled; markdown-it escapes the rest


DEFAULT_PAGES = {
    "about": ("About", "# About\n\nThis site collects and merges game metadata from many providers into one record per "
              "game, and keeps track of where every detail came from.\n\nEdit this page from **Admin → Pages**.\n",
              True, 10),
    "rules": ("Contribution rules", "# Contribution rules\n\n* Cite a source (a store page, the publisher's site) for "
              "every change.\n* Don't remove correct information; propose improvements instead.\n* Everything you "
              "contribute is released under CC0.\n* Reviewers may reject or revert changes that aren't supported by a "
              "source.\n", True, 20),
    "help": ("Help", "# Help\n\nSearch for a game, open it, and choose **Suggest an edit** to improve it. New accounts' "
             "changes are reviewed before they appear; trusted contributors' changes apply at once.\n", False, 30),
}


def seed_pages(session: Session) -> None:
    if session.scalar(select(func.count()).select_from(Page)):
        return
    for slug, (title, body, nav, pos) in DEFAULT_PAGES.items():
        session.add(Page(slug=slug, title=title, body=body, in_nav=nav, position=pos))
    session.flush()
