"""Application composition root.

Assembles configuration, the portal adapter, the snapshot store and the HTTP surface, and
installs the exception handlers that guarantee every error a client sees follows one shape.

Startup deliberately does **not** fail if the portal is unreachable. A wrapper that refuses
to boot during an upstream outage cannot serve `/health/live`, cannot be inspected, and
turns a portal blip into a crash-loop. Instead we log, come up, and report the problem
through `/health/ready` - which is what an orchestrator should act on.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.cors import CORSMiddleware

from .api.middleware import RequestContextMiddleware, SecurityHeadersMiddleware
from .api.v1.router import api_router
from .config import Settings, get_settings
from .errors import (
    ApiError,
    NotFoundError,
    UpstreamAuthError,
    UpstreamError,
    UpstreamRateLimitedError,
    UpstreamTimeoutError,
    UpstreamUnavailableError,
    ValidationError,
)
from .logging_setup import configure_logging, get_request_id
from .portal.client import UrjaPortalClient
from .portal.exceptions import (
    PortalAuthError,
    PortalError,
    PortalNotFound,
    PortalProtocolError,
    PortalRateLimited,
    PortalSessionExpired,
    PortalTimeout,
    PortalUnavailable,
)
from .portal.session import PortalSession
from .store.snapshot import SnapshotStore

logger = logging.getLogger(__name__)

DESCRIPTION = """
A clean, documented REST API over the legacy **Urja Meter Ops** portal.

The portal has no API. This service logs in as a normal user, manages the session,
reverse-engineers the portal's internal endpoints, normalises its inconsistent payloads,
and exposes the result as typed JSON that another program can consume without ever
touching the portal.

**What it adds over the portal**

* Filtering, sorting and pagination the portal cannot do (make, status, phase, build,
  any hierarchy code).
* The network hierarchy, reconstructed from per-meter paths into a queryable tree.
* Real consumption, derived by differencing the portal's cumulative registers.
* Proximity search over meter coordinates.
* An explicit data-quality report instead of silently tidied-away inconsistencies.

