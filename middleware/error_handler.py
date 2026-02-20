"""
middleware/error_handler.py — Handler global de excepciones.

Convierte excepciones al formato de error de §3.9:
  { error, error_code, message, details, recoverable, retry_after, timestamp }

Tipos mapeados:
  - RequestValidationError  → 422 VALIDATION_ERROR
  - HTTPException           → código HTTP del error original
  - ExternalServiceError    → 503 EXTERNAL_SERVICE_ERROR  (recoverable)
  - NotFoundError           → 404 NOT_FOUND
  - Exception               → 500 INTERNAL_ERROR
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException


# ── Excepciones de dominio personalizadas ─────────────────────────────────────


class AppError(Exception):
    """Clase base para errores de dominio del backend."""

    status_code: int = 500
    error_code: str = "INTERNAL_ERROR"
    message: str = "Error interno del servidor"
    recoverable: bool = False
    retry_after: int | None = None

    def __init__(self, message: str | None = None, details: str | None = None) -> None:
        self.message = message or self.__class__.message
        self.details = details
        super().__init__(self.message)


class NotFoundError(AppError):
    status_code = 404
    error_code = "NOT_FOUND"
    message = "Recurso no encontrado"


class ExternalServiceError(AppError):
    status_code = 503
    error_code = "EXTERNAL_SERVICE_ERROR"
    message = "Servicio externo no disponible"
    recoverable = True
    retry_after = 5


class AuthError(AppError):
    status_code = 401
    error_code = "AUTH_ERROR"
    message = "No autorizado"


# ── Helpers ───────────────────────────────────────────────────────────────────


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _error_body(
    error_code: str,
    message: str,
    details: str | None = None,
    recoverable: bool = False,
    retry_after: int | None = None,
) -> dict:
    return {
        "error": True,
        "error_code": error_code,
        "message": message,
        "details": details,
        "recoverable": recoverable,
        "retry_after": retry_after,
        "timestamp": _now(),
    }


# ── Handlers ──────────────────────────────────────────────────────────────────


async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    details = "; ".join(
        f"{'.'.join(str(l) for l in e['loc'])}: {e['msg']}" for e in exc.errors()
    )
    return JSONResponse(
        status_code=422,
        content=_error_body(
            error_code="VALIDATION_ERROR",
            message="Error de validación en la petición",
            details=details,
        ),
    )


async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    code_map = {
        401: ("AUTH_ERROR", False, None),
        403: ("FORBIDDEN", False, None),
        404: ("NOT_FOUND", False, None),
        429: ("RATE_LIMITED", True, 60),
        503: ("SERVICE_UNAVAILABLE", True, 5),
    }
    error_code, recoverable, retry_after = code_map.get(
        exc.status_code, ("HTTP_ERROR", False, None)
    )
    return JSONResponse(
        status_code=exc.status_code,
        content=_error_body(
            error_code=error_code,
            message=str(exc.detail),
            recoverable=recoverable,
            retry_after=retry_after,
        ),
    )


async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content=_error_body(
            error_code=exc.error_code,
            message=exc.message,
            details=getattr(exc, "details", None),
            recoverable=exc.recoverable,
            retry_after=exc.retry_after,
        ),
    )


async def generic_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(
        status_code=500,
        content=_error_body(
            error_code="INTERNAL_ERROR",
            message="Error interno del servidor",
            details=str(exc),
        ),
    )


def register_error_handlers(app) -> None:
    """Registrar todos los handlers en la instancia FastAPI."""
    app.add_exception_handler(RequestValidationError, validation_exception_handler)  # type: ignore[arg-type]
    app.add_exception_handler(HTTPException, http_exception_handler)  # type: ignore[arg-type]
    app.add_exception_handler(AppError, app_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(Exception, generic_exception_handler)
