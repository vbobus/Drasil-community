"""
Drasil FastAPI Entry Point
--------------------------
Architecture: All routes live in app/api/. This file bootstraps the app only.
"""

import os
from time import perf_counter
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from app.db.hosted import hosted_runtime

from app.core.env import cors_origins, load_local_env
from app.api.auth import router as auth_router
from app.api.accounts import router as accounts_router
from app.api.admin_agent_architecture import router as admin_agent_architecture_router
from app.api.ai import recover_incomplete_strategist_runs, router as ai_router
from app.api.calendar_blocks import router as calendar_blocks_router
from app.api.calendar_integrations import router as calendar_integrations_router
from app.api.categories import router as categories_router
from app.api.companion import router as companion_router
from app.api.habits import router as habits_router
from app.api.nodes import router as nodes_router
from app.api.workspace import router as workspace_router
from app.api.planner import router as planner_router
from app.api.preferences import router as preferences_router
from app.api.recurrence_rules import router as recurrence_rules_router
from app.api.review_actions import router as review_actions_router
from app.api.scheduling import router as scheduling_router
from app.services.agent_kernel.contracts import ReviewActionError, ReviewActionOutcome
from app.services.agent_kernel.agent_observability import (
    agent_observability_status,
    shutdown_agent_observability,
)
from app.services.account_auth import AccountContextMiddleware, initialize_account_store

try:
    from app.private_extensions import routers as private_extension_routers
except ModuleNotFoundError as error:
    if error.name != "app.private_extensions":
        raise
    private_extension_routers = ()

load_local_env()

app = FastAPI(
    title="Drasil API",
    description="Local constraint-scheduling backend for the Drasil life planning OS.",
    version="0.1.0",
    docs_url=None if hosted_runtime() else "/docs",
    redoc_url=None if hosted_runtime() else "/redoc",
    openapi_url=None if hosted_runtime() else "/openapi.json",
)


@app.exception_handler(StarletteHTTPException)
async def safe_http_error(request: Request, error: StarletteHTTPException):
    detail = error.detail
    if hosted_runtime() and error.status_code >= 500:
        detail = "The request could not be completed. Please try again or check your provider settings."
    return JSONResponse(status_code=error.status_code, content={"detail": detail}, headers=error.headers)


@app.exception_handler(Exception)
async def safe_server_error(request: Request, error: Exception):
    return JSONResponse(status_code=500, content={"detail": "The request could not be completed."},
                        headers={"Cache-Control": "no-store"})


@app.exception_handler(RequestValidationError)
async def safe_request_validation_error(
    request: Request,
    error: RequestValidationError,
):
    if request.url.path.endswith("/review-actions"):
        outcome = ReviewActionOutcome(
            status="error",
            error=ReviewActionError(
                code="invalid_action",
                summary="This review action is incomplete or invalid.",
                recovery="none",
            ),
        )
        return JSONResponse(
            status_code=422,
            content=outcome.model_dump(mode="json", by_alias=True, exclude_none=True),
        )
    if hosted_runtime():
        return JSONResponse(status_code=422, content={"detail": "Some request fields are invalid."})
    return await request_validation_exception_handler(request, error)

app.add_middleware(AccountContextMiddleware)
if hosted_runtime():
    from app.core.vercel_context import VercelHeadersMiddleware
    app.add_middleware(VercelHeadersMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins(),
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Server-Timing", "X-Request-Id"],
)
from app.core.request_timing import RequestTimingMiddleware
app.add_middleware(RequestTimingMiddleware)

# ── Routers ───────────────────────────────────────────────────────────────────
app.include_router(categories_router)
app.include_router(companion_router)
app.include_router(habits_router)
app.include_router(nodes_router)
app.include_router(workspace_router)
app.include_router(calendar_blocks_router)
app.include_router(calendar_integrations_router)
app.include_router(scheduling_router)
app.include_router(planner_router)
app.include_router(preferences_router)
app.include_router(recurrence_rules_router)
app.include_router(ai_router)
app.include_router(review_actions_router)
app.include_router(auth_router)
app.include_router(accounts_router)
app.include_router(admin_agent_architecture_router)
for private_extension_router in private_extension_routers:
    app.include_router(private_extension_router)


# ── Startup ───────────────────────────────────────────────────────────────────
@app.on_event("startup")
def on_startup() -> None:
    """Create all DB tables and seed defaults on application startup."""
    started_at = perf_counter()
    if hosted_runtime():
        from app.services.private_settings import cipher
        cipher()
        if not os.getenv("DRASIL_FRONTEND_ORIGIN", "").startswith("https://"):
            raise RuntimeError("Hosted Drasil requires its HTTPS frontend origin.")
    initialize_account_store()
    from app.db.database import initialize_existing_account_databases

    initialize_existing_account_databases(recover_incomplete_strategist_runs)
    app.state.startup_ms = round((perf_counter() - started_at) * 1000, 2)


@app.on_event("shutdown")
def on_shutdown() -> None:
    """Flush queued agent traces before the API process exits."""
    shutdown_agent_observability()


# ── Health ────────────────────────────────────────────────────────────────────
@app.get("/api/v1/health", tags=["Health"])
def health_check() -> dict:
    """Simple health-check endpoint to verify the server is running."""
    if hosted_runtime():
        return {"status": "ok", "service": "drasil-api",
                "agentObservability": agent_observability_status()}
    return {
        "status": "ok",
        "service": "drasil-api",
        "buildId": os.getenv("AGENT_KERNEL_V2_BUILD_ID"),
        "sourceDigest": os.getenv("AGENT_KERNEL_SOURCE_DIGEST"),
        "agentObservability": agent_observability_status(),
    }


def configure_bundled_web_app() -> None:
    """Serve the production React build when backend and frontend share a container."""

    configured = os.getenv("DRASIL_WEB_DIST", "").strip()
    if not configured:
        return
    web_root = Path(configured).expanduser().resolve()
    index_path = web_root / "index.html"
    if not index_path.is_file():
        raise RuntimeError(f"DRASIL_WEB_DIST does not contain index.html: {web_root}")

    @app.get("/{web_path:path}", include_in_schema=False)
    def bundled_web_asset(web_path: str) -> FileResponse:
        requested = (web_root / web_path).resolve()
        if requested.is_relative_to(web_root) and requested.is_file():
            return FileResponse(requested)
        return FileResponse(index_path)


configure_bundled_web_app()
