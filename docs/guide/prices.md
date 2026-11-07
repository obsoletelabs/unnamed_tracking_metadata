# Prices

Atlas can keep **price observations**: what an offer cost in one storefront region at one moment. Prices are
stored apart from a record's metadata. They never change a record's view or version. This release never
publishes them to followers.

This release ships the model, the API, retention and the purge. **It does not fetch prices from any store.**
Prices come in through `POST /api/v2/prices`, from a curator token or an import you run yourself.

## What a price observation holds

```json
{"canonical_id": "atlas:…", "provider": "steam", "product_id": "620", "package_id": null,
 "edition": null, "platform": "pc", "region": "us", "currency": "USD",
 "amount_minor": 999, "base_amount_minor": 1999, "discount_pct": 50,
 "availability": "available", "observed_at": "2026-10-31T12:00:00Z", "valid_until": "2026-11-07T17:00:00Z",
 "source_kind": "provider", "provenance": {"method": "api", "fetch_id": "run-42"},
 "license": {"redistributable": false, "terms": "steam-web-api"}, "retention_days": 30}
```

| Field | Rule |
| --- | --- |
| `canonical_id` | The record the offer sells. A merged record's id is followed to the surviving record. |
| `provider`, `product_id`, `package_id`, `edition`, `platform` | Together they name the **offer**. `provider` and `platform` are lower-case slugs. Product and package ids are up to 128 printable characters. |
| `region` | An ISO 3166-1 alpha-2 code in lower case (`us`, `jp`, `kw`). There is no worldwide `ww`, because a price is always regional. |
| `currency` | An active ISO 4217 code in upper case (`USD`, `JPY`, `KWD`). Metal and test codes (`XAU`, `XTS`) and withdrawn codes (`HRK`) are refused. |
| `amount_minor` | What a buyer pays **now**, as an integer in the currency's minor units. It is required when the offer is `available` or a `preorder`. A `free` offer has 0. |
| `base_amount_minor` | The **list** price, in the same units. It can never be below `amount_minor`. |
| `discount_pct` | 0–100. Atlas derives it from the two amounts. If you send it, it must agree with them within one point. |
| `availability` | One of `available`, `unavailable`, `preorder`, `free`, `unreleased`, `delisted` or `unknown`. |
| `observed_at` | When it was seen. This needs a time zone, and it cannot be more than five minutes ahead of the node's clock. |
| `valid_until` | When the price ends (a sale's end). Optional. It must be after `observed_at`. |
| `source_kind` | `provider` (a store fetch) or `manual` (the default). |
| `provenance` | Optional `node`, `method` and `fetch_id`. No other keys are accepted. |
| `license` | `redistributable` (default `false`) and the short name of the `terms`. |
| `retention_days` | 1–365, default 30. This is how long the observation is kept after `observed_at`. |

### Money is integers

Every amount is an integer number of the currency's **minor units**. The ISO 4217 exponent of the currency
says what that means:

| Currency | Exponent | `amount_minor` | Means |
| --- | --- | --- | --- |
| `USD` | 2 | `999` | 9.99 |
| `JPY` | 0 | `1980` | ¥1980 (there is no smaller unit) |
| `KWD` | 3 | `1500` | 1.500 |
| `CLF` | 4 | `12345` | 1.2345 |

These requests are rejected with a 422, and nothing in the batch is stored:

* a float, even a whole one (`999.0`);
* a string amount (`"999"`);
* a negative amount, or an amount above 10^15.

Answers repeat each amount as an exact decimal string (`"amount": "9.99"`) together with the `exponent`.
Those strings are for display. Calculate with the integers.

### Sale or list price

`amount_minor` is the current price and `base_amount_minor` the list price. Answers mark an observation with:

* `sale: true` when the current price is below the list price;
* `stale: true` once `valid_until` has passed.

### Editions, DLC and bundles

* **An edition of a game** (Deluxe, GOTY) is priced on the game's record, with `edition` set to the edition's
  name. Editions are not records; see [Relationships and editions](relations.md).
* **A DLC or an expansion** is priced on its own record, which is the record its `dlc_of` / `expansion_of`
  relation starts from.
* **A bundle** is priced on its Collection record.

## Storing prices

`POST /api/v2/prices` needs the `curate` scope. It takes `{"prices": [...]}`, with up to 500 per request. If
any item is invalid (wrong shape, currency, region, amounts or discount), the whole request is a 422 and
nothing is stored. Otherwise each item reports one of these statuses:

| Status | Meaning |
| --- | --- |
| `stored` | New observation. |
| `duplicate` | The same offer at the same `observed_at` with the same facts is already stored. Nothing changes, so sending a batch twice is safe. |
| `conflict` | The same offer at the same `observed_at` is stored with other amounts or availability. The stored one is kept. |
| `unknown_record` | No record has that id. |
| `expired` | `observed_at + retention_days` has already passed. |
| `invalid` | `observed_at` is in the future. |

## Reading prices

* `GET /api/v2/games/{id}/prices[?region=us&currency=USD&provider=steam]` returns the newest observation of
  every offer of the record. It is ordered by provider, region and currency, and holds at most 500 offers
  (`truncated` says more exist).
* `GET /api/v2/games/{id}/prices/history?region=us` returns the price history in one region, newest first.
  You can narrow it with `currency`, `provider`, `product_id`, `edition`, `platform`, `since` and `until`.
  `limit` is at most 1000. To read further, pass `next_cursor` as `cursor` until it is null. The order is
  `observed_at`, then id, so pages never overlap or skip. When you give a `currency`, `lowest` is the lowest
  retained price that matches the filters, across all pages. This is the historical low, as far back as
  retention reaches.

Both endpoints need the `read` scope. They follow merge redirects (`redirected_from`).

Rows whose licence does not allow redistribution (`license.redistributable: false`) are returned only to
`curate` and `admin` tokens. A read-only token sees only redistributable prices.

## Retention and purge

Every observation is deleted `retention_days` after its `observed_at`. Reads hide an expired row
immediately. The server's housekeeping deletes expired rows once an hour, 5000 rows per statement. To purge
now, or to see what is stored:

```bash
atlas prices purge     # {"purged": 1234}
atlas prices status    # counts, rows due for purge, retention bounds, channel state
```

`GET /api/v2/prices/status` (admin) returns the same status.

Prices are included in backups like every other table.

## Federation: protocol 1.6, not enabled in this release

Prices will replicate over a **separate signed channel**, not through the main feed. The main feed is
contiguous and kept forever, while prices change often and expire. The design (CONTRACTS-v1 C8.3) is fixed
and registered in `atlas/feed_schema.py` (`PRICE_CHANNEL`):

| | |
| --- | --- |
| Protocol | 1.6 (1.5 until the C13 renumbering, H52), capability `price-channel-v1` |
| Endpoint | `/api/v2/sync/price-changes?since=` with its own sequence (`price_feed_seq`) |
| Signature | The feed's message layout under the domain `atlas-price-v1`, so a price signature never verifies as a feed entry |
| Follower cursor | `<node>#price` |
| Pruning | A follower behind the oldest retained entry gets `{"reset": true, "snapshot_url": …}` and re-reads the latest-per-offer snapshot |
| What is sent | Only rows with `license.redistributable: true` |

In this release:

* `/sync/info` does not list `price-channel-v1`, and the node still speaks protocol 1.4;
* `/api/v2/sync/price-changes` answers 404;
* nothing price-related is signed or appended to the feed, at any `emit_protocol`;
* the feed kind `price` is **reserved**. If one ever reached the main feed, a follower of this release would
  park it (`unsupported_kind`) instead of dropping it, and this node refuses to sign one.

Followers of any earlier release see nothing new: the feed they read is unchanged.
