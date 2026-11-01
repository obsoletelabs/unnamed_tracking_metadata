"""A single-database owner-group world for trust tests: this node holds the root; other members are synthetic
identities whose keys are generated at runtime. Objects other nodes would sign are built with their keys."""

from __future__ import annotations

import base64
from datetime import datetime, timedelta

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from atlas import group_crypto as gc
from atlas import groups
from atlas.db import make_sessionmaker
from atlas.models import Base
from atlas.signing import NodeIdentity, body_hash, key_id_for, load_identity

from .conftest import build_settings
from .group_harness import REC_PW, ROOT_PW

SECRET = "synthetic-test-secret-world-0123456789abcdef"


def synth() -> NodeIdentity:
    key = Ed25519PrivateKey.generate()
    pub = gc.ed_public_b64(key)
    return NodeIdentity(node_id=key_id_for(base64.b64decode(pub)), private=key, root_public_b64=pub)


def rotation(old: NodeIdentity, root_id: str, seq: int, new: Ed25519PrivateKey) -> dict:
    body = {"key_id": gc.key_id_of(gc.ed_public_b64(new)), "public_key": gc.ed_public_b64(new)}
    digest = body_hash(body)
    signer = NodeIdentity(node_id=root_id, private=old.private)
    return {"seq": seq, "kind": "key_rotation", "body": body, "body_hash": digest,
            "signature": signer.sign(seq, "key_rotation", digest), "key_id": old.key_id, "node_id": root_id}


