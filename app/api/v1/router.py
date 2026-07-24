"""Aggregates the v1 routers under a single prefix.

The shared error-response map is applied here rather than repeated on each route, so the
documented contract cannot drift endpoint by endpoint.
"""

from __future__ import annotations

from fastapi import APIRouter

from ...errors import COMMON_ERROR_RESPONSES
from . import hierarchy, meters, system, transformers

api_router = APIRouter(responses=COMMON_ERROR_RESPONSES)
api_router.include_router(meters.router)
api_router.include_router(hierarchy.router)
api_router.include_router(transformers.router)
api_router.include_router(system.router)
