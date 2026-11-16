from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import text

from news import __version__
from news.api import admin, client, items, sources
from news.connectors.http import SafeHttp
from news.db.engine import dispose_engine, engine
from news.env import env
from news.streams import redis_client

MAX_BODY_BYTES = 5 * 1024 * 1024


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    app.state.redis = redis_client()
    client = httpx.AsyncClient()
    app.state.http = SafeHttp(client, env().user_agent)
    try:
        yield
    finally:
        await client.aclose()
        await app.state.redis.aclose()
        await dispose_engine()


def create_app() -> FastAPI:
    app = FastAPI(
        title="News API",
        version=__version__,
        description="Topic and location news intelligence. Admin endpoints need an API key "
                    "with a console role (viewer, moderator, admin) as `Authorization: Bearer`.",
        openapi_url="/v1/openapi.json",
        docs_url="/v1/docs",
        redoc_url=None,
        lifespan=lifespan,
    )

    @app.middleware("http")
    async def limit_body(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        length = request.headers.get("content-length")
        if length and length.isdigit() and int(length) > MAX_BODY_BYTES:
            return JSONResponse({"detail": "Request body too large"}, status_code=413)
        return await call_next(request)

    @app.get("/healthz", tags=["health"])
    async def healthz() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/readyz", tags=["health"])
    async def readyz(request: Request) -> JSONResponse:
        checks: dict[str, bool] = {}
        try:
            async with engine().connect() as conn:
                await conn.execute(text("SELECT 1"))
            checks["db"] = True
        except Exception:
            checks["db"] = False
        try:
            checks["redis"] = bool(await request.app.state.redis.ping())
        except Exception:
            checks["redis"] = False
        ok = all(checks.values())
        return JSONResponse({"ok": ok, **checks}, status_code=200 if ok else 503)

    app.include_router(client.router)
    app.include_router(sources.router)
    app.include_router(items.router)
    app.include_router(admin.router)
    return app
