"""
Base de datos — SQLAlchemy async con aiosqlite.

v2.0 — Robi Amigo Familiar

Tablas:
  people               — personas conocidas por Robi
  face_embeddings      — embeddings faciales (N por persona)
  zones                — mapa mental de la casa (nodos)
  zone_paths           — caminos entre zonas (aristas)
  memories             — recuerdos de Robi (person_id nullable, zone_id nullable)
  conversation_history — historial de mensajes por sesión

Uso:
    from db import engine, AsyncSessionLocal, create_all_tables

    # Crear tablas (normalmente en el startup de FastAPI):
    await create_all_tables()

    # Sesión en un endpoint / repositorio:
    async with AsyncSessionLocal() as session:
        ...
"""

from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, Index, Integer, String, Text, func
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


class PersonRow(Base):
    __tablename__ = "people"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    person_id: Mapped[str] = mapped_column(
        String(50), unique=True, nullable=False, index=True
    )  # slug único, p.ej. "persona_juan_01"
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    first_seen: Mapped[datetime] = mapped_column(
        nullable=False, server_default=func.now()
    )
    last_seen: Mapped[datetime] = mapped_column(
        nullable=False, server_default=func.now()
    )
    interaction_count: Mapped[int] = mapped_column(nullable=False, default=0)
    notes: Mapped[str] = mapped_column(
        Text, nullable=False, default=""
    )  # contexto libre: "le gusta el café", "trabaja de noche"


class FaceEmbeddingRow(Base):
    __tablename__ = "face_embeddings"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    person_id: Mapped[str] = mapped_column(
        String(50),
        ForeignKey("people.person_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    embedding: Mapped[bytes] = mapped_column(nullable=False)  # vector 128D serializado
    captured_at: Mapped[datetime] = mapped_column(
        nullable=False, server_default=func.now()
    )
    source_lighting: Mapped[str | None] = mapped_column(
        String(20), nullable=True
    )  # "day" | "night" | None

    __table_args__ = (Index("ix_face_emb_person", "person_id", "captured_at"),)


class ZoneRow(Base):
    __tablename__ = "zones"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    category: Mapped[str] = mapped_column(
        String(20), nullable=False, default="unknown"
    )  # kitchen | living | bedroom | bathroom | unknown
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    known_since: Mapped[datetime] = mapped_column(
        nullable=False, server_default=func.now()
    )
    accessible: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    current_robi_zone: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )  # solo una fila puede ser True a la vez


class ZonePathRow(Base):
    __tablename__ = "zone_paths"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    from_zone_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("zones.id", ondelete="CASCADE"), nullable=False
    )
    to_zone_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("zones.id", ondelete="CASCADE"), nullable=False
    )
    direction_hint: Mapped[str] = mapped_column(
        Text, nullable=False, default=""
    )  # "girar derecha 90° avanzar 2m"
    distance_cm: Mapped[int | None] = mapped_column(Integer, nullable=True)

    __table_args__ = (Index("ix_zone_paths_from_to", "from_zone_id", "to_zone_id"),)


class MemoryRow(Base):
    __tablename__ = "memories"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    person_id: Mapped[str | None] = mapped_column(
        String(50),
        ForeignKey("people.person_id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )  # nullable — hay memorias generales no ligadas a persona
    zone_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("zones.id", ondelete="SET NULL"),
        nullable=True,
    )  # contexto espacial de la memoria
    memory_type: Mapped[str] = mapped_column(
        String(20), nullable=False
    )  # experience | zone_info | person_fact | general
    content: Mapped[str] = mapped_column(Text, nullable=False)
    importance: Mapped[int] = mapped_column(nullable=False, default=5)  # 1-10
    timestamp: Mapped[datetime] = mapped_column(
        nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime | None] = mapped_column(nullable=True)

    __table_args__ = (
        Index("ix_memories_person_importance", "person_id", "importance"),
        Index("ix_memories_type_ts", "memory_type", "timestamp"),
    )


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
