"""HTTP adapter for the legacy Urja Meter Ops portal.

This module is the **only** place that knows the portal exists. It speaks the portal's
protocol - cookies, HMAC signatures, devalue payloads, its three different ways of saying
"your session died" - and hands the rest of the application plain Python structures.

Everything here was derived by observing the live portal; see ``PROTOCOL.md`` for the
evidence behind each behaviour. Nothing is assumed.

Endpoint map (all require a valid session cookie):

===============================================  =========================================
``GET /portal/meters/search?q=&page=``           paged list, 20/page, ``{data,total}``
``GET /portal/dts?page=``                        DT registry, 20/page, ``{data,total}``
``GET /portal/meters/{id}/energy``               full 7-day series, ``{data:[...]}``
``GET /portal/meters/{id}/geo``                  ``{data:{latitude,longitude}}`` (strings)
``GET /portal/keys``                             ``{data:{signingSecret}}``
``GET /portal/export?page=1``                    **all** meters; HMAC-signed
``GET /meters/{id}/__data.json``                 SSR detail (devalue-encoded)
===============================================  =========================================
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Any

import httpx

from .devalue import find_node_with_keys
from .exceptions import (
    PortalNotFound,
    PortalProtocolError,
    PortalSessionExpired,
    PortalTimeout,
    PortalUnavailable,
)
from .session import PortalSession
from .signing import sign_request

logger = logging.getLogger(__name__)

#: Fixed by the portal; not configurable from the client side.
PORTAL_PAGE_SIZE = 20

_RETRYABLE_STATUS = frozenset({502, 503, 504})


def escape_like(term: str) -> str:
    r"""Escape SQL ``LIKE`` wildcards so a search means what the caller typed.

    The portal interpolates ``q`` into a ``LIKE`` pattern without escaping it. Verified
    live: ``q=_`` matches all 403 meters, ``q=SE%`` matches 80, ``q=%`` matches everything.
    A user searching for a literal ``%`` should get zero results, not the whole table.

    We escape with a backslash, which is the MySQL/SQLite default escape character. This
    is an **inference** about the portal's backend, not a verified fact - if the backend
    is PostgreSQL without an explicit ``ESCAPE`` clause the backslash would be literal.
    Either way the behaviour is strictly safer than passing wildcards straight through,
    and it is confined to this one function if it ever needs revisiting.
    """
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class PortalSignatureRejected(PortalProtocolError):
    """The portal refused an HMAC signature (bad secret, or clock skew beyond +/-300s)."""


class UrjaPortalClient:
    """Async client for the Urja portal.

    Not safe to share across event loops, but safe to share across concurrent tasks on one
    loop: session refresh is single-flight and the signing secret is fetched under a lock.
    """

    def __init__(
        self,
        http: httpx.AsyncClient,
        session: PortalSession,
        base_url: str,
        *,
        max_retries: int = 3,
        backoff_base: float = 0.25,
        backoff_max: float = 4.0,
        max_concurrency: int = 6,
    ) -> None:
        self._http = http
        self._session = session
        self._base_url = base_url.rstrip("/")
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._backoff_max = backoff_max
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._signing_secret: str | None = None
        self._secret_lock = asyncio.Lock()

    # --- public API -------------------------------------------------------

    async def search_meters(self, query: str = "", page: int = 1) -> tuple[list[dict], int]:
        """One page of the meter list.

        Args:
            query: raw search term. Already-escaped by the caller if literal semantics
                are wanted - this method does not escape, so bulk crawls can pass ``""``.
            page: 1-based page number.

        Returns:
            ``(rows, total_matching)``. ``total`` is the total match count, not a page count.
        """
        body = await self._get_json("/portal/meters/search", params={"q": query, "page": page})
        return _rows_and_total(body, context="meter search")

    async def list_dts(self, page: int = 1) -> tuple[list[dict], int]:
        """One page of the distribution-transformer registry."""
        body = await self._get_json("/portal/dts", params={"page": page})
        return _rows_and_total(body, context="DT list")

    async def list_all_dts(self) -> list[dict]:
        """Every DT, by walking the pages.

        Only 40 rows across 2 pages, so this stays sequential - concurrency would add
        failure modes for no measurable gain.
        """
        first_page, total = await self.list_dts(page=1)
        rows = list(first_page)
        page = 2
        while len(rows) < total:
            batch, _ = await self.list_dts(page=page)
            if not batch:
                # Defensive: stop rather than loop forever if `total` disagrees with reality.
                logger.warning(
                    "DT pagination ended early", extra={"expected": total, "got": len(rows)}
                )
                break
            rows.extend(batch)
            page += 1
        return rows

    async def get_energy_series(self, meter_id: str) -> list[dict]:
        """Raw interval readings for one meter.

        The portal ignores every query parameter here (verified: ``from``/``to``/``days``/
        ``limit``/``page`` all return the identical full window), so range selection is the
        caller's job.
        """
        body = await self._get_json(f"/portal/meters/{meter_id}/energy")
        rows = _rows(body, context=f"energy series for {meter_id}")
        return rows

    async def get_geo(self, meter_id: str) -> dict[str, Any] | None:
        """Location for one meter.

        Coordinates arrive as **strings** here, unlike the export's floats.
        """
        body = await self._get_json(f"/portal/meters/{meter_id}/geo")
        data = body.get("data") if isinstance(body, dict) else None
        if data is None:
            return None
        if not isinstance(data, dict):
            raise PortalProtocolError(f"geo payload for {meter_id} was not an object")
        return data

    async def get_meter_detail(self, meter_id: str) -> dict[str, Any]:
        """Server-rendered detail for one meter, via SvelteKit's data endpoint.

        Returns the decoded node containing ``detail`` and ``hierarchy``. The ``detail``
        value has one of two shapes depending on the meter's ``build`` - normalising that
        is :mod:`app.domain.normalize`'s job, not this layer's.
        """
        body = await self._get_json(f"/meters/{meter_id}/__data.json", expect_json=True)
        try:
            return find_node_with_keys(body, "detail", "hierarchy")
        except PortalProtocolError as exc:
            message = str(exc)
            # A dead session is a 200 carrying a redirect; an unknown meter is a 200
            # carrying an error node. Both must be reclassified from "protocol error".
            if "redirect to '/login'" in message:
                raise PortalSessionExpired("__data.json redirected to login") from exc
            if "Meter not found" in message or "status 404" in message:
                raise PortalNotFound(f"meter {meter_id} not found") from exc
            raise

    async def get_signing_secret(self, *, force_refresh: bool = False) -> str:
        """Fetch and cache the HMAC signing secret.

        Cached because it is stable for a session's lifetime and every export would
        otherwise cost two round-trips. Refetched on demand if a signature is rejected.
        """
        if self._signing_secret and not force_refresh:
            return self._signing_secret
        async with self._secret_lock:
            if self._signing_secret and not force_refresh:
                return self._signing_secret
            body = await self._get_json("/portal/keys")
            data = body.get("data") if isinstance(body, dict) else None
            secret = data.get("signingSecret") if isinstance(data, dict) else None
            if not isinstance(secret, str) or not secret:
                raise PortalProtocolError("/portal/keys did not return a signing secret")
            self._signing_secret = secret
            return secret

    async def export_all_meters(self) -> list[dict]:
        """Every meter in one signed request - the bulk path.

        Returns all 403 meters with structured hierarchy and float coordinates. The
        ``page`` parameter is part of the signed message but is ignored by the portal
        (verified: ``page=1``, ``page=2``, ``page=99`` and no-page all return the full set).
        We keep sending ``page=1`` to match the portal's own client exactly.

        A rejected signature is retried once with a re-fetched secret, covering the case
        where the secret rotated between our cache and the call.
        """
        query = "page=1"  # mirrors the portal's own client byte-for-byte
        for attempt in (1, 2):
            secret = await self.get_signing_secret(force_refresh=attempt == 2)
            signed = sign_request(secret, "GET", "/portal/export", query)
            try:
                body = await self._get_json(
                    "/portal/export",
                    params={"page": 1},
                    headers=signed.as_headers(),
                )
            except PortalSignatureRejected:
                if attempt == 2:
                    raise
                logger.warning("export signature rejected; refreshing signing secret")
                continue
            rows = _rows(body, context="bulk export")
            logger.info("bulk export fetched", extra={"meters": len(rows)})
            return rows
        raise PortalProtocolError("unreachable")  # pragma: no cover

    # --- transport --------------------------------------------------------

    async def _get_json(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        expect_json: bool = True,
    ) -> dict[str, Any]:
        """GET with session guarantees, retries, and expiry detection.

        Retry policy, in order of precedence:
          1. session rejected  -> re-authenticate once, replay the request
          2. timeout / transport error / retryable 5xx -> exponential backoff with jitter
          3. 404 -> :class:`PortalNotFound` immediately (never retried; it is deterministic)
        """
        url = f"{self._base_url}{path}"
        last_error: Exception | None = None
        reauth_attempted = False

        for attempt in range(self._max_retries + 1):
            await self._session.ensure_authenticated()
            try:
                async with self._semaphore:
                    response = await self._http.get(url, params=params, headers=headers)
            except httpx.TimeoutException:
                last_error = PortalTimeout(f"timeout calling {path}")
                logger.warning("portal timeout", extra={"path": path, "attempt": attempt + 1})
            except httpx.HTTPError as exc:
                last_error = PortalUnavailable(f"transport error calling {path}: {exc}")
                logger.warning(
                    "portal transport error",
                    extra={"path": path, "attempt": attempt + 1, "error": str(exc)},
                )
            else:
                if self._is_session_rejected(response):
                    if reauth_attempted:
                        raise PortalSessionExpired(
                            f"session still rejected after re-authentication ({path})"
                        )
                    reauth_attempted = True
                    logger.info(
                        "portal session rejected; re-authenticating", extra={"path": path}
                    )
                    self._session.invalidate()
                    await self._session.reauthenticate()
                    continue

                if response.status_code == 401:
                    # 401 that is *not* a session problem: the export signature was refused.
                    raise PortalSignatureRejected(_error_slug(response) or "signature refused")
                if response.status_code == 404:
                    raise PortalNotFound(f"{path} returned 404")
                if response.status_code in _RETRYABLE_STATUS:
                    last_error = PortalUnavailable(f"{path} returned {response.status_code}")
                    logger.warning(
                        "portal 5xx",
                        extra={
                            "path": path,
                            "status": response.status_code,
                            "attempt": attempt + 1,
                        },
                    )
                elif response.status_code >= 400:
                    raise PortalProtocolError(
                        f"{path} returned unexpected status {response.status_code}"
                    )
                else:
                    return self._decode(response, path, expect_json)

            if attempt < self._max_retries:
                await asyncio.sleep(self._backoff_delay(attempt))

        raise last_error or PortalUnavailable(f"{path} failed after retries")

    def _decode(
        self, response: httpx.Response, path: str, expect_json: bool
    ) -> dict[str, Any]:
        if not expect_json:
            return {"raw": response.text}
        content_type = response.headers.get("content-type", "")
        if "json" not in content_type:
            # An HTML body on a JSON route means we were bounced to a login page or an
            # error page. Treat it as a protocol violation rather than parsing hopefully.
            raise PortalProtocolError(
                f"{path} returned content-type {content_type!r}, expected JSON"
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise PortalProtocolError(f"{path} returned malformed JSON") from exc
        if isinstance(body, dict) and body.get("type") == "redirect":
            raise PortalSessionExpired(f"{path} returned a soft redirect")
        if not isinstance(body, dict):
            raise PortalProtocolError(
                f"{path} returned {type(body).__name__}, expected object"
            )
        return body

    @staticmethod
    def _is_session_rejected(response: httpx.Response) -> bool:
        """Detect all three ways the portal signals a dead session.

        1. ``401 {"error":"unauthorized"}`` on ``/portal/*``
        2. ``302 Location: /login`` on HTML routes
        3. ``200 {"type":"redirect","location":"/login"}`` on ``__data.json`` - the nasty
           one, because the status line claims success.
        """
        if response.status_code == 401 and _error_slug(response) == "unauthorized":
            return True
        if response.status_code in (301, 302, 303, 307, 308):
            return "/login" in response.headers.get("location", "")
        if response.status_code == 200 and "json" in response.headers.get("content-type", ""):
            try:
                body = response.json()
            except ValueError:
                return False
            return (
                isinstance(body, dict)
                and body.get("type") == "redirect"
                and "/login" in str(body.get("location", ""))
            )
        return False

    def _backoff_delay(self, attempt: int) -> float:
        """Exponential backoff with full jitter.

        Jitter matters here: without it, a fleet of workers that all hit a portal blip
        retry in lockstep and re-create the blip.
        """
        ceiling = min(self._backoff_max, self._backoff_base * (2**attempt))
        return random.uniform(0, ceiling)


def _error_slug(response: httpx.Response) -> str | None:
    try:
        body = response.json()
    except ValueError:
        return None
    return body.get("error") if isinstance(body, dict) else None


def _rows(body: dict[str, Any], *, context: str) -> list[dict]:
    data = body.get("data")
    if data is None:
        return []
    if not isinstance(data, list):
        raise PortalProtocolError(
            f"{context}: 'data' was {type(data).__name__}, expected list"
        )
    return [row for row in data if isinstance(row, dict)]


def _rows_and_total(body: dict[str, Any], *, context: str) -> tuple[list[dict], int]:
    rows = _rows(body, context=context)
    total = body.get("total")
    if not isinstance(total, int):
        # Degrade rather than fail: the page itself is still usable.
        logger.warning("%s: missing/invalid 'total'", context, extra={"total": total})
        total = len(rows)
    return rows, total
