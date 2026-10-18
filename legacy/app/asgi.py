from .main import app, engine
from .api_contract import install_api_contract
from . import authority  # noqa: F401
from .authority_api import router as authority_router
from . import identity  # noqa: F401
from . import media  # noqa: F401
from .media import router as media_router
from . import federation  # noqa: F401
from .federation_api import router as federation_router
from . import jobs  # noqa: F401
from . import search_api  # noqa: F401
from . import security_audit  # noqa: F401
from . import observability  # noqa: F401
from .observability import install_request_metrics, router as observability_router
from .search_api import router as search_router
from .migrations import run_migrations
from .tag_registry import router as tag_registry_router
from .identity_api import router as identity_router
from . import community, community_admin, community_browser, community_moderation, curation

app.router.on_startup = [handler for handler in app.router.on_startup if getattr(handler, "__name__", "") != "startup"]
app.router.on_startup.append(run_migrations)
install_api_contract(app, engine)
install_request_metrics(app)

app.include_router(observability_router)
app.include_router(authority_router)
app.include_router(federation_router)
app.include_router(tag_registry_router)
app.include_router(identity_router)
app.include_router(media_router)
app.include_router(search_router)
app.include_router(community.router)
app.include_router(curation.router)
app.include_router(community_browser.router)
app.include_router(community_moderation.router)
app.include_router(community_admin.router)

app.router.routes = [
    route for route in app.router.routes
    if not (getattr(route, "path", "") == "/api/v1/games/{canonical_id}/images"
            and getattr(route, "endpoint", None) is community.add_image)
]
app.add_api_route("/api/v1/games/{canonical_id}/images/add", community.add_image, methods=["POST"], status_code=201)

priority = {
    "/healthz": 0,
    "/readyz": 0,
    "/metrics": 0,
    "/api/v1/authority": 0,
    "/api/v1/authority/mappings": 1,
    "/api/v1/authority/status": 0,
    "/api/v1/tag-registry/suggest": 0,
    "/api/v1/tag-registry/resolve": 1,
    "/api/v1/tag-registry/mappings": 2,
    "/api/v1/search": 0,
    "/api/v1/search/budget": 0,
    "/api/v1/games/{canonical_id}/images": 0,
    "/api/v1/games/{canonical_id}/metadata-options": 1,
    "/api/v1/games/{canonical_id}/images/add": 0,
    "/api/v1/games/{canonical_id}/presentation": 0,
    "/api/v1/games/{canonical_id}/community-summary": 0,
    "/api/v1/games/{canonical_id}/community-leaderboard": 0,
    "/api/v1/community/reports": 0,
    "/api/v1/community/reports/{report_id}/resolve": 0,
    "/api/v1/media/{sha256}": 0,
}
app.router.routes.sort(key=lambda route: priority.get(getattr(route, "path", ""), 3))
