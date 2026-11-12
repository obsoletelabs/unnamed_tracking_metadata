# Federation test lab

The federation lab is a disposable network of real Atlas servers that you can break on purpose. It exists so that changes to replication, signing and the feed formats are checked against what a follower's **database** ends up holding, not just against an HTTP status. It is test infrastructure: it changes no product behaviour.

## What it builds

```
 official (authority)      indie (authority, its own trust root)
          \                 /
           mirror  (relays both retained feeds)
            |    \
         local    local2          two plain nodes of the same owner
```

* **Five real servers**, each a separate `uvicorn` process with its own database, media folder, signing key, secret key, bootstrap token, users and sync credentials. All of them are generated for the run. Nothing is shared with a deployment and nothing leaves loopback.
* **Official-like authority** (`official`) and a **separate independent authority** (`indie`). They have different node identities and different pinned roots; a follower tracks one cursor per authority.
* A **mirror** that follows both, and two **local** nodes that follow both authorities' feeds through the mirror. `local2` stands in for a second node of the same owner; today it is a plain local node.
* The authorities use SQLite by default, or a disposable PostgreSQL database each when you pass a PostgreSQL admin URL.

It reuses the mechanism of `tools/multinode_drill.py` (in-process app creation for migrations and seed users, then a real server process). It does not use Docker Compose: the same code then runs identically on Windows, macOS and Linux, and the lab can reach into each node's database to assert on it. The compose files under `deploy/` remain the way to try a deployment by hand (see [Try federation on one machine](hosting/lab.md)).

## Run it

```bash
uv sync --frozen --extra dev
uv run python -m tools.federation_lab                      # every scenario
uv run python -m tools.federation_lab -- -k rotation       # arguments after -- go to pytest
uv run python -m tools.federation_lab --logs lab-logs      # keep every node's server log
uv run python -m tools.federation_lab \
    --postgres-admin-url postgresql+psycopg://postgres:pw@localhost:5432/postgres
```

The same scenarios run under plain pytest once you opt in (a normal `pytest` run collects them and skips them, because each scenario starts five servers):

```bash
ATLAS_FEDERATION_LAB=1 uv run pytest tests/federation_lab -q          # Linux, macOS, Git Bash
$env:ATLAS_FEDERATION_LAB = "1"; uv run pytest tests/federation_lab -q # PowerShell
```

Each scenario gets a fresh network. Starting one takes about half a minute on Windows and less on Linux, so a full run takes several minutes. Docker is not needed.

## Scenarios

Every scenario ends with assertions on node databases: observation rows by id, canonical views, retained signed entries, cursors and open sync failures. `Lab.assert_converged()` checks that each follower holds exactly each authority's published observations, the same views, the same signed entries and a cursor at the authority's head. `LabNode.digest()` hashes everything federation can change, so "unchanged" is a single comparison.

| Scenario | File | What it proves |
| --- | --- | --- |
| Initial full sync | `test_convergence.py` | Two authorities, mirror, two local nodes converge; same-owner nodes are row-for-row identical |
| Incremental sync | `test_convergence.py` | Only new and changed entries apply; an updated external id stays on one record |
| Repeated runs | `test_convergence.py` | Syncing again changes nothing on any node |
| Replayed and duplicate batches | `test_faults.py` | Lost cursor and lost retained copies re-apply to identical state; a stale page or a duplicated entry is rejected and leaves the database untouched |
| Offline and reconnect | `test_faults.py` | An unreachable authority or mirror is reported, changes nothing, and everything converges after it returns; the other authority keeps flowing |
| Cursor expiry | `test_faults.py` | Atlas has no feed retention window yet, so the way a cursor stops being valid is an authority restored from an older backup. A follower that re-reads from the start refuses the fork; an up-to-date follower sees the advertised head fall below what it verified and reports a fork or rewind |
| Bad signature | `test_faults.py` | A corrupted signature or a swapped body is rejected and nothing is applied |
| Rotated and revoked key | `test_faults.py` | Followers cross a rotation without re-pinning; the retired key can no longer sign |
| Wrong authority | `test_faults.py` | A URL serving another authority, a wrong pinned key and a relay that lacks the pinned feed are all refused |
| Revoked credential | `test_faults.py` | A revoked sync token stops replication without damage; a replacement resumes it |
| Version skew | `test_version_skew.py` | See below |
| Changes for a game never introduced (release-blocking) | `test_c13_live_regression.py` | The live failure: artwork for a game the authority knows only from a provider waits on every follower (not quarantined, cursor at head, state `waiting`); `atlas feed introduce --missing` introduces the game and the next sync applies game and artwork everywhere, the plain nodes through the mirror. |

