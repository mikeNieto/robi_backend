"""
PeopleRepository — CRUD asíncrono sobre las tablas `people` y `face_embeddings`.

Uso:
    async with AsyncSessionLocal() as session:
        repo = PeopleRepository(session)
        person = await repo.get_or_create("persona_juan_01", "Juan")
        await repo.add_embedding(person.person_id, embedding_bytes)
"""

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db import FaceEmbeddingRow, PersonRow
from models.entities import FaceEmbedding, Person


# ── Helpers ───────────────────────────────────────────────────────────────────


def _row_to_person(row: PersonRow) -> Person:
    return Person(
        id=row.id,
        person_id=row.person_id,
        name=row.name,
        first_seen=row.first_seen,
        last_seen=row.last_seen,
        interaction_count=row.interaction_count,
        notes=row.notes,
    )


def _row_to_embedding(row: FaceEmbeddingRow) -> FaceEmbedding:
    return FaceEmbedding(
        id=row.id,
        person_id=row.person_id,
        embedding=row.embedding,
        captured_at=row.captured_at,
        source_lighting=row.source_lighting,
    )


# ── Repositorio ───────────────────────────────────────────────────────────────


class PeopleRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ── People CRUD ───────────────────────────────────────────────────────────

    async def create(
        self,
        person_id: str,
        name: str,
        notes: str = "",
    ) -> Person:
        """Inserta una nueva persona y devuelve la entidad con el id asignado."""
        row = PersonRow(
            person_id=person_id,
            name=name,
            notes=notes,
        )
        self._session.add(row)
        await self._session.flush()
        await self._session.refresh(row)
        return _row_to_person(row)

    async def get_by_person_id(self, person_id: str) -> Person | None:
        """Devuelve la persona por su slug de negocio."""
        result = await self._session.execute(
            select(PersonRow).where(PersonRow.person_id == person_id)
        )
        row = result.scalar_one_or_none()
        return _row_to_person(row) if row else None

    async def get_or_create(self, person_id: str, name: str) -> tuple[Person, bool]:
        """
        Devuelve (persona, created).
        Si no existe la crea; si existe la actualiza last_seen + interaction_count.
        """
        person = await self.get_by_person_id(person_id)
        if person is None:
            person = await self.create(person_id, name)
            return person, True

        # update touch fields
        result = await self._session.execute(
            select(PersonRow).where(PersonRow.person_id == person_id)
        )
        row = result.scalar_one()
        row.last_seen = datetime.now(timezone.utc)
        row.interaction_count = (row.interaction_count or 0) + 1
        await self._session.flush()
        await self._session.refresh(row)
        return _row_to_person(row), False

    async def update_name(self, person_id: str, name: str) -> Person | None:
        """Actualiza el nombre de la persona. Devuelve None si no existe."""
        result = await self._session.execute(
            select(PersonRow).where(PersonRow.person_id == person_id)
        )
        row = result.scalar_one_or_none()
        if row is None:
            return None
        row.name = name
        await self._session.flush()
        await self._session.refresh(row)
        return _row_to_person(row)

    async def update_notes(self, person_id: str, notes: str) -> None:
        """Actualiza el campo de notas libres de una persona."""
        result = await self._session.execute(
            select(PersonRow).where(PersonRow.person_id == person_id)
        )
        row = result.scalar_one_or_none()
        if row is not None:
            row.notes = notes
            await self._session.flush()

    async def list_all(self) -> list[Person]:
        """Devuelve todas las personas ordenadas por last_seen desc."""
        result = await self._session.execute(
            select(PersonRow).order_by(PersonRow.last_seen.desc())
        )
        return [_row_to_person(r) for r in result.scalars().all()]

    async def delete(self, person_id: str) -> bool:
        """Elimina la persona (y sus embeddings por CASCADE). Devuelve True si existía."""
        result = await self._session.execute(
            select(PersonRow).where(PersonRow.person_id == person_id)
        )
        row = result.scalar_one_or_none()
        if row is None:
            return False
        await self._session.delete(row)
        await self._session.flush()
        return True

    # ── FaceEmbeddings ────────────────────────────────────────────────────────

    async def add_embedding(
        self,
        person_id: str,
        embedding: bytes,
        source_lighting: str | None = None,
    ) -> FaceEmbedding:
        """
        Agrega un nuevo embedding facial a la persona indicada.
        Lanza ValueError si la persona no existe.
        """
        person = await self.get_by_person_id(person_id)
        if person is None:
            raise ValueError(f"Persona '{person_id}' no encontrada")

        row = FaceEmbeddingRow(
            person_id=person_id,
            embedding=embedding,
            source_lighting=source_lighting,
        )
        self._session.add(row)
        await self._session.flush()
        await self._session.refresh(row)
        return _row_to_embedding(row)

    async def get_embeddings(self, person_id: str) -> list[FaceEmbedding]:
        """Devuelve todos los embeddings de una persona, ordenados por captured_at asc."""
        result = await self._session.execute(
            select(FaceEmbeddingRow)
            .where(FaceEmbeddingRow.person_id == person_id)
            .order_by(FaceEmbeddingRow.captured_at.asc())
        )
        return [_row_to_embedding(r) for r in result.scalars().all()]

    async def get_all_embeddings(self) -> list[FaceEmbedding]:
        """
        Devuelve todos los embeddings de todas las personas.
        Usado por GET /api/restore y por el matcher de caras en Android.
        """
        result = await self._session.execute(
            select(FaceEmbeddingRow).order_by(
                FaceEmbeddingRow.person_id, FaceEmbeddingRow.captured_at
            )
        )
        return [_row_to_embedding(r) for r in result.scalars().all()]

    async def delete_embedding(self, embedding_id: int) -> bool:
        """Elimina un embedding por su PK. Devuelve True si existía."""
        result = await self._session.execute(
            select(FaceEmbeddingRow).where(FaceEmbeddingRow.id == embedding_id)
        )
        row = result.scalar_one_or_none()
        if row is None:
            return False
        await self._session.delete(row)
        await self._session.flush()
        return True
