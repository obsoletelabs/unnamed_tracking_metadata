"""Default multiplayer support (issue #39): min/max players and crossplay as standard game fields."""

from types import SimpleNamespace

import httpx
from sqlalchemy import select

from atlas import custom, importer
from atlas.models import CustomValue, FieldDef
from atlas.providers import Fetcher
from atlas.providers.base import ProviderResult

from .conftest import build_settings


def keys(session):
    return {f.key for f in session.scalars(select(FieldDef).where(FieldDef.media_type == "game"))}


def test_multiplayer_fields_are_seeded_once_and_deletion_is_respected(session, ctx):
    custom.seed(session)
    custom.seed_multiplayer(session, ctx)
    assert {"players_min", "players_max", "crossplay"} <= keys(session)
    kinds = {f.key: f.kind for f in session.scalars(select(FieldDef))}
    assert kinds["players_min"] == kinds["players_max"] == "number" and kinds["crossplay"] == "choice"

    custom.delete_field(session, ctx, "game", "crossplay")
    custom.seed_multiplayer(session, ctx)  # the next start must not bring it back
    assert "crossplay" not in keys(session)


def test_mirrors_take_multiplayer_fields_from_their_upstream(session):
    from atlas.service import Ctx
    from atlas.signing import load_identity

    settings = build_settings(role="mirror")
    mirror = Ctx(settings, load_identity(session))
    custom.seed(session)
    custom.seed_multiplayer(session, mirror)
    assert "players_max" not in keys(session)


def test_multiplayer_values_are_validated(session, ctx):
    custom.seed(session)
    custom.seed_multiplayer(session, ctx)
    ok = custom.clean_values(session, "game", {"players_min": "1", "players_max": 4, "crossplay": "partial"},
                             editable_only=False)
    assert ok == {"players_min": 1, "players_max": 4, "crossplay": "partial"}
    for bad in ({"crossplay": "maybe"}, {"players_max": "lots"}):
        try:
            custom.clean_values(session, "game", bad, editable_only=False)
        except Exception as exc:
            assert "choose one of" in str(exc) or "number" in str(exc)
        else:
            raise AssertionError(bad)


def test_steam_reports_crossplay_only_when_the_store_says_so():
    from atlas.providers.steam import _multiplayer

    assert _multiplayer({"categories": [{"description": "Cross-Platform Multiplayer"}]}) == {"crossplay": "yes"}
    assert _multiplayer({"categories": [{"description": "Multi-player"}]}) == {}  # silence is not "no"
    assert _multiplayer({}) == {}


class Prov:
    name = "mp"
    redistributable = False
    requests_per_minute = 600

    def __init__(self, custom_values):
        self.custom_values = custom_values

    def search(self, client, query):
        return []

    def fetch(self, client, external_id):
        return ProviderResult(external_id="1", title="Overcooked", platform="pc", provider_ids={"mp": "1"},
                              custom=self.custom_values)


def test_import_stores_provider_reported_multiplayer_values(session, ctx):
    custom.seed(session)
    custom.seed_multiplayer(session, ctx)
    fetcher = Fetcher(ctx.settings, {"mp": Prov({"crossplay": "yes", "players_max": 4, "unknown_field": "x"})},
                      httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404))))
    st = SimpleNamespace(providers=fetcher, ctx=ctx)
    result = importer.import_from_provider(st, session, "mp", "1")
    rows = {v.key: v.value for v in session.scalars(select(CustomValue).where(CustomValue.canonical_id == result.row.id))}
    assert rows == {"crossplay": "yes", "players_max": 4}  # the undefined field is skipped, not an error
    assert custom.merged(session, ctx, result.row.id)["players_max"] == 4
