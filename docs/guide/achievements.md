# Achievements

Atlas keeps the **list** of achievements for a game: what exists, how rare it is, what it looks like. It never stores anyone's progress; the Tracking App (or any client) keeps who unlocked what and matches it to this list using the ids below.

## What an achievement has

| Field | Meaning |
|---|---|
| `id` | Atlas id of the entry. Stable while the entry exists. |
| `name`, `description` | Text. Up to 200 / 2000 characters. |
| `hidden` | A secret achievement. See [Hidden achievements](#hidden-achievements). |
| `kind` | `progression`, `missable` or `win_condition`; absent when unknown. |
| `tier` | `bronze`, `silver`, `gold`; absent when the source has none. |
| `points` | Whole number 0–100000 (Steam has none, RetroAchievements and Xbox use them). |
| `global_percent` | Share of all players who have it, 0–100. |
| `icon_url`, `locked_icon_url` | Atlas's own verified copy of the unlocked / locked image (`/api/v2/media/<sha256>`), or `null` while it is not cached yet. |
| `external_ids` | The achievement's id at each source, for example `{"steam": "ACH_WIN_1"}`. Clients use this to match their own unlock data. |
| `sources` | Which sources contributed. |

Achievements from different sources are merged by normalised name. The highest-trust source wins each field; lower ones only fill gaps. A provider id is never assumed to match another provider's achievement with a different name.

## On the website

The game page lists achievements with icon, kind, tier, points and global percentage. Hidden ones show as "Hidden achievement" with a `?` icon.

![Achievements on a game page](img/achievements-desktop.png)

Signed-in users add achievements as `Name | Description | extras`; extras are recognised by value in any order: `progression` / `missable` / `win condition`, `bronze` / `silver` / `gold`, a number (points), `hidden`, an icon address (a second address is the locked icon).

## Reading

`GET /api/v2/games/{id}/achievements` (scope `read`):

```json
{
  "id": "01J…", "redirected_from": null, "hidden_count": 1,
  "results": [
    {"id": "01J…", "name": "Escape", "description": "Leave the house", "hidden": false, "revealed": false,
     "kind": "progression", "tier": "gold", "points": 50, "global_percent": 3.5,
     "icon_url": "/api/v2/media/<sha256>", "locked_icon_url": null,
     "external_ids": {"steam": "ACH_ESCAPE"}, "sources": ["steam"]}
  ]
}
```

Relative image paths are resolved against the Atlas base URL. Origin image addresses are never returned, so clients cannot be made to hotlink a third-party site. Images need a publicly reachable Atlas for the Tracking App to download them (as for artwork).

## Hidden achievements

A hidden achievement is returned **redacted**: `name` is `"Hidden achievement"`, `description` is empty, no icon, no `external_ids`, `revealed: false`. Kind, tier, points and percentage stay, because they do not spoil anything. If *any* source marks an achievement hidden it stays hidden.

To reveal, add `?reveal=all` or `?reveal=<id>,<id>` (Atlas ids). Atlas honours it only for a **signed-in user's personal token** or a token with the `curate` scope. Any other token, and the public website, still gets redacted rows. A client should ask for the ids the user has already unlocked, so spoilers are not fetched for achievements the user has not earned. Revealed rows have `revealed: true`.

## Contributing

* Curators: `PUT /api/v2/games/{id}/achievements` with `{"items": [...]}` replaces the curator source's list. Every field above (except `id`, `sources`, `external_ids`) can be sent; `external_id` is the id at that source; `icon_url` / `locked_icon_url` must be `http(s)` addresses and are downloaded by Atlas's artwork cache under the same safety rules (public addresses only, content-type and size checks).
* Everyone signed in: **Add achievements** on the game page creates a proposal that goes through the normal review (reputation rules, revert).

## Host app contract

The Tracking App's Atlas plugin calls `GET /api/v2/games/{id}/achievements` (default, redacted) while it handles the normal metadata operation and forwards the entries as the app's `achievements` metadata patch. Mapping:

| Atlas | App |
|---|---|
| `id` | external id of the entry |
| `icon_url` / `locked_icon_url` | icon address (resolved against the Atlas base URL, then fetched by the app's own image pipeline) |
| `global_percent` | `rarityPercent` |
| `tier` | `tierOverride` |
| `kind`, `hidden`, `revealed`, `name`, `description`, `points` | same names |
| `external_ids` | lets the app match a user's synced progress (Steam id, RetroAchievements id) to the entry |

Entries that are hidden and not revealed are stored by the app as placeholders and not shown on shared surfaces. No per-user data is sent to Atlas. Reveal-by-id is available in Atlas for clients that want it; the app does not use it in this release.
