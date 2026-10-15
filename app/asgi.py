from .main import app
from .tag_registry import router as tag_registry_router
from .community import router as community_router

app.include_router(tag_registry_router)
app.include_router(community_router)

# Static paths must precede generic parameter routes in Starlette's router.
priority = {
    "/api/v1/tag-registry/suggest": 0,
    "/api/v1/tag-registry/resolve": 1,
    "/api/v1/tag-registry/mappings": 2,
    "/api/v1/games/{canonical_id}/images": 0,
    "/api/v1/games/{canonical_id}/metadata-options": 1,
}
app.router.routes.sort(key=lambda route: priority.get(getattr(route, "path", ""), 3))
