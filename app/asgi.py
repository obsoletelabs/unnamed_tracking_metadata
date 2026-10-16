from .main import app
from .tag_registry import router as tag_registry_router
from . import community, community_admin, community_browser, community_moderation, curation

app.include_router(tag_registry_router)
app.include_router(community.router)
app.include_router(curation.router)
app.include_router(community_browser.router)
app.include_router(community_moderation.router)
app.include_router(community_admin.router)

# The main app already owns the browser-only /api/v1/games/{id}/images POST shape in older builds.
# Remove the duplicate community POST route and expose its JSON integration endpoint at /images/add.
app.router.routes = [
    route for route in app.router.routes
    if not (getattr(route, "path", "") == "/api/v1/games/{canonical_id}/images"
            and getattr(route, "endpoint", None) is community.add_image)
]
app.add_api_route("/api/v1/games/{canonical_id}/images/add", community.add_image, methods=["POST"], status_code=201)

# Static paths must precede generic parameter routes in Starlette's router.
priority = {
    "/api/v1/tag-registry/suggest": 0,
    "/api/v1/tag-registry/resolve": 1,
    "/api/v1/tag-registry/mappings": 2,
    "/api/v1/games/{canonical_id}/images": 0,
    "/api/v1/games/{canonical_id}/metadata-options": 1,
    "/api/v1/games/{canonical_id}/images/add": 0,
    "/api/v1/games/{canonical_id}/presentation": 0,
    "/api/v1/games/{canonical_id}/community-summary": 0,
    "/api/v1/games/{canonical_id}/community-leaderboard": 0,
    "/api/v1/community/reports": 0,
    "/api/v1/community/reports/{report_id}/resolve": 0,
}
app.router.routes.sort(key=lambda route: priority.get(getattr(route, "path", ""), 3))
