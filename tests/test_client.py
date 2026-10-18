import importlib.util
import pathlib

import pytest
from fastapi.testclient import TestClient

from atlas import main

from .conftest import build_settings

spec = importlib.util.spec_from_file_location(
    "atlas_client", pathlib.Path(__file__).parent.parent / "clients/python/atlas_client.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


@pytest.fixture
def api(tmp_path):
    s = build_settings(database_url=f"sqlite:///{tmp_path}/c.db", bootstrap_admin_token="k" * 40)
    with TestClient(main.create_app(s), base_url="http://atlas") as tc:
        yield mod.AtlasClient("http://atlas", "k" * 40, client=tc)


def test_round_trip_and_batching(api):
    api.submit("steam", "inst", [{"external_id": str(i), "title": f"Game {i}", "release_date": "2020-01-01"}
                                 for i in range(5)])
    items = [{"namespace": "steam", "external_id": str(i)} for i in range(5)] + [{"title": "Nope"}]
    out = list(api.resolve(items, batch_size=2))
    assert [r["canonical_id"] is not None for r in out] == [True] * 5 + [False]
    gid = out[0]["canonical_id"]
    assert api.game(gid)["title"] == "Game 0"
    assert api.search("game 3")[0]["title"] == "Game 3"


def test_errors_surface(api):
    with pytest.raises(mod.AtlasClientError) as e:
        api.game("atlas:missing")
    assert e.value.status == 404