class World:
    def __init__(self, monkeypatch, recovery: int = 1) -> None:
        monkeypatch.setattr(gc, "SCRYPT_N", 2 ** 10)
        monkeypatch.setattr(gc, "SCRYPT_MIN_N", 2 ** 10)
        self.settings = build_settings(secret_key=SECRET, public_url="http://me.test", app_name="Me")
        engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(engine)
        self.engine = engine
        self.maker = make_sessionmaker(engine, self.settings)
        self.s = self.maker()
        self.me = load_identity(self.s, self.settings)
        created = groups.create_group(self.s, self.settings, self.me, "World", ROOT_PW, REC_PW[:recovery],
                                      threshold=1 if recovery == 1 else 2)
        self.files = created.recovery_files
        groups.confirm_recovery(self.s, self.settings, created.group, REC_PW[0])
        self.s.commit()
        self.group = created.group
        self.count = 0

    @property
    def gid(self) -> str:
        return self.group.group_id

    def root(self) -> Ed25519PrivateKey:
        return groups.unlock_root(self.s, self.settings, self.group, ROOT_PW)

    def recovery_keys(self, passphrases=None) -> list[Ed25519PrivateKey]:
        passphrases = passphrases or REC_PW
        return [gc.open_recovery_file(f, p).recovery for f, p in zip(self.files, passphrases, strict=False)]

    def member(self, ident: NodeIdentity, rev: int = 1, url: str | None = None, name: str | None = None,
               box: str | None = None) -> dict:
        self.count += 1
        tag = ident.node_id[:6]
        return {"node_id": ident.node_id, "root_public_key": ident.trust_public_b64,
                "box_public_key": box or gc.x_public_b64(gc.X25519PrivateKey.generate()),
                "url": url or f"http://{tag}.test", "name": name or f"Node {tag}", "rev": rev}

    def pop(self, ident: NodeIdentity, member: dict) -> dict:
        return gc.member_pop(ident.private, self.gid, member, ident.key_id)

    def enrol(self, ident: NodeIdentity, scopes=None, delegate=None, member=None, readmit=None,
              now: datetime | None = None) -> dict:
        member = member or self.member(ident)
        return groups.issue_cert(self.s, self.group, self.me, member, self.pop(ident, member),
                                 list(scopes or groups.DEFAULT_SCOPES), delegate, signer_root=self.root(),
                                 days=30 if delegate else 365, readmit=readmit, now=now)

    def delegate(self, ident: NodeIdentity, max_members: int = 2) -> dict:
        return self.enrol(ident, [*groups.DEFAULT_SCOPES, "group.enrol"], groups.default_delegate(max_members))

    def node_cert(self, issuer: NodeIdentity, target: NodeIdentity, n: int, parent: str | None, *, member=None,
                  pop=None, scopes=None, delegate=None, days: float = 20, start: datetime | None = None,
                  serial: str | None = None, body_issuer: str | None = None, readmit=None) -> dict:
        start = start or groups.utcnow()
        member = member or self.member(target)
        serial = serial or f"{issuer.node_id}/{n}"
        body = {"serial": serial, "issuer": body_issuer or issuer.node_id, "parent_serial": parent,
                "member": {**member, "url_verified": False}, "member_pop": pop or self.pop(target, member),
                "scopes": list(scopes or groups.DEFAULT_SCOPES), "delegate": delegate, "readmit": readmit,
                "issued_at": gc.stamp(start), "not_before": gc.stamp(start),
                "not_after": gc.stamp(start + timedelta(days=days)), "enrolment": None}
        return gc.make_object("group.cert", self.gid, f"cert:{serial}", body,
                              {"kind": "node", "id": issuer.node_id, "key_id": issuer.key_id}, issuer.private)

    def root_cert(self, target: NodeIdentity, n: int, *, member=None, days: float = 365, start=None,
                  root: Ed25519PrivateKey | None = None, root_key_id: str | None = None, r: str | None = None) -> dict:
        start = start or groups.utcnow()
        member = member or self.member(target)
        serial = f"root/{n}-{r or '0123456789abcdef'}"
        body = {"serial": serial, "issuer": "root", "parent_serial": None, "member": {**member, "url_verified": False},
                "member_pop": self.pop(target, member), "scopes": list(groups.DEFAULT_SCOPES), "delegate": None,
                "readmit": None, "issued_at": gc.stamp(start), "not_before": gc.stamp(start),
                "not_after": gc.stamp(start + timedelta(days=days)), "enrolment": None}
        return gc.make_object("group.cert", self.gid, f"cert:{serial}", body,
                              {"kind": "root", "id": self.gid, "key_id": root_key_id or self.group.root_key_id},
                              root or self.root())

    def ingest(self, obj: dict, now: datetime | None = None) -> str:
        out = groups.ingest_cert(self.s, self.group, obj, now or groups.utcnow())
        groups.rebuild(self.s, self.group, self.me.node_id, now)
        return out

    def crl(self, signer: NodeIdentity | None, entries: list[dict], seq: int, root=None, issued=None,
            root_key_id: str | None = None) -> dict:
        issuer = "root" if signer is None else signer.node_id
        body = {"issuer": issuer, "crl_seq": seq, "issued_at": gc.stamp(issued or groups.utcnow()), "entries": entries}
        if signer is None:
            who = {"kind": "root", "id": self.gid, "key_id": root_key_id or self.group.root_key_id}
            key = root or self.root()
        else:
            who, key = {"kind": "node", "id": signer.node_id, "key_id": signer.key_id}, signer.private
        return gc.make_object("group.crl", self.gid, f"crl:{issuer}/{seq}", body, who, key)

    def apply(self, crl: dict, now: datetime | None = None) -> str:
        out = groups.ingest_crl(self.s, self.group, crl, now or groups.utcnow())
        groups.rebuild(self.s, self.group, self.me.node_id, now)
        return out

    def trust(self, now: datetime | None = None) -> groups.Trust:
        return groups.trust(self.s, self.group, now, own_node=self.me.node_id)

    def verdict(self, obj: dict, now: datetime | None = None) -> str:
        v = self.trust(now).valid(obj)
        return "ok" if v.ok else v.reason

    def succession(self, index: int, new_root: Ed25519PrivateKey, signers: list[Ed25519PrivateKey], *,
                   carry: list[dict] | None = None, last_n: int | None = None, last_crl=None, delay: int = 24,
                   recovery: dict | None = None, previous: str | None = None) -> dict:
        tr = self.trust()
        carry = carry if carry is not None else [{"serial": c["body"]["serial"], "body_hash": c["body_hash"]}
                                                 for c in tr.members().values() if c["signer"]["kind"] == "root"]
        body = {"index": index, "previous_root_key_id": previous or self.group.root_key_id,
                "new_root_public_key": gc.ed_public_b64(new_root),
                "new_root_key_id": gc.key_id_of(gc.ed_public_b64(new_root)),
                "recovery": recovery or self.group.recovery, "activation_delay_hours": delay, "carry": carry,
                "old_root_last_serial_n": last_n if last_n is not None else groups.next_root_n(self.s, self.gid) - 1,
                "old_root_last_crl": last_crl or {"crl_seq": 0, "body_hash": "none"}, "issued_at": gc.stamp()}
        first = signers[0]
        obj = gc.make_object("group.succession", self.gid, f"succession:{index}", body,
                             {"kind": "recovery", "id": self.gid, "key_id": gc.key_id_of(gc.ed_public_b64(first))},
                             first)
        obj["cosigs"] = [{"key_id": gc.key_id_of(gc.ed_public_b64(k)), "sig": gc.co_sign(obj, k)} for k in signers[1:]]
        obj["new_root_pop"] = gc.co_sign(obj, new_root)
        return obj
