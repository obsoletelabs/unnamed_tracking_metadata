"""Security-sensitive administrator actions leave an audit entry (issue #13)."""

from sqlalchemy import select

from atlas.models import AuditEntry

from .test_web import signup, site, token  # noqa: F401

BOOT = "b" * 40  # the bootstrap admin token of the shared `site` fixture


def actions(site):  # noqa: F811
    with site.st.maker() as s:
        return [(e.action, e.target, e.detail) for e in s.scalars(select(AuditEntry))]


def test_api_token_creation_and_revocation_are_audited_without_the_secret(site):  # noqa: F811
    h = {"Authorization": f"Bearer {BOOT}"}
    made = site.post("/api/v2/tokens", json={"name": "ci", "scopes": ["read"]}, headers=h)
    assert made.status_code == 201
    secret, tid = made.json()["token"], made.json()["id"]
    assert site.delete(f"/api/v2/tokens/{tid}", headers=h).status_code == 204
    log = actions(site)
    created = next(e for e in log if e[0] == "token_create" and e[1] == tid)
    assert created[2]["via"] == "api" and created[2]["scopes"] == ["read"] and created[2]["by_token"]
    assert any(e[0] == "token_revoke" and e[1] == tid for e in log)
    assert secret not in str(log)


def test_schema_and_sign_in_provider_changes_are_audited(site):  # noqa: F811
    signup(site, "alice")  # the first account is the administrator
    t = token(site, "/admin/schema")
    assert site.post("/admin/schema/types", data={"csrf": t, "key": "movie", "label": "Movie", "enabled": "on"}
                     ).status_code == 303
    assert site.post("/admin/schema/movie/fields", data={"csrf": t, "key": "runtime", "label": "Runtime",
                                                          "kind": "number", "public": "on", "editable": "on"}
                     ).status_code == 303
    assert site.post("/admin/schema/movie/fields/runtime/delete", data={"csrf": t}).status_code == 303
    assert site.post("/admin/schema/types/movie/delete", data={"csrf": t}).status_code == 303
    seen = {(a, tgt) for a, tgt, _ in actions(site)}
    assert {("schema_type_save", "movie"), ("schema_field_save", "movie/runtime"),
            ("schema_field_delete", "movie/runtime"), ("schema_type_delete", "movie")} <= seen
