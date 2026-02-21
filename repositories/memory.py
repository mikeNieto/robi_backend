"""
MemoryRepository — CRUD asíncrono sobre la tabla `memories`.

Incluye un filtro de privacidad basado en palabras clave (versión sin Gemini).
La versión con clasificación Gemini se añadirá en la iteración 5.

Uso:
    async with AsyncSessionLocal() as session:
        repo = MemoryRepository(session)
        mem = await repo.save(user_id="user_juan", memory_type="fact",
                              content="Le gusta el café", importance=7)
"""

from datetime import datetime, timezone

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from db import MemoryRow
from models.entities import Memory


# ── Filtro de privacidad (basado en palabras clave) ───────────────────────────

_PRIVACY_KEYWORDS: frozenset[str] = frozenset(
    [
        # Datos personales sensibles
        "contraseña",
        "password",
        "clave",
        "pin",
        "tarjeta",
        "crédito",
        "débito",
        "cuenta bancaria",
        "dni",
        "pasaporte",
        "número de seguridad",
        "seguridad social",
        "dirección",
        "domicilio",
        # Salud
        "medicamento",
        "diagnóstico",
        "enfermedad",
        "tratamiento",
        # En inglés
        "address",
        "passport",
        "credit card",
        "debit card",
        "bank account",
        "social security",
        "medication",
        "diagnosis",
    ]
)


def is_private(content: str) -> bool:
    """
    Devuelve True si el contenido contiene alguna palabra clave sensible.
    Comparación case-insensitive. En la iteración 5 esto se reemplaza por
    clasificación con Gemini Flash Lite.
    """
    lower = content.lower()
    return any(kw in lower for kw in _PRIVACY_KEYWORDS)


# ── Helpers ───────────────────────────────────────────────────────────────────


def _row_to_entity(row: MemoryRow) -> Memory:
    return Memory(
        id=row.id,
        user_id=row.user_id,
        memory_type=row.memory_type,
        content=row.content,
        importance=row.importance,
        timestamp=row.timestamp,
        expires_at=row.expires_at,
    )


# ── Repositorio ───────────────────────────────────────────────────────────────


class MemoryRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def save(
        self,
        user_id: str,
        memory_type: str,
        content: str,
        importance: int = 5,
        expires_at: datetime | None = None,
    ) -> Memory | None:
        """
        Persiste una nueva memoria.

        Devuelve None si el contenido es detectado como privado por el
        filtro de palabras clave (no se guarda).
        """
        if is_private(content):
            return None

        row = MemoryRow(
            user_id=user_id,
            memory_type=memory_type,
            content=content,
            importance=importance,
            expires_at=expires_at,
        )
        self._session.add(row)
        await self._session.flush()
        await self._session.refresh(row)
        return _row_to_entity(row)

    async def get_for_user(
        self,
        user_id: str,
        *,
        include_expired: bool = False,
    ) -> list[Memory]:
        """
        Devuelve todas las memorias del usuario, ordenadas por importancia desc
        y timestamp desc. Por defecto excluye las expiradas.
        """
        stmt = select(MemoryRow).where(MemoryRow.user_id == user_id)

        if not include_expired:
            now = datetime.now(timezone.utc)
            stmt = stmt.where(
                (MemoryRow.expires_at.is_(None)) | (MemoryRow.expires_at > now)
            )

        stmt = stmt.order_by(MemoryRow.importance.desc(), MemoryRow.timestamp.desc())
        result = await self._session.execute(stmt)
        return [_row_to_entity(r) for r in result.scalars().all()]

    async def get_recent_important(
        self,
        user_id: str,
        *,
        min_importance: int = 5,
        limit: int = 5,
    ) -> list[Memory]:
        """
        Devuelve las memorias más importantes del usuario (§3.6).
        Filtra por importancia >= min_importance, ordena por timestamp DESC,
        devuelve hasta `limit` resultados. Excluye memorias expiradas.
        """
        now = datetime.now(timezone.utc)
        stmt = (
            select(MemoryRow)
            .where(MemoryRow.user_id == user_id)
            .where(MemoryRow.importance >= min_importance)
            .where((MemoryRow.expires_at.is_(None)) | (MemoryRow.expires_at > now))
            .order_by(MemoryRow.timestamp.desc())
            .limit(limit)
        )
        result = await self._session.execute(stmt)
        return [_row_to_entity(r) for r in result.scalars().all()]

    async def delete(self, memory_id: int) -> bool:
        """Elimina una memoria por su PK. Devuelve True si existía."""
        result = await self._session.execute(
            select(MemoryRow).where(MemoryRow.id == memory_id)
        )
        row = result.scalar_one_or_none()
        if row is None:
            return False
        await self._session.delete(row)
        await self._session.flush()
        return True

    async def delete_for_user(self, user_id: str) -> int:
        """Elimina todas las memorias del usuario. Devuelve el número de filas borradas."""
        result = await self._session.execute(
            delete(MemoryRow)
            .where(MemoryRow.user_id == user_id)
            .returning(MemoryRow.id)
        )
        return len(result.fetchall())
