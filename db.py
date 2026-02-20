"""
Base de datos — SQLAlchemy async con aiosqlite.

Tablas: users, memories, interactions, conversation_history

Uso:
    from db import engine, AsyncSessionLocal, create_all_tables

    # Crear tablas (normalmente en el startup de FastAPI):
    await create_all_tables()

    # Sesión en un endpoint / repositorio:
    async with AsyncSessionLocal() as session:
        ...
"""

from datetime import datetime

from sqlalchemy import ForeignKey, Index, String, Text, func
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


# ── Motor y sesión ────────────────────────────────────────────────────────────


def _make_engine(database_url: str):
    """Crea el motor async. Acepta la URL desde config para facilitar los tests."""
    return create_async_engine(
        database_url,
        echo=False,
        connect_args={"check_same_thread": False},
    )


def _make_session_factory(engine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


# Instancias por defecto (cargadas desde config al importar el módulo)
def _default_engine():
    from config import settings

    return _make_engine(settings.DATABASE_URL)


# Se inicializan en init_db() para no importar config en tiempo de módulo
# (facilita tests que sobreescriben la URL)
engine: AsyncEngine | None = None
AsyncSessionLocal: async_sessionmaker[AsyncSession] | None = None


def init_db(database_url: str | None = None) -> None:
    """
    Inicializa el motor y la fábrica de sesiones.
    Llamar una vez en el startup de FastAPI o al inicio de los tests.
    """
    global engine, AsyncSessionLocal
    if database_url is None:
        from config import settings

        database_url = settings.DATABASE_URL
    engine = _make_engine(database_url)
    AsyncSessionLocal = _make_session_factory(engine)


# ── ORM base ──────────────────────────────────────────────────────────────────


class Base(DeclarativeBase):
    pass


# ── Tablas ────────────────────────────────────────────────────────────────────


class UserRow(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(
        String(50), unique=True, nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    face_embedding: Mapped[bytes | None] = mapped_column(
        nullable=True
    )  # vector 128D — desde Android
    preferences: Mapped[str] = mapped_column(
        Text, nullable=False, default="{}"
    )  # JSON serializado
    created_at: Mapped[datetime] = mapped_column(
        nullable=False, server_default=func.now()
    )
    last_seen: Mapped[datetime] = mapped_column(
        nullable=False, server_default=func.now()
    )


class MemoryRow(Base):
    __tablename__ = "memories"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(
        String(50), ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False
    )
    memory_type: Mapped[str] = mapped_column(
        String(20), nullable=False
    )  # fact | preference | conversation
    content: Mapped[str] = mapped_column(Text, nullable=False)
    importance: Mapped[int] = mapped_column(nullable=False, default=5)  # 1-10
    timestamp: Mapped[datetime] = mapped_column(
        nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime | None] = mapped_column(nullable=True)

    __table_args__ = (Index("ix_memories_user_importance", "user_id", "importance"),)


class InteractionRow(Base):
    __tablename__ = "interactions"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(
        String(50), ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False
    )
    request_type: Mapped[str] = mapped_column(
        String(20), nullable=False
    )  # audio | vision | text
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    timestamp: Mapped[datetime] = mapped_column(
        nullable=False, server_default=func.now()
    )

    __table_args__ = (Index("ix_interactions_user_ts", "user_id", "timestamp"),)


class ConversationHistoryRow(Base):
    __tablename__ = "conversation_history"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(10), nullable=False)  # user | assistant
    content: Mapped[str] = mapped_column(Text, nullable=False)
    message_index: Mapped[int] = mapped_column(nullable=False, default=0)
    is_compacted: Mapped[bool] = mapped_column(nullable=False, default=False)
    timestamp: Mapped[datetime] = mapped_column(
        nullable=False, server_default=func.now()
    )

    __table_args__ = (Index("ix_conv_session_idx", "session_id", "message_index"),)


# ── Helpers ───────────────────────────────────────────────────────────────────


async def create_all_tables() -> None:
    """Crea todas las tablas si no existen. Llamar en el lifespan de FastAPI."""
    if engine is None:
        init_db()
    assert engine is not None
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def drop_all_tables() -> None:
    """Elimina todas las tablas. Solo para tests."""
    if engine is None:
        init_db()
    assert engine is not None
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
