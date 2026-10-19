"""Install the published Atlas package on an explicitly disposable Tracking App stack.

The caller owns provisioning/cleanup. Only public HTTP APIs and the supplied, verified .utp
are used; no host source imports or substituted gateway/network responses are involved.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx

PLUGIN = "official.metadata-atlas"
PROVIDER = PLUGIN + ".atlas"


def checked(client, method, path, expected=200, **kwargs):
    response = client.request(method, path, **kwargs)
    # Never include response bodies or request values in failures: configuration contains secrets.
    assert response.status_code == expected, f"{method} {path}: expected {expected}, got {response.status_code}"
    return response.json() if response.content else None


def wait_for(action, timeout=30):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        result = action()
        if result:
            return result
        time.sleep(0.2)
    raise AssertionError("timed out waiting for public API state")


def run(args):
    password = os.environ["TRACKING_APP_TEST_PASSWORD"]
    atlas_admin = os.environ["ATLAS_TEST_ADMIN_TOKEN"]
    report = {"started_at": datetime.now(UTC).isoformat(), "checks": [], "package": args.package.name,
              "package_sha256": hashlib.sha256(args.package.read_bytes()).hexdigest()}
    checks = report["checks"]
    with httpx.Client(base_url=args.host_url, timeout=35, trust_env=False) as admin, \
            httpx.Client(base_url=args.host_url, timeout=35, trust_env=False) as regular, \
            httpx.Client(base_url=args.atlas_url, timeout=20, trust_env=False,
                         headers={"Authorization": f"Bearer {atlas_admin}"}) as atlas:
        checked(admin, "POST", "/api/auth/login", json={"username_or_email": args.username, "password": password})
        installed = checked(admin, "GET", "/api/plugins")
        assert not installed, "use a fresh disposable host with no installed plugins"
        blob = args.package.read_bytes()
        preview = checked(admin, "POST", "/api/plugins/install/preview",
                          files={"file": (args.package.name, blob, "application/octet-stream")})
        assert preview["plugin_id"] == PLUGIN and preview["signature_verified"] and preview["installable"]
        permissions = [item["key"] for item in preview["permissions"]]
        report.update(package_digest=preview["digest"], publisher_key_id=preview["publisher_key_id"])
        checked(admin, "POST", "/api/plugins/install", expected=201,
                params=[*(('approved_permissions', key) for key in permissions), ('confirm_dangerous', 'true')],
                data={"admin_password": password}, files={"file": (args.package.name, blob, "application/octet-stream")})
        def running():
            rows = checked(admin, "GET", "/api/plugins")
            row = next(item for item in rows if item["plugin_id"] == PLUGIN)
            return row if row["status"] == "running" and row["health"] == "healthy" else None

        wait_for(running)
        checks.append("published signed package preview, explicit permissions, installation and a real healthy worker")
        credential = checked(atlas, "POST", "/api/v2/tokens", expected=201,
                             json={"name": "tracking-app-drill", "scopes": ["read"]})
        token = credential["token"]
        suffix = secrets.token_hex(4)
        title = "Atlas Acceptance " + suffix
        payload = {"connection": {"source_kind": "manual", "source": "manual", "source_scope": "tracking-app-drill"},
                   "observations": [{"external_id": suffix, "title": title, "release_date": "2020-01-02",
                                     "provider_ids": {"gog": "drill-" + suffix},
                                     "facts": {"developer": "Atlas Original Developer", "tags": ["Puzzle"],
                                               "description": "Public curated acceptance metadata."}}]}
        result = checked(atlas, "POST", "/api/v2/observations", json=payload)
        cid = result["results"][0]["atlas"]["canonical_id"]

        def configure(url, credential=token):
            return checked(admin, "PUT", f"/api/metadata/providers/{PROVIDER}/configuration",
                           json={"scope": "system", "values": {"base_url": url, "api_token": credential}})

        configure(args.atlas_peer_url)
        username, user_password = "atlas_user_" + suffix, secrets.token_urlsafe(32) + "Aa1!"
        checked(admin, "POST", "/api/auth/users", expected=201,
                json={"username": username, "email": f"{username}@example.test", "password": user_password})
        checked(regular, "POST", "/api/auth/login", json={"username_or_email": username, "password": user_password})
        providers = wait_for(lambda: (data if any(p["provider_id"] == PROVIDER for p in data["providers"]) else None)
                             if (data := checked(regular, "GET", "/api/metadata/providers")) else None)
        assert token not in json.dumps(providers)
        choices = {p["provider_id"]: p["provider_id"] == PROVIDER for p in providers["providers"]}
        checked(regular, "PATCH", "/api/preferences", json={"metadata_providers_enabled": choices})
        checked(regular, "PUT", f"/api/metadata/providers/{PROVIDER}/configuration", expected=403,
                json={"scope": "system", "values": {"base_url": args.atlas_peer_url}})
        assert regular.get("/api/plugins/manager-settings").status_code == 403
        checks.append("ordinary host user inherits configuration without seeing the token or obtaining administrator access")
        session = checked(regular, "POST", "/api/metadata/sessions", expected=201,
                          json={"query": title, "media_type": "game"})
        sid = session["id"]

        def enriched():
            data = checked(regular, "GET", f"/api/metadata/sessions/{sid}")
            return next((row for row in data["results"] if row.get("provider_ids", {}).get("atlas") == cid
                         and row.get("metadata", {}).get("developer") == "Atlas Original Developer"), None)

        candidate = wait_for(enriched)
        assert candidate["provider_ids"]["gog"] == "drill-" + suffix
        checked(regular, "POST", f"/api/metadata/sessions/{sid}/selection", json={"candidate_id": candidate["id"]})
        assert admin.get(f"/api/metadata/sessions/{sid}").status_code == 404
        checks.append("real search, canonical/store identifiers, enrichment, selection and per-account session ownership")
        game = checked(regular, "POST", "/api/game/create", expected=201,
                       json={"title": title, "folder_location": "atlas_drill_" + suffix,
                             "provider_ids": {"atlas": cid, "gog": "drill-" + suffix}})
        gid = game["id"]
        refreshed = checked(regular, "POST", f"/api/game/{gid}/metadata/refresh",
                            json={"update_text": True, "fill_missing_art": False})
        assert refreshed["status"] == "updated", refreshed["status"]
        report["refresh_provider_label"] = refreshed["provider"]
        game = checked(regular, "GET", f"/api/game/get/{gid}")
        assert game["developer"] == "Atlas Original Developer" and "Puzzle" in game["tags"]
        checked(regular, "PATCH", f"/api/game/update/{gid}", json={"developer": "My Protected Developer"})
        payload["observations"][0]["facts"]["developer"] = "Atlas Updated Developer"
        checked(atlas, "POST", "/api/v2/observations", json=payload)
        checked(regular, "POST", f"/api/game/{gid}/metadata/refresh",
                json={"update_text": True, "fill_missing_art": False})
        assert checked(regular, "GET", f"/api/game/get/{gid}")["developer"] == "My Protected Developer"
        checks.append("existing game refresh consumes Atlas while preserving manually locked fields")
        configure("http://127.0.0.1:9")
        checked(regular, "POST", f"/api/game/{gid}/metadata/refresh", json={"fill_missing_art": False})
        assert checked(regular, "GET", f"/api/game/get/{gid}")["developer"] == "My Protected Developer"
        configure(args.atlas_peer_url)
        checked(regular, "DELETE", f"/api/metadata/sessions/{sid}", expected=204)
        checks.append("unreachable Atlas degrades metadata work without breaking local game reads or protected data")
        feed = checked(atlas, "GET", "/api/v2/sync/changes")
        assert username not in json.dumps(feed) and user_password not in json.dumps(feed)
        report["canonical_id"] = cid
    report["status"] = "passed"
    report["limits"] = ["loopback/container HTTP; production TLS and browser layout not exercised",
                        "runtime uses the documented NONBUBBLE_ENV development fallback inside disposable containers",
                        "public artwork download, achievements and live external provider calls not exercised"]
    return report


def main_cli():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host-url", required=True)
    parser.add_argument("--atlas-url", required=True, help="Atlas URL reachable by this tool")
    parser.add_argument("--atlas-peer-url", required=True, help="same Atlas URL reachable from the plugin runtime")
    parser.add_argument("--package", required=True, type=Path)
    parser.add_argument("--username", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--disposable", action="store_true", required=True, help="confirm this host is disposable")
    args = parser.parse_args()
    report = run(args)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main_cli()
