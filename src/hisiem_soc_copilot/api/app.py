"""FastAPI application factory (transport only).

The app wires: lifespan (open/close container), exception handlers, health probe,
and the API routers. It contains no business logic, SQL, prompts, or graph nodes.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from ..bootstrap.container import Container
from ..config import Settings
from .errors import register_exception_handlers
from .routers.investigations import router as investigations_router


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the FastAPI application bound to a Container.

    ``settings`` is injected for tests; production uses ``get_settings()``.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        container = Container(settings if settings is not None else _settings())
        await container.open()
        app.state.container = container
        try:
            yield
        finally:
            await container.close()

    app = FastAPI(title="HISIEM SOC Copilot", version="0.1.0", lifespan=lifespan)
    register_exception_handlers(app)

    @app.get("/healthz", tags=["ops"])
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "service": "hisiem-soc-copilot"}

    app.include_router(investigations_router)
    return app


def _settings() -> Settings:
    from ..config import get_settings

    return get_settings()
