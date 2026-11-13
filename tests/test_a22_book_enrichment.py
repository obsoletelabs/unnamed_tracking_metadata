"""CONTRACT-C15 T-B12 (A22): a book travels as a `record` and the follower builds it from its own Open Library access.

Real nodes on SQLite files talking through the real sync routes (`test_sync_compat.Node`, as the C13 enrichment
tests). The authority imports a book from Open Library; Open Library observations are not redistributable (D15.2), so
only the record's identity is signed. The follower, with H56 on, fetches the work from its *own* Open Library adapter
(a fake transport over the synthetic fixtures) and ends with editions, authors and the cover row; it signs nothing."""

from __future__ import annotations

from sqlalchemy import select

from atlas import enrich, importer
from atlas.models import Artwork, Canonical, ChangeLog, Crosswalk, Observation

from .test_a22_openlibrary import ISBN, W1, fixture_client, ol
from .test_sync_compat import Node, pull


def book_node(tmp_path, name, role, **settings) -> tuple[Node, list]:
    node = Node(tmp_path, name, role, **settings)
    calls: list = []
    node.st.providers.client = fixture_client(calls)
    node.st.providers.providers["openlibrary"] = ol()
    return node, calls


def signed(node: Node) -> list[tuple[str, dict]]:
    with node.st.maker() as s:
        return [(row.kind, row.body) for row in s.scalars(select(ChangeLog).order_by(ChangeLog.seq))]


def test_book_record_introduced_and_enriched(tmp_path):
    authority, _ = book_node(tmp_path, "authority", "authority", emit_protocol="1.5", federation_introduce="all")
    with authority.st.maker() as s:
        got = importer.import_from_provider(authority.st, s, "openlibrary", W1)
        s.commit()
        cid = got.row.id
    entries = signed(authority)
    records = [b for k, b in entries if k == "record"]
    assert [r.get("canonical_id") or r.get("id") for r in records] == [cid]
    assert records[0]["media_type"] == "book" and records[0]["provider_ids"]["openlibrary"] == W1
    assert not any(k == "obs" and b["observation"]["source"] == "openlibrary" for k, b in entries)  # D15.2: kept local

    mirror, calls = book_node(tmp_path, "mirror", "mirror", sync_enrich_from_own_providers=True)
    pull(mirror, authority)
    with mirror.st.maker() as s:
        assert sorted((j["ns"], j["id"]) for _, j in enrich.pending(s)) == [("openlibrary", W1)]  # isbn: no provider
        before = len(s.scalars(select(ChangeLog)).all())
        row = s.get(Canonical, cid)
        assert row.media_type == "book" and not row.view.get("editions")
    assert calls == []  # nothing fetched until the job runs
    assert enrich.run(mirror.st.maker, mirror.st) == {"fetched": 1, "dropped": 0, "deferred": 0, "failed": 0}
    assert {c.url.path for c in calls} >= {f"/works/{W1}.json", f"/works/{W1}/editions.json"}
    with mirror.st.maker() as s:
        row = s.get(Canonical, cid)
        assert row.media_type == "book" and row.merged_into is None
        assert [a["name"] for a in row.view["authors"]] == ["Ione Marsh", "Pell Arden"]
        assert any(e.get("isbn_13") == ISBN for e in row.view["editions"])
        obs = s.scalars(select(Observation).where(Observation.canonical_id == cid,
                                                  Observation.source == "openlibrary")).one()
        assert obs.origin_node is None and obs.redistributable is False
        assert s.get(Crosswalk, ("openlibrary", W1)).canonical_id == cid
        assert [(a.kind, a.source) for a in s.scalars(select(Artwork).where(Artwork.canonical_id == cid))] == [
            ("poster", "openlibrary")]
        assert len(s.scalars(select(ChangeLog)).all()) == before  # nothing re-emitted
