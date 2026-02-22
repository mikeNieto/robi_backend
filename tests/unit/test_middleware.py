"""
Tests unitarios v2.0 — Middleware e Infraestructura HTTP

Cubre:
  - APIKeyMiddleware: clave válida / inválida / ausente
  - GET /api/health: respuesta correcta sin API Key
  - Formato de error §3.9 (error_code, recoverable, retry_after, timestamp)
  - LoggingMiddleware: inyecta X-Request-Id en la respuesta
  - Error handlers: 422, 404, 500
"""

import os

import pytest
from httpx import ASGITransport, AsyncClient
from main import app

# La API Key que usa el app en tests (definida en conftest.py)
TEST_API_KEY = os.environ["API_KEY"]

# ── Fixture ───────────────────────────────────────────────────────────────────

# Usamos ASGITransport para NO disparar el lifespan (evita crear la BD real)


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


# ═══════════════════════════════════════════════════════
# Health endpoint
# ═══════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_health_no_api_key():
    """GET /api/health debe ser accesible sin API Key."""
    async with _client() as client:
        r = await client.get("/api/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "version": "2.0"}


@pytest.mark.asyncio
async def test_health_with_api_key():
    """GET /api/health también funciona con API Key correcta."""
    async with _client() as client:
        r = await client.get("/api/health", headers={"X-API-Key": TEST_API_KEY})
    assert r.status_code == 200


# ═══════════════════════════════════════════════════════
# APIKeyMiddleware
# ═══════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_api_key_missing_returns_401():
    """Sin X-API-Key en rutas REST protegidas → 401."""
    async with _client() as client:
        r = await client.get("/api/restore")
    assert r.status_code == 401
    body = r.json()
    assert body["error"] is True
    assert body["error_code"] == "INVALID_API_KEY"
    assert body["recoverable"] is False


@pytest.mark.asyncio
async def test_api_key_wrong_returns_401():
    """API Key incorrecta → 401 con formato §3.9."""
    async with _client() as client:
        r = await client.get("/api/restore", headers={"X-API-Key": "wrong-key"})
    assert r.status_code == 401
    body = r.json()
    assert body["error_code"] == "INVALID_API_KEY"
    assert "timestamp" in body


@pytest.mark.asyncio
async def test_api_key_valid_passes_through():
    """API Key correcta → el middleware deja pasar (aquí da 404 porque la ruta no existe)."""
    async with _client() as client:
        r = await client.get("/api/nonexistent", headers={"X-API-Key": TEST_API_KEY})
    # El middleware pasa, FastAPI devuelve 404
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_api_key_empty_string_returns_401():
    async with _client() as client:
        r = await client.get("/api/restore", headers={"X-API-Key": ""})
    assert r.status_code == 401


# ═══════════════════════════════════════════════════════
# Logging middleware — X-Request-Id header
# ═══════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_request_id_header_injected():
    """LoggingMiddleware debe inyectar X-Request-Id en cada respuesta."""
    async with _client() as client:
        r = await client.get("/api/health")
    assert "x-request-id" in r.headers
    request_id = r.headers["x-request-id"]
    assert len(request_id) == 36  # UUID v4: "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"


@pytest.mark.asyncio
async def test_request_ids_are_unique():
    """Cada petición debe recibir un request_id diferente."""
    async with _client() as client:
        r1 = await client.get("/api/health")
        r2 = await client.get("/api/health")
    assert r1.headers["x-request-id"] != r2.headers["x-request-id"]


# ═══════════════════════════════════════════════════════
# Error handler — formato §3.9
# ═══════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_404_error_format():
    """Ruta inexistente con API Key válida → 404 con formato correcto."""
    async with _client() as client:
        r = await client.get("/api/does-not-exist", headers={"X-API-Key": TEST_API_KEY})
    assert r.status_code == 404
    body = r.json()
    assert body["error"] is True
    assert body["error_code"] == "NOT_FOUND"
    assert "timestamp" in body
    assert "message" in body


@pytest.mark.asyncio
async def test_error_response_has_all_fields():
    """El cuerpo de error debe incluir todos los campos de §3.9."""
    async with _client() as client:
        r = await client.get("/api/restore", headers={"X-API-Key": "bad"})
    body = r.json()
    required = {
        "error",
        "error_code",
        "message",
        "details",
        "recoverable",
        "retry_after",
        "timestamp",
    }
    assert required.issubset(body.keys())


@pytest.mark.asyncio
async def test_app_error_not_found_handler():
    """NotFoundError de dominio → 404 con formato correcto."""
    from fastapi import APIRouter
    from middleware.error_handler import NotFoundError

    test_router = APIRouter()

    @test_router.get("/test-not-found")
    async def _raise():
        raise NotFoundError("Usuario no encontrado")

    app.include_router(test_router)

    async with _client() as client:
        r = await client.get("/test-not-found", headers={"X-API-Key": TEST_API_KEY})
    assert r.status_code == 404
    body = r.json()
    assert body["error_code"] == "NOT_FOUND"
    assert body["message"] == "Usuario no encontrado"


@pytest.mark.asyncio
async def test_external_service_error_recoverable():
    """ExternalServiceError → 503 recoverable con retry_after."""
    from fastapi import APIRouter
    from middleware.error_handler import ExternalServiceError

    test_router = APIRouter()

    @test_router.get("/test-service-error")
    async def _raise():
        raise ExternalServiceError("Gemini no disponible", details="Timeout 30s")

    app.include_router(test_router)

    async with _client() as client:
        r = await client.get("/test-service-error", headers={"X-API-Key": TEST_API_KEY})
    assert r.status_code == 503
    body = r.json()
    assert body["recoverable"] is True
    assert body["retry_after"] == 5
    assert body["details"] == "Timeout 30s"
