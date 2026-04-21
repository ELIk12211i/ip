# -*- coding: utf-8 -*-
"""
server/app/main.py
------------------
FastAPI application entry point.

Run with:
    cd server
    python -m uvicorn app.main:app --reload

Endpoints:
    /health                — liveness probe
    /license/*             — client-facing, open (no auth)
    /admin/login           — session-based admin login
    /admin/*               — Jinja-rendered admin dashboard
    /admin/api/*           — JSON admin API (session-protected)
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import HTTPException as FastAPIHTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .auth import RedirectToLogin, purge_expired_sessions
from .database import DB_PATH, init_db
from .routes import admin_api, admin_pages, checkout, licenses, webhooks


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)


app = FastAPI(
    title="Magnet Frame Pro — License Server",
    version="2.0.0",
)


# ---------------------------------------------------------------------------
# CORS — permissive for /license/* so the desktop client can reach it.
# ---------------------------------------------------------------------------
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Static files (shared by admin UI). The frontend agent owns the contents of
# server/app/static/admin/.
# ---------------------------------------------------------------------------
_STATIC_DIR = Path(__file__).parent / "static"
_STATIC_DIR.mkdir(parents=True, exist_ok=True)
(_STATIC_DIR / "admin").mkdir(parents=True, exist_ok=True)

app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")


# ---------------------------------------------------------------------------
# Jinja templates — the same instance the admin_pages router uses. Exposed
# on app.state for any downstream consumer.
# ---------------------------------------------------------------------------
templates = Jinja2Templates(
    directory=str(Path(__file__).parent / "templates")
)
app.state.templates = templates


# ---------------------------------------------------------------------------
# Routers
# ---------------------------------------------------------------------------
app.include_router(licenses.router, prefix="/license", tags=["license"])
app.include_router(checkout.router, prefix="/checkout", tags=["checkout"])
app.include_router(webhooks.router, prefix="/webhook", tags=["webhook"])
app.include_router(admin_api.router, prefix="/admin/api", tags=["admin-api"])
app.include_router(admin_pages.router, prefix="/admin", tags=["admin-pages"])


# ---------------------------------------------------------------------------
# Startup / health / redirects
# ---------------------------------------------------------------------------
@app.on_event("startup")
def _on_startup() -> None:
    init_db()
    purge_expired_sessions()
    logger.info("License DB ready at %s", DB_PATH)


@app.get("/health")
def health():
    """Lightweight liveness probe."""
    return {"ok": True, "service": "license-server", "version": "2.0.0"}


@app.get("/", include_in_schema=False)
def root():
    """Redirect the bare root to the admin dashboard (or login)."""
    return RedirectResponse("/admin/", status_code=302)


# ---------------------------------------------------------------------------
# Custom handler — admin page routes raise RedirectToLogin when the caller
# is unauthenticated; convert it into a 302 response transparently.
# ---------------------------------------------------------------------------
@app.exception_handler(RedirectToLogin)
async def _redirect_to_login(_request: Request, exc: RedirectToLogin):
    return RedirectResponse(exc.path, status_code=302)


# The page-level auth helper uses HTTPException(303, Location=...) as a cheap
# redirect trigger; keep the generic behaviour but make sure that when we see
# status_code == 303 and a Location header, we emit an actual redirect (FastAPI
# already does this, but the explicit handler keeps things tidy).
@app.exception_handler(FastAPIHTTPException)
async def _http_exception(request: Request, exc: FastAPIHTTPException):
    if (
        exc.status_code in (302, 303)
        and exc.headers
        and exc.headers.get("Location")
    ):
        return RedirectResponse(
            exc.headers["Location"], status_code=exc.status_code
        )
    # Default fallback — mimic FastAPI's default.
    from fastapi.responses import JSONResponse
    return JSONResponse(
        {"detail": exc.detail}, status_code=exc.status_code,
        headers=exc.headers,
    )
