"""Load synthetic data into the node started by e2e/server.py. Every title, name and text here is invented.

Writes e2e/.seed.json (git-ignored) with the generated catalogue ids so the tests can link straight to
records. Run from the repository root:  uv run python e2e/seed.py
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent
PORT = int(os.environ.get("E2E_PORT", "8765"))
BASE = f"http://127.0.0.1:{PORT}"
BOOTSTRAP_TOKEN = os.environ.get("E2E_BOOTSTRAP_TOKEN", "e2e-synthetic-bootstrap-token-0123456789abcdef")
PASSWORD = os.environ.get("E2E_PASSWORD", "e2e-Synthetic-Pass-12345")  # synthetic, only valid on the throwaway node
USERS = ("alice", "bob", "carol")  # alice is ATLAS_INITIAL_ADMIN

GAMES = [
    ("Starlight Harbor", "2021-03-12", "Lantern Forge", "An exploration game about restoring a lighthouse town.",
     ["exploration", "cozy"], ["adventure"]),
    ("Cinder Circuit", "2019-10-04", "Bytewright", "A roguelike where every run rewires the dungeon.",
     ["roguelike", "pixel-art"], ["action"]),
    ("Paper Meridian", "2023-06-20", "Quill Collective", "Narrative puzzle game folded from a single sheet of paper.",
     ["puzzle", "story-rich"], ["puzzle"]),
    ("Hollow Orchard", "2017-09-29", "Mossbark Games", "Farm sim with a quiet mystery underneath.",
     ["farming", "mystery"], ["simulation"]),
    ("Voltage Drift", "2022-11-08", "Redline Labs", "Arcade racing on magnetic tracks.",
     ["racing", "arcade"], ["racing"]),
    ("The Long Quiet", "2015-02-17", "Northwind Studio",
     "Survival in a silent frozen city. " + "Silent frozen streets and long nights. " * 12, ["survival"], ["adventure"]),
]
ACHIEVEMENT_NAMES = ["First Light", "Keeper of the Flame", "Lost and Found", "Night Owl", "Cartographer", "Collector"]


def csrf_of(html: str) -> str:
    match = re.search(r'name="csrf" value="([^"]+)"', html)
    return match.group(1) if match else ""


def api(client: httpx.Client, method: str, path: str, token: str, **kw) -> httpx.Response:
    r = client.request(method, "/api/v2" + path, headers={"Authorization": f"Bearer {token}"}, **kw)
    if r.status_code >= 300:
        sys.exit(f"seed: {method} {path} -> {r.status_code} {r.text[:300]}")
    return r


def web_post(client: httpx.Client, path: str, **data) -> httpx.Response:
    data["csrf"] = csrf_of(client.get("/me").text)
    r = client.post(path, data=data)
    if r.status_code >= 400:
        sys.exit(f"seed: POST {path} -> {r.status_code} {r.text[:200]}")
    return r


def main() -> None:
    ids: dict[str, str] = {}
    with httpx.Client(base_url=BASE, timeout=30, follow_redirects=False) as api_client:
        token = api(api_client, "POST", "/tokens", BOOTSTRAP_TOKEN,
                    json={"name": "e2e-seed", "scopes": ["read", "contribute", "curate", "admin", "sync"]}).json()["token"]
        for i, (title, date, developer, description, tags, genres) in enumerate(GAMES):
            observation = {"external_id": f"E2E{1000 + i}", "title": title, "release_date": date, "platforms": ["PC"],
                           "facts": {"developer": developer, "publisher": developer, "description": description,
                                     "tags": tags, "genres": genres, "critic_score": 60 + i * 5}}
            r = api(api_client, "POST", "/observations", token,
                    json={"connection": {"source_kind": "manual", "source": "manual", "source_scope": "e2e"},
                          "media_type": "game", "observations": [observation]})
            ids[title] = r.json()["results"][0]["atlas"]["canonical_id"]
        r = api(api_client, "POST", "/observations", token,
                json={"connection": {"source_kind": "manual", "source": "manual", "source_scope": "e2e"},
                      "media_type": "movie", "observations": [
                          {"external_id": "E2E-M1", "title": "Midnight Lanterns", "release_date": "2019-11-02",
                           "facts": {"description": "A courier crosses a city during a lantern festival.", "genres": ["drama"]}}]})
        ids["Midnight Lanterns"] = r.json()["results"][0]["atlas"]["canonical_id"]
        achievements = [{"name": name, "description": f"Synthetic achievement {k + 1}.", "hidden": k == 4,
                         "tier": ["bronze", "silver", "gold"][k % 3], "points": 5 * (k + 1), "global_percent": round(60 / (k + 1), 1)}
                        for k, name in enumerate(ACHIEVEMENT_NAMES)]
        api(api_client, "PUT", f"/games/{ids['Starlight Harbor']}/achievements", token, json={"items": achievements})

    sessions: dict[str, httpx.Client] = {}
    try:
        for user in USERS:
            c = httpx.Client(base_url=BASE, timeout=30, follow_redirects=False)
            sessions[user] = c
            r = c.post("/register", data={"csrf": csrf_of(c.get("/register").text), "username": user, "password": PASSWORD,
                                          "email": f"{user}@example.invalid"})
            if r.status_code >= 400:
                sys.exit(f"seed: register {user} -> {r.status_code} {r.text[:200]}")
        web_post(sessions["bob"], "/new", media_type="game", title="Moss and Mortar", release_date="2026-02-02",
                 developer="Fernlight", publisher="Fernlight", description="A pending proposal for a new game.",
                 platforms="PC", tags="building\ncozy", features="", note="Announced on their blog (synthetic).")
        web_post(sessions["alice"], f"/game/{ids['Starlight Harbor']}/comments", body="Lovely game, synthetic comment.")
        web_post(sessions["alice"], "/me/lists", name="Cozy favourites", description="Synthetic list", visibility="public")
    finally:
        for c in sessions.values():
            c.close()
    (HERE / ".seed.json").write_text(json.dumps({"ids": ids}, indent=1), encoding="utf-8")
    print(f"seeded {len(ids)} records")


if __name__ == "__main__":
    main()
