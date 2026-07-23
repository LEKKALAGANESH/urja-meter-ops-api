"""Portal session lifecycle: login, expiry tracking, and single-flight re-authentication.

The portal issues a `better-auth` cookie with ``Max-Age=3600``. A long-running service
will therefore cross that boundary repeatedly, so session handling is a first-class
concern rather than a login call at startup.

Two mechanisms, deliberately both:

**Proactive.** ``POST /api/auth/sign-in/email`` returns the session's ``expiresAt``, so we
know the deadline instead of guessing it. We re-authenticate ``session_refresh_margin_seconds``
early, which keeps the expiry off the request path entirely under normal operation.

**Reactive.** Clocks drift, sessions get revoked server-side, and deploys restart mid-flight.
So we *also* detect rejection on the response and retry once with a fresh session. Proactive
refresh alone would be an assumption; reactive alone would make every expiry a user-visible
latency spike.

Concurrent refreshes are collapsed by an :class:`asyncio.Lock` (single-flight). Without it,
N in-flight requests all hitting expiry would fire N logins, and each new cookie would
invalidate the previous one - a self-inflicted outage under exactly the load where it hurts.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from .exceptions import (
    PortalAuthError,
    PortalProtocolError,
    PortalRateLimited,
    PortalTimeout,
    PortalUnavailable,
)

logger = logging.getLogger(__name__)

#: better-auth's JSON sign-in route. Preferred over the SvelteKit form action at POST /login
#: because it returns a structured body including the session deadline, where the form
#: action returns only an opaque 303 redirect envelope.
SIGN_IN_PATH = "/api/auth/sign-in/email"
GET_SESSION_PATH = "/api/auth/get-session"
SESSION_COOKIE_NAME = "__Secure-better-auth.session_token"

#: Fallback lifetime used only when the portal omits ``expiresAt``. Matches the observed
#: cookie Max-Age of 3600s.
DEFAULT_SESSION_LIFETIME = timedelta(seconds=3600)

#: How many times to wait-and-retry when the login route throttles us.
LOGIN_RATE_LIMIT_RETRIES = 2

#: Used only if a 429 arrives without a usable retry hint.
DEFAULT_RETRY_AFTER_SECONDS = 10.0


class PortalSession:
    """Owns the authenticated ``httpx.AsyncClient`` and its session state."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        base_url: str,
        username: str,
        password: str,
        *,
        refresh_margin_seconds: int = 120,
    ) -> None:
        self._client = client
        self._base_url = base_url.rstrip("/")
        self._username = username
        self._password = password
        self._refresh_margin = timedelta(seconds=refresh_margin_seconds)
        self._lock = asyncio.Lock()
        self._expires_at: datetime | None = None
        self._authenticated_at: datetime | None = None
        self._login_count = 0

    # --- introspection (surfaced by /health/ready and /api/v1/system/session) ---

    @property
    def is_authenticated(self) -> bool:
        return self._expires_at is not None and not self._is_stale()

    @property
    def expires_at(self) -> datetime | None:
        return self._expires_at

    @property
    def authenticated_at(self) -> datetime | None:
        return self._authenticated_at

    @property
    def login_count(self) -> int:
        """Number of logins performed. A steadily climbing value in production means
        sessions are being invalidated faster than expected - worth alerting on."""
        return self._login_count

    def _is_stale(self) -> bool:
        if self._expires_at is None:
            return True
        return _utcnow() >= (self._expires_at - self._refresh_margin)

    # --- lifecycle --------------------------------------------------------

    async def ensure_authenticated(self) -> None:
        """Guarantee a live session, refreshing proactively if near expiry."""
        if not self._is_stale():
            return
        await self.reauthenticate()

    async def reauthenticate(self) -> None:
        """Force a fresh login, collapsing concurrent callers into one request."""
        async with self._lock:
            # Re-check inside the lock: whoever held it may have just refreshed for us.
            if not self._is_stale():
                return
            await self._login()

    def invalidate(self) -> None:
        """Mark the session dead so the next call re-authenticates.

        Called when the portal rejects a request, so a stale-but-unexpired local view of
        the deadline cannot keep us looping on 401s.
        """
        self._expires_at = None

    async def _login(self) -> None:
        """Authenticate, retrying politely if the portal throttles us.

        The login route is rate limited (measured: the 4th attempt in a window returns
        429 with ``x-retry-after: 10``). We honour the advertised delay rather than
        retrying blindly, because ignoring it is how a client gets locked out for longer.
        """
        for attempt in range(LOGIN_RATE_LIMIT_RETRIES + 1):
            response = await self._post_credentials()
            if response.status_code != 429:
                break
            delay = _retry_after_seconds(response) or DEFAULT_RETRY_AFTER_SECONDS
            if attempt == LOGIN_RATE_LIMIT_RETRIES:
                raise PortalRateLimited(
                    "portal is rate limiting sign-in attempts", retry_after_seconds=delay
                )
            logger.warning(
                "login rate limited; backing off as instructed",
                extra={"retry_after_seconds": delay, "attempt": attempt + 1},
            )
            await asyncio.sleep(delay)

        if response.status_code in (401, 403):
            # Never echo the response body: it can contain the submitted credentials.
            raise PortalAuthError(
                f"portal rejected credentials for {self._username!r} "
                f"(HTTP {response.status_code})"
            )
        if response.status_code >= 400:
            raise PortalAuthError(f"unexpected login status {response.status_code}")

        try:
            body = response.json()
        except ValueError as exc:
            raise PortalProtocolError("login response was not JSON") from exc

        if SESSION_COOKIE_NAME not in self._client.cookies:
            raise PortalAuthError("login succeeded but no session cookie was issued")

        self._expires_at = _parse_expiry(body)
        self._authenticated_at = _utcnow()
        self._login_count += 1
        logger.info(
            "portal session established",
            extra={
                "expires_at": self._expires_at.isoformat(),
                "login_count": self._login_count,
            },
        )

    async def _post_credentials(self) -> httpx.Response:
        try:
            return await self._client.post(
                f"{self._base_url}{SIGN_IN_PATH}",
                json={"email": self._username, "password": self._password},
                # better-auth enforces an Origin header on state-changing routes and
                # returns MISSING_OR_NULL_ORIGIN without it.
                headers={"origin": self._base_url},
            )
        except httpx.TimeoutException as exc:
            raise PortalTimeout("timed out while authenticating") from exc
        except httpx.HTTPError as exc:
            raise PortalUnavailable(f"could not reach portal to authenticate: {exc}") from exc

    async def fetch_remote_session(self) -> dict[str, Any] | None:
        """Ask the portal what it thinks of our session.

        Used by the readiness probe: it verifies the cookie is genuinely accepted upstream,
        rather than trusting our local expiry bookkeeping.
        """
        try:
            response = await self._client.get(f"{self._base_url}{GET_SESSION_PATH}")
        except httpx.HTTPError as exc:
            raise PortalUnavailable(f"could not reach portal: {exc}") from exc
        if response.status_code != 200:
            return None
        try:
            body = response.json()
        except ValueError:
            return None
        return body if isinstance(body, dict) and body.get("session") else None


def _parse_expiry(body: Any) -> datetime:
    """Read ``session.expiresAt`` from a login response, tolerating its absence.

    Falls back to now + 1 hour so a portal that stops advertising the deadline degrades to
    the previously observed behaviour instead of disabling session management entirely.
    """
    raw = None
    if isinstance(body, dict):
        session = body.get("session")
        if isinstance(session, dict):
            raw = session.get("expiresAt")
    if isinstance(raw, str):
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        except ValueError:
            logger.warning("unparseable session expiry %r; using default lifetime", raw)
    return _utcnow() + DEFAULT_SESSION_LIFETIME


def _retry_after_seconds(response: httpx.Response) -> float | None:
    """Read the throttle delay.

    better-auth sends ``x-retry-after`` (measured), not the standard ``Retry-After``;
    both are accepted so a future correction upstream does not break us.
    """
    for header in ("x-retry-after", "retry-after"):
        raw = response.headers.get(header)
        if raw:
            try:
                return max(0.0, float(raw))
            except ValueError:
                continue
    return None


def _utcnow() -> datetime:
    return datetime.now(UTC)