See `PROTOCOL.md` for how the portal actually works, and `README.md` for design decisions
and trade-offs.
"""

TAGS_METADATA = [
    {
        "name": "Meters",
        "description": "Meter listing, detail, consumption and proximity search.",
    },
    {"name": "Hierarchy", "description": "The reconstructed Zone->DT network tree."},
    {"name": "Transformers", "description": "Distribution transformer registry."},
    {"name": "System", "description": "Health, readiness, snapshot control and data quality."},
]


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings: Settings = app.state.settings
    configure_logging(settings.log_level, settings.log_format)

    if not settings.has_credentials:
        # Loud, actionable, and does not leak the values themselves.
        logger.error(
            "PORTAL_USERNAME/PORTAL_PASSWORD are not set - every upstream call will fail. "
            "Copy .env.example to .env and fill them in."
        )

    http = httpx.AsyncClient(
        timeout=httpx.Timeout(
            connect=settings.portal_connect_timeout,
            read=settings.portal_read_timeout,
            write=settings.portal_read_timeout,
            pool=settings.portal_read_timeout,
        ),
        limits=httpx.Limits(
            max_connections=settings.portal_max_connections,
            max_keepalive_connections=settings.portal_max_connections,
        ),
        follow_redirects=False,  # redirects to /login are a signal, not something to follow
        headers={"user-agent": f"{settings.app_name}/{settings.app_version}"},
    )
    session = PortalSession(
        http,
        settings.portal_base_url,
        settings.portal_username,
        settings.portal_password.get_secret_value(),
        refresh_margin_seconds=settings.session_refresh_margin_seconds,
    )
    client = UrjaPortalClient(
        http,
        session,
        settings.portal_base_url,
        max_retries=settings.portal_max_retries,
        backoff_base=settings.portal_backoff_base,
        backoff_max=settings.portal_backoff_max,
        max_concurrency=settings.portal_max_concurrency,
    )
    store = SnapshotStore(
        client,
        ttl_seconds=settings.snapshot_ttl_seconds,
        consumption_ttl_seconds=settings.consumption_cache_ttl_seconds,
        consumption_max_entries=settings.consumption_cache_max_entries,
    )

    app.state.http = http
    app.state.portal_session = session
    app.state.portal_client = client
    app.state.store = store

    if settings.snapshot_refresh_on_start and settings.has_credentials:
        try:
            await store.get()
        except PortalError as exc:
            logger.error(
                "initial snapshot build failed; starting anyway",
                extra={"error": f"{type(exc).__name__}: {exc}"},
            )

    try:
        yield
    finally:
        await http.aclose()


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, settings.log_format)

    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        description=DESCRIPTION,
        openapi_tags=TAGS_METADATA,
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
        contact={"name": "Flock Energy take-home submission"},
        license_info={"name": "MIT"},
    )
    app.state.settings = settings

    # Order matters: request context is outermost so its ID is available to everything.
    app.add_middleware(SecurityHeadersMiddleware)
    if settings.cors_allow_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_allow_origins,
            allow_methods=["GET", "POST"],
            allow_headers=["*"],
            expose_headers=["X-Request-ID", "X-Snapshot-Age-Seconds", "X-Snapshot-Source"],
        )
    app.add_middleware(RequestContextMiddleware)

    app.include_router(api_router, prefix=settings.api_prefix)
    _install_exception_handlers(app)
    _use_real_error_schema(app)

    @app.get("/", include_in_schema=False)
    async def root() -> dict[str, Any]:
        return {
            "service": settings.app_name,
            "version": settings.app_version,
            "documentation": "/docs",
            "openapi": "/openapi.json",
            "api": settings.api_prefix,
            "health": "/api/v1/health/live",
        }

    return app


def _use_real_error_schema(app: FastAPI) -> None:
    """Replace FastAPI's advertised 422 body with the envelope we actually return.

    FastAPI hardcodes ``HTTPValidationError`` (``{"detail": [...]}``) for validation
    failures, but our handler returns ``{"error": {...}}``. Left uncorrected the spec
    lies, and generated clients fail to deserialise every 422 they receive.
    """
    original_openapi = app.openapi

    def openapi() -> dict[str, Any]:
        schema = original_openapi()
        ref = {"$ref": "#/components/schemas/ErrorResponse"}
        for operations in schema.get("paths", {}).values():
            for operation in operations.values():
                response = operation.get("responses", {}).get("422")
                if response and "content" in response:
                    response["content"]["application/json"]["schema"] = ref
        # FastAPI's own validation schemas are now unreferenced.
        for unused in ("HTTPValidationError", "ValidationError"):
            schema.get("components", {}).get("schemas", {}).pop(unused, None)
        app.openapi_schema = schema
        return schema

    app.openapi = openapi


def _install_exception_handlers(app: FastAPI) -> None:
    """Map every failure mode onto the single public error shape.

    The mapping is the contract: portal faults become 502/503/504 and never leak upstream
    HTML or stack traces, while genuine caller mistakes stay in the 4xx range.
    """

    @app.exception_handler(ApiError)
    async def handle_api_error(_: Request, exc: ApiError) -> JSONResponse:
        if exc.status_code >= 500:
            logger.error("api error", extra={"code": exc.code, "detail": exc.message})
        return JSONResponse(
            status_code=exc.status_code, content=exc.to_payload(get_request_id())
        )

    @app.exception_handler(PortalError)
    async def handle_portal_error(_: Request, exc: PortalError) -> JSONResponse:
        mapped = _map_portal_error(exc)
        logger.error(
            "upstream portal failure",
            extra={"portal_error": type(exc).__name__, "detail": str(exc)},
        )
        headers = {}
        if isinstance(mapped, UpstreamRateLimitedError) and mapped.retry_after_seconds:
            headers["Retry-After"] = str(int(mapped.retry_after_seconds))
        return JSONResponse(
            status_code=mapped.status_code,
            content=mapped.to_payload(get_request_id()),
            headers=headers,
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        error = ValidationError(
            "One or more request parameters were invalid.",
            details={"fields": _summarise_validation(exc)},
        )
        return JSONResponse(status_code=422, content=error.to_payload(get_request_id()))

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_exception(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        error = (
            NotFoundError(str(exc.detail))
            if exc.status_code == 404
            else ApiError(str(exc.detail))
        )
        error.status_code = exc.status_code
        if exc.status_code != 404:
            error.code = f"http_{exc.status_code}"
        return JSONResponse(
            status_code=exc.status_code, content=error.to_payload(get_request_id())
        )

    @app.exception_handler(Exception)
    async def handle_unexpected(_: Request, exc: Exception) -> JSONResponse:
        # Last line of defence: log everything, tell the client nothing exploitable.
        logger.exception("unhandled exception", extra={"error_type": type(exc).__name__})
        return JSONResponse(status_code=500, content=ApiError().to_payload(get_request_id()))


def _map_portal_error(exc: PortalError) -> ApiError:
    if isinstance(exc, PortalRateLimited):
        return UpstreamRateLimitedError(retry_after_seconds=exc.retry_after_seconds)
    if isinstance(exc, PortalNotFound):
        return NotFoundError("The portal does not have that resource.")
    if isinstance(exc, PortalTimeout):
        return UpstreamTimeoutError()
    if isinstance(exc, PortalUnavailable):
        return UpstreamUnavailableError()
    if isinstance(exc, (PortalAuthError, PortalSessionExpired)):
        return UpstreamAuthError()
    if isinstance(exc, PortalProtocolError):
        return UpstreamError(
            "The upstream portal returned data in an unexpected shape. "
            "This usually means the portal changed; see the service logs."
        )
    return UpstreamError()


def _summarise_validation(exc: RequestValidationError) -> list[dict[str, str]]:
    """Flatten pydantic's error list into something a human can act on."""
    return [
        {
            "field": ".".join(str(part) for part in error.get("loc", []) if part != "query"),
            "problem": error.get("msg", "invalid"),
        }
        for error in exc.errors()
    ]


app = create_app()
