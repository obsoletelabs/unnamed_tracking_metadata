from .main import app
from .tag_registry import router as tag_registry_router

app.include_router(tag_registry_router)
