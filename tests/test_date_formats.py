"""One date formatter for every screen (SPEC-UI-POLISH-PASS §6-§7).

The guards (T-D1, T-D2) fail when a template or a web module formats a date by itself again; everything else
checks the formatter's output in each style and the partial-date entry it reads back.
"""
import ast
import json
import re
from datetime import UTC, date, datetime
from html import unescape
from pathlib import Path

import pytest

from atlas import dates

from .test_web import fresh, signup, site, token  # noqa: F401  (fixture)

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "atlas" / "web" / "templates"
WEB = ROOT / "atlas" / "web"
VECTORS = Path(__file__).parent / "fixtures" / "date-format-vectors.json"

# ---- T-D1 / T-D2: no formatting outside the formatter -----------------------------------------------------

TEMPLATE_BANNED = [
    (re.compile(r"strftime"), "strftime in a template"),
    (re.compile(r"\|\s*date\b"), "a |date filter"),
    (re.compile(r"\.isoformat\(\)"), "isoformat() shown as text"),
    # a raw timestamp or release value printed as it is stored
    (re.compile(r"\{\{\s*(?!lbl\.)[\w.\[\]'\"]+\.(?:\w+_at|as_of|release_date|observed_at|expires_at)\s*\}\}"),
     "a raw stored date printed without day()/when()"),  # lbl.* are field labels
]


def test_no_strftime_in_templates():
    found = []
    for path in sorted(TEMPLATES.rglob("*.html")):
        text = path.read_text(encoding="utf-8")
        for pattern, why in TEMPLATE_BANNED:
            for m in pattern.finditer(text):
                line = text.count("\n", 0, m.start()) + 1
                found.append(f"{path.relative_to(ROOT)}:{line}: {why}: {m.group(0)!r}")
    assert not found, "dates must go through when()/day() (atlas/dates.py):\n" + "\n".join(found)


# Storage and wire formats, never shown to a person.
PYTHON_ALLOWED = {
    ("setup_checklist.py", "isoformat"),  # node_state timestamp
}


