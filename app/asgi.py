from .main import app
from .tag_registry import router as tag_registry_router

app.include_router(tag_registry_router)

# Keep the fixed-name API endpoints ahead of the generic single-tag route.
priority = {"/api/v1/tag-registry/resolve": 0, "/api/v1/tag-registry/mappings": 1}
app.router.routes.sort(key=lambda route: priority.get(getattr(route, "path", ""), 2))
