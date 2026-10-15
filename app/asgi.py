from .main import app
from .tag_registry import router as tag_registry_router

app.include_router(tag_registry_router)

# Static paths must precede the {slug} route in Starlette's router.
priority = {
    "/api/v1/tag-registry/suggest": 0,
    "/api/v1/tag-registry/resolve": 1,
    "/api/v1/tag-registry/mappings": 2,
}
app.router.routes.sort(key=lambda route: priority.get(getattr(route, "path", ""), 3))
