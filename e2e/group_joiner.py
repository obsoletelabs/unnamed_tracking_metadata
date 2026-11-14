"""A synthetic joining node for the node-group evidence screenshots (e2e/tests/group.evidence.spec.js).

It runs the real joiner side of hello and reveal (CONTRACT-C10 §2.3) against the e2e node with a key generated
here at runtime, and prints {"enrolment_id", "sas"} as JSON, so the inviter's SAS screen can be captured.
`complete` then runs the joiner's last step (§2.3 complete) once the inviter has issued the certificate, so the
inviter's enrolment becomes active (U3: the done step's account-link offer). The throwaway joiner key is kept in the
git-ignored e2e/.run folder between the two calls; it is not a running node, so it never answers anything itself.
Usage (repository root): uv run python e2e/group_joiner.py <inviter url> <code> <name>
                         uv run python e2e/group_joiner.py complete <inviter url> <enrolment id>
"""

from __future__ import annotations

import base64
import json
import secrets
import sys
from pathlib import Path

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from atlas import group_crypto as gc
from atlas.signing import canonical_json, key_id_for

STATE = Path(__file__).resolve().parent / ".run" / "joiner"


def main(url: str, code: str, name: str) -> int:
    invite = gc.decode_code(code)
    key = Ed25519PrivateKey.generate()
    pub = gc.ed_public_b64(key)
    node_id = key_id_for(base64.b64decode(pub))
    member = {"node_id": node_id, "root_public_key": pub, "box_public_key": gc.x_public_b64(X25519PrivateKey.generate()),
              "url": f"http://{name.lower().replace(' ', '-')}-{node_id[:4]}.invalid", "name": name, "rev": 1}
    nonce_j = secrets.token_bytes(32)
    hello = {"v": 1, "invite_id": invite.invite_id, "group_id": invite.group_id, "issued_at": gc.stamp(),
             "joiner": {"node_id": node_id, "root_public_key": pub, "signing_key_id": node_id, "rotations": [],
                        "member": member, "member_pop": gc.member_pop(key, invite.group_id, member, node_id),
                        "atlas_protocol": "1.1", "group_protocol": "1.0"},
             "commit_j": gc.commitment(nonce_j)}
    t1 = canonical_json(hello)
    body = {**hello, "pop": gc.enrol_pop(key, "joiner-hello", gc.sha256_hex(t1)),
            "mac": gc.enrol_mac(invite.k, "joiner-hello", t1)}
    with httpx.Client(timeout=15, trust_env=False) as client:
        reply = client.post(f"{url}/api/v2/group/enrol/hello", json=body)
        reply.raise_for_status()
        data = reply.json()
        eid, th = data["enrolment_id"], data["transcript_hash"]
        mac = gc.enrol_mac(invite.k, "joiner-reveal", f"{th}\n{eid}\n{gc.b64(nonce_j)}".encode())
        client.post(f"{url}/api/v2/group/enrol/reveal",
                    json={"enrolment_id": eid, "nonce_j": gc.b64(nonce_j), "mac": mac}).raise_for_status()
    STATE.mkdir(parents=True, exist_ok=True)
    raw = key.private_bytes_raw()
    (STATE / f"{eid}.json").write_text(json.dumps({"k": gc.b64(invite.k), "th": th, "key": gc.b64(raw)}))
    print(json.dumps({"enrolment_id": eid, "sas": gc.sas(invite.k, th, nonce_j), "node_id": node_id}))
    return 0


def complete(url: str, eid: str) -> int:
    state = json.loads((STATE / f"{eid}.json").read_text())
    k, th = base64.b64decode(state["k"]), state["th"]
    key = Ed25519PrivateKey.from_private_bytes(base64.b64decode(state["key"]))
    body = {"enrolment_id": eid, "pop": gc.enrol_pop(key, "joiner-complete", th, eid),
            "mac": gc.enrol_mac(k, "joiner-complete", f"{th}\n{eid}".encode())}
    with httpx.Client(timeout=15, trust_env=False) as client:
        reply = client.post(f"{url}/api/v2/group/enrol/complete", json=body)
    print(json.dumps({"status": reply.status_code}))
    return 0 if reply.status_code == 200 else 1


if __name__ == "__main__":
    if sys.argv[1] == "complete":
        raise SystemExit(complete(*sys.argv[2:4]))
    raise SystemExit(main(*sys.argv[1:4]))
