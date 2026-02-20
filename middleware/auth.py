"""
middleware/auth.py — Validación de API Key.

Comprueba el header `X-API-Key` en todas las rutas REST.
Usa `secrets.compare_digest` para evitar timing-attacks.

Rutas excluidas (no requieren API Key):
  - GET  /api/health     (health check público)
  - WS   /ws/*           (la autenticación WS se hace en websockets/auth.py)
"""

import secrets

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

from config import settings

# Rutas que no necesitan API Key
_PUBLIC_PREFIXES = ("/api/health", "/ws/", "/docs", "/openapi.json", "/redoc")


class APIKeyMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)
        self._api_key = settings.API_KEY

    async def dispatch(self, request: Request, call_next):
        # Dejar pasar rutas públicas
        if any(request.url.path.startswith(p) for p in _PUBLIC_PREFIXES):
            return await call_next(request)

        api_key = request.headers.get("X-API-Key", "")
        if not secrets.compare_digest(api_key.encode(), self._api_key.encode()):
            return JSONResponse(
                status_code=401,
                content={
                    "error": True,
                    "error_code": "INVALID_API_KEY",
                    "message": "API Key inválida o ausente",
                    "details": None,
                    "recoverable": False,
                    "retry_after": None,
                    "timestamp": _utcnow(),
                },
            )

        return await call_next(request)


def _utcnow() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()
