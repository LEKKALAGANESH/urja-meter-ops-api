"""Aggregates the v1 routers under a single prefix."""

from __future__ import annotations

from fastapi import APIRouter

from . import hierarchy, meters, system, transformers

api_router = APIRouter()
api_router.include_router(meters.router)
api_router.include_router(hierarchy.router)
api_router.include_router(transformers.router)
api_router.include_router(system.router)
