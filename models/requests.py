"""
Modelos Pydantic para payloads de entrada en la REST API.
Sin `from typing import` — tipos nativos Python 3.12.
"""

from pydantic import BaseModel, Field


# ── Usuarios ──────────────────────────────────────────────────────────────────


class FaceRegisterRequest(BaseModel):
    """POST /api/face/register — registrar un nuevo usuario con embedding facial."""

    user_id: str = Field(..., min_length=1, max_length=50)
    name: str = Field(..., min_length=1, max_length=100)
    embedding_b64: str = Field(
        ..., description="Vector FaceNet 128D codificado en base64"
    )


# ── Memoria ───────────────────────────────────────────────────────────────────


class MemorySaveRequest(BaseModel):
    """POST /api/users/{user_id}/memory — guardar una nueva memoria."""

    memory_type: str = Field("fact", pattern="^(fact|preference|conversation)$")
    content: str = Field(..., min_length=1, max_length=2000)
    importance: int = Field(5, ge=1, le=10)
    expires_at: str | None = Field(
        default=None, description="ISO-8601 datetime; null = sin expiración"
    )
