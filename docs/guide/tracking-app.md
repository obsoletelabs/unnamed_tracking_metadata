# The Unnamed Tracking App

Atlas works with the [Unnamed Tracking App](https://github.com/obsoletelabs/unnamed_tracking_app_2) in two independent ways. Use either or both.

## Metadata: the Atlas provider plugin

The plugin **`official.metadata-atlas`** adds Atlas as a provider for games, anime, movies and TV shows: typed search, merged metadata, achievements for games, artwork and a health check.

### Install

1. Run an Atlas node ([Quick start](quickstart.md)) and create a token with only the **`read`** scope (**Admin → Tokens**).
2. Get the signed package from the official catalogue. This integration's Atlas provider release is **0.6.0**; development builds from `python tools/build_packages.py` are isolated unsigned previews and require explicit consent.
3. In the app: **Plugin Manager** → install the `.utp` (unsigned previews need explicit consent) and grant the declared permissions.
4. Open the provider's settings and enter **Atlas server URL** (`https://atlas.example.org`, optional path prefix) and **Atlas API token** (write-only, stored encrypted by the app).

### What it does

| Operation | Behaviour |
| --- | --- |
| Search | `GET /api/v2/games?q=…&type=…`; searches the requested type (`game`, `anime`, `movie` or `tv_show`) and returns the Atlas ID plus every linked provider ID (`steam`, `igdb`, `tmdb`…) |
| Metadata | maps Atlas fields into the app's game, anime, movie or TV fields and forwards bounded custom values. A candidate without an Atlas ID is resolved by linked provider IDs or exact title and year, constrained to the same media type; uncertain matches are dropped |
| Achievements | for games, supplies definitions, descriptions, hidden flags, global percentages and cached icons. The app imports definitions without changing player progress, and can resolve Atlas records from linked IDs such as Steam. Non-game records do not request achievements |
| Media | lists only artwork Atlas has downloaded and verified. For anime, movies and TV, key art is exposed as a poster. The app fetches the public `<server>/media/<hash>` links itself |
| Health | checks the configured URL and read token |

The app combines Atlas with its other providers. The user's provider order sets the default winner when two sources give different values; fields that only one source has are retained. Image providers have their own order. Atlas's achievement catalogue is a separate resource, so the app can refresh definitions without replacing game metadata from Steam or another provider.

![Atlas game record with its cached artwork and achievement catalogue](img/achievements-desktop.png)
![Atlas artwork grouped by media role](img/gallery-desktop.png)

### Contract tests

`tests/test_tracking_app_contract.py` covers the plugin's read-only token, search variations, lookup by Atlas ID, resolving a candidate that only has other provider IDs, metadata and achievement fields, artwork before and after caching, and tokenless public `/media/<sha256>` downloads. It also covers the incremental change endpoint below. An Atlas change that breaks these client requests fails this contract suite.

### Incremental client updates

Client apps that cache Atlas records can poll the signed change feed through a simpler read-only endpoint:

```http
GET /api/v2/changes?since=120&limit=100
Authorization: Bearer <read-scope-token>
```

```json
{
  "node_id": "atlas-node-id",
  "head": 124,
  "next_cursor": 124,
  "has_more": false,
  "results": [
    {"id": "atlas:0123456789abcdefghijklmnop", "version": 8, "merged_into": null},
    {"id": "atlas:1123456789abcdefghijklmnop", "version": 5, "merged_into": "atlas:2123456789abcdefghijklmnop"}
  ]
}
```

`since` is the last `next_cursor` stored by the client. Continue with that cursor while `has_more` is true; the page limit is 1–500 feed events. The result list deduplicates canonical IDs touched in that page and reports each record's current version. A non-null `merged_into` identifies a redirect, so clients should refresh the destination record too. Events that do not affect a canonical record still advance the cursor. The optional `node_id` selects a replicated authority feed retained by this Atlas node; omit it to poll this node's own feed.

The route requires the `read` scope. It returns IDs and versions only; clients fetch changed records, achievements and artwork through their usual endpoints.


### Limits to know

* The app downloads artwork itself and refuses private addresses, so artwork needs a publicly reachable Atlas. Metadata works on a LAN.
* Atlas serves achievement definitions but never player progress; unlock state belongs to each app user.
* The plugin sends no personal data: only titles, years and provider IDs.

### Verified host workflow

The [release drill](release-verification.md) installs the unchanged signed `.utp` through the
real Plugin Manager, approves its exact declared permissions, starts the runtime worker and
uses ordinary host accounts. It checks secret redaction/system-configuration denial, search,
canonical/store identifiers, metadata enrichment, achievement definitions, per-user search-session ownership, game creation,
the metadata-refresh endpoint, manually protected fields and an unreachable Atlas.
Host, runtime and Atlas communicate over real HTTP; gateways and provider replies are not
mocked. The container runtime uses its documented `NONBUBBLE_ENV` development fallback, so
this is functional acceptance, not verification of production sandbox isolation. The existing
refresh response labels a focused local record **Library** even when Atlas supplied fields.

The drill exercises the signed package with a real local stack. The incremental client route is
covered by Atlas contract tests and does not change the plugin's request flow.

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
