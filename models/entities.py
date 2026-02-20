"""
Entidades de dominio — representación en memoria (no SQLAlchemy).
Usadas como DTOs entre repositorios y servicios.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class User:
    user_id: str  # p.ej. "user_juan_123" | "unknown"
    name: str
    face_embedding: bytes | None = (
        None  # vector 128D serializado, sincronizado desde Android
    )
    preferences: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=_now)
    last_seen: datetime = field(default_factory=_now)
    id: int | None = None  # PK asignada por la BD


@dataclass
class Memory:
    user_id: str
    memory_type: str  # "fact" | "preference" | "conversation"
    content: str
    importance: int = 5  # 1-10
    timestamp: datetime = field(default_factory=_now)
    expires_at: datetime | None = None
    id: int | None = None


@dataclass
class Interaction:
    user_id: str
    request_type: str  # "audio" | "vision" | "text"
    summary: str
    timestamp: datetime = field(default_factory=_now)
    id: int | None = None


@dataclass
class ConversationMessage:
    session_id: str
    role: str  # "user" | "assistant"
    content: str
    message_index: int = 0
    is_compacted: bool = False  # True si representa un resumen compactado
    timestamp: datetime = field(default_factory=_now)
    id: int | None = None
