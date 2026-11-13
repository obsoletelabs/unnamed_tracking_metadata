"""A21 evidence: a real running node (the code of the current working directory) on a throwaway SQLite file that is
upgraded from the previous release (migration 0060, A20) while it already holds an administrator-made media type
`book` ("Novels", two custom fields, one of them `authors`). The upgrade to 0061 adopts it as the built-in book type,
so Admin -> Media types shows the notice and the type page shows achievements off. One curated book record (invented
title, authors and ISBNs) is added so the type has a record.

Run from the worktree:  .venv/Scripts/python <this file> <out.json> <stop-file>
It stays up until <stop-file> exists.
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

from sqlalchemy import create_engine, text

sys.path.insert(0, str(Path.cwd()))
from atlas import db, service  # noqa: E402
from atlas.contract import ObservationBatchV1  # noqa: E402
from tools.multinode_drill import PASSWORD, Node  # noqa: E402

out, stop = Path(sys.argv[1]), Path(sys.argv[2])
root = Path(tempfile.mkdtemp(prefix="a21-lab-"))
url = f"sqlite:///{root}/node.db"
db.upgrade(url, "0060")  # the previous release's schema
engine = create_engine(url)
with engine.begin() as c:
    c.execute(text("INSERT INTO media_type (key, label, enabled, position, labels, origin, achievements) "
                   "VALUES ('book', 'Novels', 1, 9, :l, 'local', 1)"), {"l": json.dumps({"developer": "Writer"})})
    for n, (key, label) in enumerate((("authors", "Authors (old field)"), ("shelf", "Shelf"))):
        c.execute(text("INSERT INTO field_def (id, media_type, origin, key, label, kind, choices, help, public, "
                       "editable, position) VALUES (:i, 'book', 'local', :k, :lab, 'text', '[]', '', 1, 1, :p)"),
                  {"i": f"01J000000000000000000000A{n}", "k": key, "lab": label, "p": n})
engine.dispose()

node = Node(root, "node", "authority", database_url=url, extra_env={"ATLAS_EMIT_PROTOCOL": "1.5"})  # migrates to 0061
with node.state.maker() as s:
    batch = ObservationBatchV1.model_validate({
        "connection": {"source_kind": "manual", "source": "manual", "source_scope": "default"}, "media_type": "book",
        "observations": [{"external_id": "lantern-keeper", "title": "The Lantern Keeper",
                          "ext": {"book": {"authors": [{"name": "Ione Marsh"}, {"name": "Pell Arden",
                                                                               "role": "translator"}],
                                           "page_count": 412,
                                           "editions": [{"name": "Paperback", "isbn_13": "978-1-234-56789-7",
                                                         "format": "paperback"}]}}}]})
    node.state.ctx.settings.emit_protocol = "1.5"
    [res] = service.ingest(s, node.state.ctx, batch, curator=True, media_type="book")
    # review fix: a series (invented) and a positioned series_member, for the relations page
    series = ObservationBatchV1.model_validate({
        "connection": {"source_kind": "manual", "source": "manual", "source_scope": "default"}, "media_type": "series",
        "observations": [{"external_id": "lamp-cycle", "title": "The Lamp Cycle"}]})
    [ser] = service.ingest(s, node.state.ctx, series, curator=True, media_type="series")
    from atlas import relations

    relations.add(s, node.state.ctx, res.canonical_id, ser.canonical_id, "series_member", position="1")
    s.commit()
node.start()

out.write_text(json.dumps({"node": node.url, "user": "drill-admin", "password": PASSWORD, "cid": res.canonical_id}),
               encoding="utf-8")
print("ready", node.url, flush=True)
try:
    while not stop.exists():
        time.sleep(0.5)
finally:
    node.close()
