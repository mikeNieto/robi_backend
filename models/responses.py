"""
Modelos Pydantic para respuestas de la REST API.
Sin `from typing import` — tipos nativos Python 3.12.
"""

from pydantic import BaseModel, Field


# ── Salud ─────────────────────────────────────────────────────────────────────


class HealthResponse(BaseModel):
    status: str = "ok"
    version: str = "1.4"


# ── Error estándar (§3.9) ─────────────────────────────────────────────────────


class ErrorResponse(BaseModel):
    error: bool = True
    error_code: str
    message: str
    details: str | None = None
    recoverable: bool = False
    retry_after: int | None = None  # segundos; None si no aplica
    timestamp: str  # ISO-8601


# ── Usuarios ──────────────────────────────────────────────────────────────────


class UserResponse(BaseModel):
    user_id: str
    name: str
    created_at: str
    last_seen: str
    has_face_embedding: bool


class UserListResponse(BaseModel):
    users: list[UserResponse]
    total: int


class FaceRegisterResponse(BaseModel):
    user_id: str
    name: str
    message: str = "Usuario registrado correctamente"


# ── Memoria ───────────────────────────────────────────────────────────────────


class MemoryItemResponse(BaseModel):
    id: int
    memory_type: str
    content: str
    importance: int = Field(ge=1, le=10)
    timestamp: str
    expires_at: str | None = None


class MemoryListResponse(BaseModel):
    user_id: str
    memories: list[MemoryItemResponse]
    total: int


class MemorySaveResponse(BaseModel):
    id: int
    message: str = "Memoria guardada correctamente"


class MemoryDeleteResponse(BaseModel):
    deleted: int
    message: str = "Memorias eliminadas"
