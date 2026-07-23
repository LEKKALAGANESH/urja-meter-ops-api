"""Portal-adapter-internal exceptions.

These describe *what the portal did*, in the portal's own terms. The API layer translates
them into the public contract in :mod:`app.errors`. Keeping the two separate means the
adapter can be reused (CLI, batch job, another service) without dragging HTTP semantics
along with it.
"""

from __future__ import annotations


class PortalError(Exception):
    """Base for every fault originating at the upstream portal."""


class PortalAuthError(PortalError):
    """Login was rejected, or a session could not be established."""


class PortalSessionExpired(PortalError):
    """A request was rejected because the session is no longer valid.

    Raised for all three expiry signals the portal uses (see PROTOCOL.md):
    a 401 JSON body, a 302 to /login, and a 200 ``__data.json`` carrying an
    embedded ``{"type":"redirect"}``.
    """


class PortalRateLimited(PortalError):
    """The portal throttled us (HTTP 429).

    Measured on the login route: the 4th sign-in within the window is rejected with
    ``x-retry-after: 10``. Data routes showed no limit at ~3 req/s. This is the main
    reason session refresh is single-flight - a fleet of concurrent 401s each triggering
    their own login would trip the limit and lock the service out of the portal entirely.
    """

    def __init__(self, message: str, retry_after_seconds: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class PortalNotFound(PortalError):
    """The portal reported that the requested entity does not exist."""


class PortalUnavailable(PortalError):
    """Network-level failure: DNS, connection refused, TLS, or 5xx after retries."""


class PortalTimeout(PortalError):
    """The portal did not respond within the configured timeout."""


class PortalProtocolError(PortalError):
    """The portal responded, but the payload did not match the documented shape.

    This is the important one: it fires when the portal changes underneath us. It is
    logged loudly rather than being silently coerced into an empty result, because a
    silent empty result looks identical to "this meter genuinely has no data".
    """
