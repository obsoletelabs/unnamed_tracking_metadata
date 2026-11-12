"""Database convergence across a real five-node federation (see docs/guide/federation-lab.md)."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.federation_lab


def test_initial_full_sync_converges_every_node(lab):
    official, indie, mirror, local, local2 = (lab.nodes[n] for n in ("official", "indie", "mirror", "local", "local2"))
    for index in range(6):
        official.post(f"Official Game {index}", developer="Lab Studio")
    for index in range(3):
        indie.post(f"Indie Game {index}")
    assert official.head() == 7 and indie.head() == 4  # each feed starts with `wire_start` (C16 Z1)
    # Nothing has been replicated yet: no follower holds a single federated row.
    assert local.observations(official) == {} and mirror.observations(indie) == {}
    with pytest.raises(AssertionError):
        lab.assert_converged()  # the convergence check has teeth: an unsynced follower fails it

    lab.sync_all()

    lab.assert_converged()
    assert len(local.observations(official)) == 6 and len(local.observations(indie)) == 3
    # Two independent trust roots stay separate: each follower tracks one cursor per authority.
    assert official.node_id != indie.node_id
    assert mirror.cursor(official) == 7 and mirror.cursor(indie) == 4
    # The same-owner second node ends up identical to the first, row for row.
    assert local.observations(official) == local2.observations(official)
    assert local.canonicals() == local2.canonicals()


def test_incremental_sync_applies_only_new_entries(lab):
    official, mirror, local = lab.nodes["official"], lab.nodes["mirror"], lab.nodes["local"]
    first = official.post("Incremental One")
    lab.sync_all()
    before_ids = set(local.observations(official))

    updated = official.post("Incremental One", external_id="Incremental One", developer="Added Later")
    assert updated == first, "the same external id must stay on the same canonical record"
    second = official.post("Incremental Two")
    lab.sync_all()

    lab.assert_converged()
    new_ids = set(local.observations(official)) - before_ids
    assert len(new_ids) == 2 and local.cursor(official) == official.head() == 4
    assert mirror.cursor(official) == 4
    view = local.canonicals({first})[first]
    assert "Added Later" in view[2]
    assert second in local.canonicals()


def test_repeated_syncs_are_idempotent(lab):
    official, indie, local = lab.nodes["official"], lab.nodes["indie"], lab.nodes["local"]
    official.post("Repeat Official")
    indie.post("Repeat Indie")
    lab.sync_all()
    settled = {name: node.digest() for name, node in lab.nodes.items()}

    for _ in range(3):
        lab.sync_all()

    assert {name: node.digest() for name, node in lab.nodes.items()} == settled
    assert not local.open_failures()
    lab.assert_converged()
