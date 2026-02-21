"""
UserRepository — CRUD asíncrono sobre la tabla `users`.

Uso:
    async with AsyncSessionLocal() as session:
        repo = UserRepository(session)
        user = await repo.create("user_juan", "Juan")
"""

import json
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db import UserRow
from models.entities import User


def _row_to_entity(row: UserRow) -> User:
    return User(
        id=row.id,
        user_id=row.user_id,
        name=row.name,
        face_embedding=row.face_embedding,
        preferences=json.loads(row.preferences) if row.preferences else {},
        created_at=row.created_at,
        last_seen=row.last_seen,
    )


class UserRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        user_id: str,
        name: str,
        face_embedding: bytes | None = None,
        preferences: dict | None = None,
    ) -> User:
        """Inserta un nuevo usuario y devuelve la entidad con el id asignado."""
        row = UserRow(
            user_id=user_id,
            name=name,
            face_embedding=face_embedding,
            preferences=json.dumps(preferences or {}),
        )
        self._session.add(row)
        await self._session.flush()  # obtiene el id generado sin hacer commit
        await self._session.refresh(row)
        return _row_to_entity(row)

    async def get_by_id(self, pk: int) -> User | None:
        """Devuelve el usuario por clave primaria interna."""
        result = await self._session.execute(select(UserRow).where(UserRow.id == pk))
        row = result.scalar_one_or_none()
        return _row_to_entity(row) if row else None

    async def get_by_user_id(self, user_id: str) -> User | None:
        """Devuelve el usuario por su `user_id` de negocio."""
        result = await self._session.execute(
            select(UserRow).where(UserRow.user_id == user_id)
        )
        row = result.scalar_one_or_none()
        return _row_to_entity(row) if row else None

    async def update_last_seen(self, user_id: str) -> None:
        """Actualiza el timestamp `last_seen` al momento actual."""
        result = await self._session.execute(
            select(UserRow).where(UserRow.user_id == user_id)
        )
        row = result.scalar_one_or_none()
        if row is not None:
            row.last_seen = datetime.now(timezone.utc)
            await self._session.flush()

    async def list_all(self) -> list[User]:
        """Devuelve todos los usuarios ordenados por `last_seen` desc."""
        result = await self._session.execute(
            select(UserRow).order_by(UserRow.last_seen.desc())
        )
        return [_row_to_entity(r) for r in result.scalars().all()]
