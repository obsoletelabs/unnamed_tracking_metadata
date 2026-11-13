"""A20 evidence: real running nodes (the code of the current working directory) on throwaway SQLite files.

* `authority`: role authority, emit protocol 1.5, the H54 bridge on (followers below 1.5 may exist). Its feed signs one
  observation of a record of the custom media type `comic` before the authority ever published that type (the shape
  an older authority, or a type added later, produces).
* `follower` (inherits the upstream's media types, the default) pulls it: the entry waits for the type.
* `strict` (ATLAS_INHERIT_SCHEMA=false) pulls it: the entry is quarantined `media_type_not_adopted`.
* Then the authority creates `comic` with achievements off (Admin → Fields) and a provider-only comic record, so its
  Admin → Peers counts one record the bridge does not carry.

Every title is invented. Run from the worktree:  .venv/Scripts/python <this file> <out.json> <stop-file>
It stays up until <stop-file> exists.
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path.cwd()))
from atlas import feed, service  # noqa: E402
from atlas.contract import ObservationBatchV1  # noqa: E402
from atlas.ids import canonical_id, ulid  # noqa: E402
from tools.multinode_drill import PASSWORD, Node, csrf  # noqa: E402

out, stop = Path(sys.argv[1]), Path(sys.argv[2])
root = Path(tempfile.mkdtemp(prefix="a20-lab-"))
auth = Node(root, "authority", "authority",
            extra_env={"ATLAS_EMIT_PROTOCOL": "1.5", "ATLAS_FEDERATION_BRIDGE_RECORD_OBS": "true"})
fol = Node(root, "follower", "mirror")
strict = Node(root, "strict", "mirror", extra_env={"ATLAS_INHERIT_SCHEMA": "false"})

cid = canonical_id()
with auth.state.maker() as s:  # signed before the authority published the type
    feed.append_unchecked(s, auth.state.ctx.identity, "obs", {
        "canonical_id": cid, "media_type": "comic",
        "observation": {"id": ulid(), "source_kind": "manual", "source": "curator", "source_scope": "a20-lab",
                        "external_id": "lantern-keepers-1", "payload": {"title": "The Lantern Keepers, Volume 1"},
                        "source_updated_at": None, "observed_at": "2026-11-10T09:00:00+00:00"}})
    s.commit()

auth.start()
for node in (fol, strict):
    node.start()
    node.sync(node.add_peer(auth))

# The authority now defines the type (achievements off) and holds a provider-only record of it.
form = {"csrf": csrf(auth.admin, "/admin/schema"), "key": "comic", "label": "Comic"}
assert auth.admin.post("/admin/schema/types", data=form).status_code == 303
form = {"csrf": csrf(auth.admin, "/admin/schema/comic"), "key": "comic", "label": "Comic", "enabled": "on",
        "position": "7", "label_developer": "Writer", "achievements_shown": "1"}
assert auth.admin.post("/admin/schema/types", data=form).status_code == 303
with auth.state.maker() as s:
    batch = ObservationBatchV1.model_validate({"connection": {"source_kind": "metadata", "source": "steam",
                                                              "source_scope": "a20-lab"},
                                              "observations": [{"external_id": "9000001",
                                                                "title": "Harbour Signals, Book Two",
                                                                "provider_ids": {"steam": "9000001"}}]})
    service.ingest(s, auth.state.ctx, batch, system=True, media_type="comic")
    s.commit()

out.write_text(json.dumps({"follower": fol.url, "strict": strict.url, "authority": auth.url, "user": "drill-admin",
                           "password": PASSWORD, "cid": cid}), encoding="utf-8")
print("ready", fol.url, strict.url, auth.url, flush=True)
try:
    while not stop.exists():
        time.sleep(0.5)
finally:
    for node in (fol, strict, auth):
        node.close()
