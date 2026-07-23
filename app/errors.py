"""Public error contract.

Every failure the API can emit is a subclass of :class:`ApiError`, so the shape a client
sees is identical whether the fault was ours, the portal's, or the caller's:

    {"error": {"code": "...", "message": "...", "details": {...}, "request_id": "..."}}

Rules enforced here:
  * ``message`` is safe to show a human. Stack traces and upstream HTML never reach it.
  * ``code`` is a stable machine-readable slug. Clients branch on this, not on prose.
  * Upstream faults are mapped to 502/503/504 - never leaked as a raw 500.
"""

from __future__ import annotations

from typing import Any


class ApiError(Exception):
    """Base class for all errors rendered through the public error contract."""

    status_code: int = 500
    code: str = "internal_error"
    message: str = "An unexpected error occurred."

    def __init__(
        self,
        message: str | None = None,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.message = message or self.__class__.message
        self.details = details or {}
        super().__init__(self.message)

    def to_payload(self, request_id: str | None = None) -> dict[str, Any]:
        error: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details:
            error["details"] = self.details
        if request_id:
            error["request_id"] = request_id
        return {"error": error}


# --- Caller-side faults (4xx) ---------------------------------------------


class NotFoundError(ApiError):
    status_code = 404
    code = "not_found"
    message = "The requested resource does not exist."


class ValidationError(ApiError):
    status_code = 422
    code = "validation_error"
    message = "The request parameters were invalid."


# --- Upstream/portal faults (5xx) -----------------------------------------


class UpstreamError(ApiError):
    """The portal responded, but not in a way we can use."""

    status_code = 502
    code = "upstream_error"
    message = "The upstream Urja portal returned an unusable response."


class UpstreamTimeoutError(ApiError):
    status_code = 504
    code = "upstream_timeout"
    message = "The upstream Urja portal did not respond in time."


class UpstreamUnavailableError(ApiError):
    status_code = 503
    code = "upstream_unavailable"
    message = "The upstream Urja portal is unreachable."


class UpstreamAuthError(ApiError):
    """We could not establish or maintain a portal session.

    Deliberately 502, not 401: the *caller* is not unauthorised - our service is. Returning
    401 here would wrongly invite the caller to retry with credentials they do not own.
    """

    status_code = 502
    code = "upstream_auth_failed"
    message = "Could not authenticate against the upstream Urja portal."


class UpstreamRateLimitedError(ApiError):
    """The upstream portal is throttling us.

    503 rather than 429: the *caller* did not exceed a limit - we did, against the portal.
    A 429 would wrongly tell the client to slow its own requests down. The advertised
    delay is echoed in a ``Retry-After`` response header.
    """

    status_code = 503
    code = "upstream_rate_limited"
    message = "The upstream Urja portal is rate limiting this service. Retry shortly."

    def __init__(
        self, message: str | None = None, *, retry_after_seconds: float | None = None
    ):
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class ConfigurationError(ApiError):
    status_code = 500
    code = "configuration_error"
    message = "The service is not correctly configured."


class SnapshotUnavailableError(ApiError):
    status_code = 503
    code = "snapshot_unavailable"
    message = "The meter snapshot has not been built yet. Retry shortly."
