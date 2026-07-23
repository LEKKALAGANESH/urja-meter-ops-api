"""Dependency wiring and shared request-parameter models.

Long-lived collaborators (HTTP client, portal session, snapshot store) are built once at
startup and hung off ``app.state``. Routes reach them through these dependencies rather
than through module-level globals, which is what lets tests inject fakes without patching.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, Query, Request

from ..config import Settings, get_settings
from ..domain.models import Page, PageMeta
from ..errors import SnapshotUnavailableError
from ..portal.client import UrjaPortalClient
from ..portal.session import PortalSession
from ..store.snapshot import Snapshot, SnapshotStore


def get_store(request: Request) -> SnapshotStore:
    store = getattr(request.app.state, "store", None)
    if store is None:  # pragma: no cover - only reachable if startup failed
        raise SnapshotUnavailableError("Snapshot store is not initialised.")
    return store


def get_client(request: Request) -> UrjaPortalClient:
    return request.app.state.portal_client


def get_session(request: Request) -> PortalSession:
    return request.app.state.portal_session


def get_config(request: Request) -> Settings:
    return getattr(request.app.state, "settings", None) or get_settings()


async def get_snapshot(store: SnapshotStore = Depends(get_store)) -> Snapshot:
    """The current snapshot, rebuilding it if stale.

    Every data route depends on this, so freshness is enforced in exactly one place.
    """
    return await store.get()


@dataclass
class Pagination:
    page: int
    page_size: int

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.page_size

    def slice(self, items: list) -> list:
        return items[self.offset : self.offset + self.page_size]

    def envelope(self, items: list, total: int) -> Page:
        total_pages = max(1, -(-total // self.page_size))  # ceiling division
        return Page(
            items=items,
            meta=PageMeta(
                page=self.page,
                page_size=self.page_size,
                total_items=total,
                total_pages=total_pages,
                has_next=self.page < total_pages,
                has_previous=self.page > 1,
            ),
        )


def pagination(
    page: int = Query(1, ge=1, description="1-based page number."),
    page_size: int = Query(25, ge=1, le=200, description="Items per page (max 200)."),
) -> Pagination:
    """Validated pagination parameters.

    Bounds are enforced here rather than passed through, because the portal's own
    pagination silently accepts nonsense - ``page=0`` and ``page=abc`` both return page 1,
    which makes a naive crawler duplicate rows without ever seeing an error.
    """
    return Pagination(page=page, page_size=page_size)
