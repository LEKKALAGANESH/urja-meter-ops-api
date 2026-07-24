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
from ..errors import ValidationError
from ..portal.client import UrjaPortalClient
from ..portal.session import PortalSession
from ..store.snapshot import Snapshot, SnapshotStore


def _ensure_runtime(request: Request) -> None:
    """Attach the portal client/session/store if a request arrives before they exist.

    On a normal uvicorn/container boot the lifespan builds them first. On serverless
    platforms that do not reliably run ASGI lifespan events (e.g. Vercel), the first request
    lands here instead; ``attach_runtime`` is idempotent, so this is a no-op once built. The
    import is local to dodge a circular import at module load.
    """
    if getattr(request.app.state, "store", None) is None:
        from ..main import attach_runtime

        attach_runtime(request.app)


def get_store(request: Request) -> SnapshotStore:
    _ensure_runtime(request)
    return request.app.state.store


def get_client(request: Request) -> UrjaPortalClient:
    _ensure_runtime(request)
    return request.app.state.portal_client


def get_session(request: Request) -> PortalSession:
    _ensure_runtime(request)
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
    page_size: int | None = Query(
        None,
        ge=1,
        description="Items per page. Defaults to DEFAULT_PAGE_SIZE, capped at MAX_PAGE_SIZE.",
    ),
    settings: Settings = Depends(get_config),
) -> Pagination:
    """Validated pagination parameters.

    Bounds are enforced here rather than passed through, because the portal's own
    pagination silently accepts nonsense - ``page=0`` and ``page=abc`` both return page 1,
    which makes a naive crawler duplicate rows without ever seeing an error.

    The default page size and the ceiling come from settings
    (``DEFAULT_PAGE_SIZE`` / ``MAX_PAGE_SIZE``), so they are real operational knobs rather
    than constants that silently ignore the environment.
    """
    size = settings.default_page_size if page_size is None else page_size
    if size > settings.max_page_size:
        raise ValidationError(
            "page_size exceeds the maximum allowed.",
            details={"max_page_size": settings.max_page_size},
        )
    return Pagination(page=page, page_size=size)
