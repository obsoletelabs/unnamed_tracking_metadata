# Community metadata and image curation

The metadata service supports multiple image candidates and community-voted metadata alternatives for every game.

## Images

Images are independent candidates. A game can have any number of images, and each image can declare a role and aspect ratio:

- `cover`
- `banner`
- `background`
- `screenshot`
- `logo`
- `icon`
- `thumbnail`
- `other`

Width and height automatically produce a reduced aspect ratio such as `16:9`; callers can provide an explicit ratio such as `21:9` when dimensions are unavailable.

Existing browser uploads remain supported. API integrations can add URL-backed candidates with:

`POST /api/v1/games/{canonical_id}/images/add`

and retrieve all candidates with:

`GET /api/v1/games/{canonical_id}/images`

The response groups candidates by role and also returns every distinct aspect ratio. Each candidate exposes `upvotes`, `downvotes`, `score`, `my_vote`, and `preferred`.

## Voting

A user has at most one vote per image. `1` upvotes, `-1` downvotes, and `0` removes the user's vote:

`POST /api/v1/images/{image_id}/vote`

Preferred selection is deterministic: the highest community score wins within its image role, with upvotes and then creation order used as stable tie-breakers. A candidate must have a positive score before it can become the preferred image, so an unvoted image never silently wins a community choice.

## Metadata alternatives

Community alternatives are stored separately from the canonical record so an integration cannot silently overwrite trusted metadata. Supported candidate fields are `title`, `developer`, `description`, `release_year`, and `custom_fields`.

Create an alternative with:

`POST /api/v1/games/{canonical_id}/metadata-options`

List alternatives with:

`GET /api/v1/games/{canonical_id}/metadata-options`

Vote with:

`POST /api/v1/metadata-options/{candidate_id}/vote`

The same one-vote-per-user rule applies. A metadata candidate becomes the preferred community value only after it has a positive net score. The preferred value is exposed in `preferred_metadata` on the normal game representation, while the canonical stored field remains unchanged until a separate curation/review workflow chooses to publish it.

## API permissions

Reading candidates uses `read`. Adding image candidates uses `images`; proposing metadata alternatives and voting use `write`. Health checks remain public. API tokens are stored as hashes and votes cannot be duplicated for the same user/candidate pair because the database enforces unique constraints.
