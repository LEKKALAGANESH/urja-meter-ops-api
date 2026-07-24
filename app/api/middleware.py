"""Cross-cutting HTTP concerns: correlation IDs, access logging, security headers."""

from __future__ import annotations

import logging
import time
import uuid

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from ..logging_setup import request_id_var

logger = logging.getLogger("app.access")

REQUEST_ID_HEADER = "X-Request-ID"


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Assign every request an ID, bind it to the log context, and echo it back.

    An inbound ``X-Request-ID`` is honoured so a trace started by an upstream caller or a
    gateway stays intact end to end; otherwise we mint one.
    """

    async def dispatch(self, request: Request, call_next):
        request_id = request.headers.get(REQUEST_ID_HEADER) or uuid.uuid4().hex
        token = request_id_var.set(request_id)
        request.state.request_id = request_id
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            # Logged here with full context; app.main's handler owns the response body.
            logger.exception(
                "unhandled exception",
                extra={
                    "method": request.method,
                    "path": request.url.path,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                },
            )
            raise
        finally:
            request_id_var.reset(token)

        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        response.headers[REQUEST_ID_HEADER] = request_id
        response.headers["Server-Timing"] = f"app;dur={duration_ms}"
        logger.info(
            "request completed",
            extra={
                "method": request.method,
                "path": request.url.path,
                "status": response.status_code,
                "duration_ms": duration_ms,
            },
        )
        return response


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Baseline hardening headers.

    This service serves JSON plus one documentation page, so the policy is deliberately
    tight. ``/docs`` needs its own relaxation for the bundled Swagger assets, applied by
    path rather than globally.
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault(
            "Permissions-Policy", "geolocation=(), microphone=(), camera=()"
        )
        policy = _policy_for(request.url.path)
        if policy:
            response.headers.setdefault("Content-Security-Policy", policy)
        return response


#: The console loads its own stylesheet, ES modules and API responses, so it needs
#: 'self' rather than the API's 'none'. It still ships no inline script and no inline
#: style attribute source, so 'unsafe-inline' is deliberately absent.
_APP_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; font-src 'self'; base-uri 'none'; form-action 'none'; "
    "frame-ancestors 'none'"
)

#: JSON endpoints load nothing at all, so they get the strictest possible policy.
_API_CSP = "default-src 'none'; frame-ancestors 'none'"


def _policy_for(path: str) -> str:
    """Pick the tightest policy that still lets the response work.

    Swagger UI and Redoc are excluded entirely: they inject inline scripts and styles,
    so any policy strict enough to be worth setting would break them.
    """
    if path.startswith(("/docs", "/redoc")):
        return ""
    return _APP_CSP if path.startswith("/app") else _API_CSP
