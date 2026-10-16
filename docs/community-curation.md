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

## Presentation selection

Consumers that need one image can use:

`GET /api/v1/games/{canonical_id}/presentation?role=cover&aspect_ratio=16:9`

The selector prefers an exact requested ratio, then the same orientation, then the closest numerical ratio. Community score is used after aspect-ratio suitability, with stable vote/time/id tie-breakers. An image is returned as `preferred` only when its community score is positive; otherwise the response makes the lack of a community preference explicit and still exposes alternatives.

This lets clients request a 16:9 cover, 21:9 banner, portrait asset, or another layout without forcing the catalogue to store one universal image.

## Voting

A verified user has at most one vote per image. `1` upvotes, `-1` downvotes, and `0` removes the user's vote:

`POST /api/v1/images/{image_id}/vote`

Preferred selection is deterministic: the highest community score wins within its image role, with upvotes and then creation order used as stable tie-breakers. A candidate must have a positive score before it can become the preferred image, so an unvoted image never silently wins a community choice.

API community mutations require an email-verified account; browser community actions already require the same verification state.

## Metadata alternatives

Community alternatives are stored separately from the canonical record so an integration cannot silently overwrite trusted metadata. Supported candidate fields are `title`, `developer`, `description`, `release_year`, and `custom_fields`.

Create an alternative with:

`POST /api/v1/games/{canonical_id}/metadata-options`

List alternatives with:

`GET /api/v1/games/{canonical_id}/metadata-options`

Vote with:

`POST /api/v1/metadata-options/{candidate_id}/vote`

The same one-vote-per-user rule applies. A metadata candidate becomes the preferred community value only after it has a positive net score. The preferred value is exposed in `preferred_metadata` on the normal game representation, while the canonical stored field remains unchanged until a separate curation/review workflow chooses to publish it.

## Reporting and moderation

Community content can be reported without directly hiding or rewriting the item. This is intentional: a single malicious report should not be able to suppress a legitimate image or metadata candidate.

Report an image or metadata candidate with:

`POST /api/v1/community/reports`

using exactly one of `image_id` or `candidate_id`, plus one of these reasons:

- `copyright`
- `spam`
- `abuse`
- `wrong-game`
- `misleading`
- `unsafe`
- `other`

Each verified user can keep at most one open report against a particular item. Administrators can review the open queue at `/admin/community/reports` and resolve or dismiss reports with an audit note. Report status and moderation state are deliberately separate from community voting, so reporting creates a review signal rather than silently manipulating the ranking.

## API permissions

Reading candidates uses `read`. Adding image candidates uses `images`; proposing metadata alternatives, voting, and reporting use `write` and require email verification. Moderation reports contain private moderation state and therefore use the signed-in administrator session rather than bearer-token access for the admin queue. Health checks remain public. API tokens are stored as hashes and votes cannot be duplicated for the same user/candidate pair because the database enforces unique constraints.
