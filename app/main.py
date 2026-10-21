"""Space Intelligence — FastAPI entry point.

    uvicorn app.main:app --reload
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.config import settings
from app.routes import data_api, site
from app.templating import templates


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Free hosting: fetch the database published by GitHub Actions (no-op unless SECTOR_INTEL_SYNC=github).
    from app.sync import start_background_sync  # noqa: PLC0415

    start_background_sync()
    yield


def create_app() -> FastAPI:
    app = FastAPI(
        lifespan=lifespan,
        title="Space Intelligence — API",
        description="Veille spatial et défense : articles, événements recoupés, scores expliqués, signaux, sources.",
        version="1.0.0",
    )
    app.mount("/static", StaticFiles(directory=str(settings.static_dir)), name="static")
    app.include_router(data_api.router)
    app.include_router(site.router)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException):
        if request.url.path.startswith(("/api", "/static")):
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        return templates.TemplateResponse(request, "site/error.html",
                                          {"active": "", "info": None, "status_code": exc.status_code, "detail": exc.detail},
                                          status_code=exc.status_code)

    return app


app = create_app()