### Version skew and the expected failures

A newer authority will emit things an older follower does not understand: new entry kinds (relationships, prices) and new observation fields (date precision). [Issue #33](https://github.com/obsoletelabs/unnamed_tracking_metadata/issues/33) tracks the missing compatibility gate. The lab has one release of the code, so it plays "newer" by signing such entries with the authority's real key (`LabNode.emit`).

Before the compatibility gate a follower logged and skipped such entries while its cursor moved past them. Now it **parks** them ([Compatibility and upgrades](federation.md#compatibility-and-upgrades)):

* `test_unknown_entry_kind_is_not_silently_dropped`: an unknown kind is parked (`unsupported_kind`); the cursor stops before it and the follower's status names it.
* `test_unknown_observation_field_is_not_silently_dropped`: an observation with an unknown payload field is parked (`unsupported_field`).
* `test_mirror_relays_newer_entries_verbatim`: a parked mirror still retains and relays the entry and everything after it byte for byte; the entry after it waits instead of being applied out of order.
* `test_skipping_a_parked_entry_lets_the_rest_apply_and_is_audited`: the operator escape hatch.
* A known kind from the same authority applies normally.

The assertions use `disposition()` and fail on `"dropped"`; "applied", "blocked" (cursor held back) and "recorded" (an open sync failure) pass.

The rewound-authority scenario (`test_rewound_authority_is_not_silently_ignored_by_an_up_to_date_follower`) now passes too: a follower compares the advertised head with the entries it already verified and reports a fork or rewind instead of success. Recovering from it is still #15 / #24.

A real mixed-version check against the previous release is the separate skew drill (v0 against wire 1): see [Release verification](release-verification.md#version-skew-drill).

For a real mixed-version network, build a second virtual environment from the older or newer release and run one node from it by setting `ATLAS_LAB_PYTHON_<NODE>` (for example `ATLAS_LAB_PYTHON_LOCAL=/path/to/older/venv/bin/python`) or passing `Lab(pythons={"local": "..."})`. That node's server then runs the other release; the lab itself, its direct database reads and its seeding still use the current code, so this suits releases with the same database schema.

## Writing a scenario

```python
import pytest

pytestmark = pytest.mark.federation_lab

def test_something(lab):                      # `lab` is a fresh, wired five-node network
    official, local = lab.nodes["official"], lab.nodes["local"]
    official.post("A Game", developer="Studio")
    lab.sync_all()                            # authorities -> mirror -> local and local2
    lab.assert_converged()
    official.stop(); official.start()         # take a node offline and bring it back
    local.pull(official, tamper=lambda path, body: body)   # real pull code, hostile network
```

Useful pieces on `LabNode`: `post`, `emit` (sign any entry), `follow`, `head`, `cursor`, `retained`, `own_feed`, `observations`, `canonicals`, `open_failures`, `digest`, `set_cursor`, `forget_retained`, `truncate_feed`, `revoke_token`, and `pull(..., tamper=...)` which runs the follower's real pull code against an upstream while rewriting the JSON it returns.

## In CI

`.github/workflows/federation-lab.yml` runs the lab nightly, on demand, and on pull requests that touch replication, signing, the feed models, migrations or the lab itself. It uses a PostgreSQL service for both authorities and uploads the JUnit report and every node's server log. The regular `ci.yml` run does not start the lab.

## Limits

* Transport is plain loopback HTTP in development mode; production TLS is not exercised.
* "Newer release" is simulated by signed entries until a second release exists; see above for running a real one.
* Push delivery, online promotion and live failover do not exist yet and have no scenarios.
* All data is synthetic. The users and credentials are generated per run and exist only in that run's databases and logs.
