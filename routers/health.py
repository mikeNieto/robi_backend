"""
routers/health.py — Health check público.

GET /api/health → {"status": "ok", "version": "1.4"}
"""

from fastapi import APIRouter

from models.responses import HealthResponse

router = APIRouter(prefix="/api", tags=["health"])


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse()