def test_no_display_formatting_in_web_python():
    found = []
    for path in sorted(WEB.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                name = node.func.attr
                if name in ("strftime", "isoformat") and (path.name, name) not in PYTHON_ALLOWED:
                    found.append(f"{path.relative_to(ROOT)}:{node.lineno}: .{name}()")
            if isinstance(node, ast.FunctionDef) and node.name == "when" and path.name == "routes.py":
                found.append(f"{path.relative_to(ROOT)}:{node.lineno}: a second when() formatter")
    assert not found, "format dates with atlas.dates (format_day/format_instant):\n" + "\n".join(found)


# ---- the formatter -----------------------------------------------------------------------------------------

DT = datetime(2020, 9, 17, 14, 5, tzinfo=UTC)


@pytest.mark.parametrize(("style", "day", "instant"), [
    ("dmy", "17/09/2020", "17/09/2020 14:05 UTC"),
    ("long", "17 September 2020", "17 September 2020, 14:05 UTC"),
    ("mdy", "09/17/2020", "09/17/2020 2:05 PM UTC"),
    ("iso", "2020-09-17", "2020-09-17 14:05 UTC"),
])
def test_every_style(style, day, instant):
    prefs = {"date_format": style}
    assert dates.format_day("2020-09-17", "day", prefs) == day
    assert dates.format_day(date(2020, 9, 17), None, prefs) == day
    assert dates.format_instant(DT, prefs) == instant


def test_vectors():
    """Shared vectors (§6.7): the app's formatter reads the same file."""
    for v in json.loads(VECTORS.read_text(encoding="utf-8")):
        prefs = {"date_format": v["style"], **({"time_format": v["time"]} if v.get("time") else {})}
        if v["kind"] == "day":
            assert dates.format_day(v["value"], v.get("precision"), prefs) == v["shown"], v
        elif v["kind"] == "instant":
            when = datetime.fromisoformat(v["value"])
            assert dates.format_instant(when, prefs) == v["shown"], v
        else:
            assert dates.placeholder(prefs, partial=v.get("partial", True)) == v["shown"], v


def test_partial_dates_never_look_like_a_day():
    for style in dates.STYLES:
        prefs = {"date_format": style}
        assert dates.format_day("2011", "year", prefs) == "2011"
        month = dates.format_day("2011-04", "month", prefs)
        assert month in ("April 2011", "2011-04")
        assert not re.fullmatch(r"\d{1,2}[/.]\d{1,2}[/.]\d{4}", month)


def test_default_is_dmy_for_anonymous_and_unset():
    assert dates.style_of({}) == "dmy" and dates.style_of(None) == "dmy"
    assert dates.format_day("2020-09-17", "day", {}) == "17/09/2020"
    assert not dates.style_chosen({})


def test_legacy_values_map():
    assert dates.style_of({"date_format": "locale"}) == "dmy"
    assert dates.style_of({"date_format": "ymd"}) == "iso"
    assert dates.style_chosen({"date_format": "ymd"}) and not dates.style_chosen({"date_format": "locale"})


def test_relative_instants():
    now = datetime(2020, 9, 17, 14, 5, tzinfo=UTC)
    assert dates.format_instant(now, {}, relative=True, now=now) == "just now"
    assert dates.format_instant(datetime(2020, 9, 17, 13, 50, tzinfo=UTC), {}, relative=True, now=now) == "15 min ago"
    assert dates.format_instant(datetime(2020, 9, 16, 12, 0, tzinfo=UTC), {}, relative=True, now=now) == "yesterday"


# ---- entry (§7.2) ------------------------------------------------------------------------------------------

@pytest.mark.parametrize(("typed", "prefs", "value"), [
    ("2020-09-17", {}, "2020-09-17"),
    ("2020", {}, "2020"),
    ("2020-09", {}, "2020-09"),
    ("2011-4", {}, "2011-04"),
    ("17/09/2020", {}, "2020-09-17"),
    ("17.09.2020", {}, "2020-09-17"),
    ("09/2020", {}, "2020-09"),
    ("17 Sep 2020", {}, "2020-09-17"),
    ("Sep 17 2020", {}, "2020-09-17"),
    ("September 2020", {}, "2020-09"),
    ("09/17/2020", {"date_format": "mdy"}, "2020-09-17"),
    ("03/04/2020", {"date_format": "dmy"}, "2020-04-03"),
    ("03/04/2020", {"date_format": "mdy"}, "2020-03-04"),
    ("29/02/2020", {}, "2020-02-29"),
])
def test_parse_entry_reads(typed, prefs, value):
    r = dates.parse_entry(typed, prefs)
    assert r.ok is not None and r.ok.value == value, r


def test_parse_entry_ambiguous_until_a_style_is_chosen():
    r = dates.parse_entry("03/04/2020", {})
    assert r.ok is None and [d.value for d in r.ambiguous] == ["2020-04-03", "2020-03-04"]
    assert dates.parse_entry("13/04/2020", {}).ok.value == "2020-04-13"  # only one reading


@pytest.mark.parametrize(("typed", "message"), [
    ("29/02/2021", "not a calendar date"),
    ("31/04/2020", "not a calendar date"),
    ("2011-13", "no month 13"),
    ("1949", "1950 and 2100"),
    ("soon", "not a date this field accepts"),
])
def test_parse_entry_errors(typed, message):
    r = dates.parse_entry(typed, {})
    assert r.ok is None and r.error and message in r.error, r


def test_parse_entry_any_year_for_day_fields():
    assert dates.parse_entry("21/12/1937", {}, any_year=True).ok.value == "1937-12-21"


@pytest.mark.parametrize(("style", "shown"), [("dmy", "dd/mm/yyyy"), ("mdy", "mm/dd/yyyy"), ("iso", "yyyy-mm-dd"),
                                               ("long", "17 September 2020")])
def test_placeholders_follow_style(style, shown):
    assert dates.placeholder({"date_format": style}, partial=False) == shown
    assert dates.placeholder({"date_format": style}).startswith(shown)


# ---- rendered pages ----------------------------------------------------------------------------------------

def test_every_screen_follows_the_style(site):  # noqa: F811
    page = unescape(site.get(f"/game/{site.gid}").text)
    assert "17/09/2020" in page and ">2020-09-17<" not in page  # default; ISO only inside datetime=
    signup(site, "alice")
    for style, shown in (("mdy", "09/17/2020"), ("long", "17 September 2020"), ("iso", "2020-09-17"),
                         ("dmy", "17/09/2020")):
        r = site.post("/prefs/date-format", data={"csrf": token(site, "/"), "value": style, "next": "/"})
        assert r.status_code == 303, r.text
        page = unescape(site.get(f"/game/{site.gid}").text)
        assert shown in page, style
        edit = site.get(f"/game/{site.gid}/edit").text
        assert f'placeholder="{dates.placeholder({"date_format": style})}"' in unescape(edit)


def test_when_is_global_and_prefs_aware(site):  # noqa: F811
    signup(site, "alice")  # first account: administrator
    site.post("/prefs/date-format", data={"csrf": token(site, "/"), "value": "mdy", "next": "/"})
    admin = site.get("/admin").text
    today = datetime.now(UTC)
    shown = f"{today.month:02d}/{today.day:02d}/{today.year}"
    assert re.search(r"<time datetime=\"[^\"]+\"[^>]*>" + shown, admin)


def test_no_time_tag_in_attributes_or_nested(site):  # noqa: F811
    signup(site, "alice")
    for path in ("/", f"/game/{site.gid}", "/admin", "/admin/peers", "/me", "/search?q=Hades"):
        html = site.get(path).text
        for attr in re.findall(r'(?:title|aria-label|placeholder|value)="([^"]*)"', html):
            assert "<time" not in attr, (path, attr)
        assert not re.search(r"<time[^>]*>(?:(?!</time>).)*<time", html, re.S), path
