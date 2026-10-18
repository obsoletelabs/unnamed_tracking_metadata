# The Unnamed Tracking App

Atlas works with the [Unnamed Tracking App](https://github.com/obsoletelabs/unnamed_tracking_app_2) in two independent ways. Use either or both.

## Metadata: the Atlas provider plugin

The plugin **`official.metadata-atlas`** (source in the plugins repository, pull request #64) adds Atlas as a metadata provider: search, merged metadata, artwork and a health check.

### Install

1. Run an Atlas node ([Quick start](quickstart.md)) and create a token with only the **`read`** scope (**Admin → Tokens**).
2. Get the plugin package. The plugin repository's branch CI publishes an unsigned preview (`unsigned-dist`); or build one: `python tools/build_packages.py` produces `official.metadata-atlas-<version>.utp`. Signed releases come from the official publisher.
3. In the app: **Plugin Manager** → install the `.utp` (unsigned previews need explicit consent) and grant the declared permissions.
4. Open the provider's settings and enter **Atlas server URL** (`https://atlas.example.org`, optional path prefix) and **Atlas API token** (write-only, stored encrypted by the app).

### What it does

| Operation | Behaviour |
| --- | --- |
| Search | `GET /games?q=…`; returns Atlas id plus the store ids Atlas has linked (`steam`, `igdb`…) |
| Metadata | by Atlas id, or for a candidate that only has other providers' ids, by those ids or exact title+year. **Only a certain match is accepted**; uncertain returns nothing |
| Media | artwork Atlas has already downloaded and verified, as public `<server>/media/<hash>` links |
| Health | validates the URL and token |

### Limits to know

* The app downloads artwork itself and refuses private addresses, so **artwork needs a publicly reachable Atlas**. Metadata works on a LAN.
* Achievements are not part of the app's provider contract yet. Atlas serves the achievement *list* (`GET /games/{id}/achievements`); there is no per-user progress anywhere in Atlas.
* Custom fields are available in the API (`custom`) but the app's metadata model has no slot for them.
* The plugin sends no personal data: only titles, years and provider ids.

## Signing in to the Tracking App with Atlas

**This needs no plugin.** The app has first-class OpenID Connect support (**Settings → Administration → Single sign-on**), and Atlas is a standard provider. A plugin can't add login methods (plugins have no login hook), and none is needed.

It has been verified with the app's own client stack: the same Authlib registration the app uses (`src/core/oidc.py`) completes a login against a live Atlas, receiving the ID token and claims (`tools/compat/tracking_app_oidc.py`).

### Step by step

**On Atlas**

1. Turn on **Act as a sign-in provider (OIDC)** in **Admin → Settings → Federation**. `ATLAS_PUBLIC_URL` must be the public `https://` address.
2. **Admin → Sign-in sharing → Register application:**
   * *Name:* `Unnamed Tracking App`
   * *Redirect addresses:* the exact callback the app shows, `https://<your-app>/api/auth/oidc/callback/<slug>` (choose the slug in step 3; e.g. `atlas`).
   * *Token signature:* **RS256**
   * *Require PKCE:* **un-tick**. The app's client authenticates with its secret and does not send PKCE.
   * Copy the **client id** and **secret**.

**In the Tracking App** (**Settings → Administration → Single sign-on → Add provider**)

| Field | Value |
| --- | --- |
| Provider name / slug | `Atlas` / `atlas` |
| Issuer / discovery URL | `https://atlas.example.org` |
| Client ID / secret | from Atlas |
| Scopes | `openid profile email` |
| Groups claim / admin group | leave empty (Atlas sends no groups) |
| User matching | **Username** if the same people use the same names, otherwise **Email** |
| Allow new users | your choice |
| Require verified email | **On** if you only want addresses people have confirmed (Atlas sets `email_verified` accordingly) |
| Login button | text such as “Continue with Atlas” |

### Things to expect

* The app requires an **email claim**: an Atlas user without an email address cannot sign in. Authority nodes require one anyway.
* Users see Atlas's consent screen once per sign-in, then land in the app.
* There is no group or admin mapping. Make administrators in the app.
* Signing out of the app does not sign out of Atlas, and vice versa.

If something fails, see [Sign-in sharing → Troubleshooting](sso.md#troubleshooting).

### Want this to be one click?

A preset “Add Atlas” template in the app's SSO settings (pre-filling scopes and matching) would be a small change **in the app itself**, not in a plugin. It is not done; it would be a pull request to the app repository.
