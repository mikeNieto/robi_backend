"""
routers/restore.py — Endpoint de restauración del estado de Robi v2.0.

GET /api/restore
    Devuelve el estado completo para que la app Android re-sincronice tras
    una desconexión: personas conocidas, zonas + paths, memorias generales.

No requiere cuerpo de request. La autenticación se delega al APIKeyMiddleware.
"""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from dependencies import get_session
from models.responses import (
    RestoreMemoryResponse,
    RestorePersonResponse,
    RestoreResponse,
    RestoreZonePathResponse,
    RestoreZoneResponse,
)
from repositories.memory import MemoryRepository
from repositories.people import PeopleRepository
from repositories.zones import ZonesRepository

router = APIRouter(prefix="/api", tags=["restore"])


@router.get("/restore", response_model=RestoreResponse)
async def restore(session: AsyncSession = Depends(get_session)) -> RestoreResponse:
    """
    Devuelve el estado completo de Robi para re-sincronización del cliente Android.

    - **people**: todas las personas conocidas con embeddings
    - **zones**: todas las zonas con sus paths salientes
    - **general_memories**: memorias generales (sin persona asociada)
    """
    people_repo = PeopleRepository(session)
    zones_repo = ZonesRepository(session)
    memory_repo = MemoryRepository(session)

    # ── Personas ──────────────────────────────────────────────────────────────
    all_people = await people_repo.list_all()
    all_embeddings = await people_repo.get_all_embeddings()

    # Index embeddings by person_id for fast lookup
    import base64
    from collections import defaultdict

    embeddings_by_person: dict[str, list[str]] = defaultdict(list)
    for emb in all_embeddings:
        embeddings_by_person[emb.person_id].append(
            base64.b64encode(emb.embedding).decode()
        )

    people_out = [
        RestorePersonResponse(
            person_id=p.person_id,
            name=p.name,
            first_seen=p.first_seen,
            last_seen=p.last_seen,
            interaction_count=p.interaction_count,
            notes=p.notes,
            face_embeddings=embeddings_by_person.get(p.person_id, []),
        )
        for p in all_people
    ]

    # ── Zonas ─────────────────────────────────────────────────────────────────
    all_zones = await zones_repo.list_all()

    zones_out = []
    for zone in all_zones:
        paths = await zones_repo.get_paths_from(zone.id)  # type: ignore[arg-type]
        paths_out = [
            RestoreZonePathResponse(
                to_zone_id=path.to_zone_id,
                direction_hint=path.direction_hint,
                distance_cm=path.distance_cm,
            )
            for path in paths
        ]
        zones_out.append(
            RestoreZoneResponse(
                id=zone.id,  # type: ignore[arg-type]
                name=zone.name,
                category=zone.category,
                description=zone.description,
                known_since=zone.known_since,
                accessible=zone.accessible,
                is_current=zone.current_robi_zone,
                paths=paths_out,
            )
        )

    # ── Memorias generales ────────────────────────────────────────────────────
    general_mems = await memory_repo.get_general(include_expired=False, limit=50)
    memories_out = [
        RestoreMemoryResponse(
            id=m.id,  # type: ignore[arg-type]
            memory_type=m.memory_type,
            content=m.content,
            importance=m.importance,
            created_at=m.timestamp,
            person_id=m.person_id,
            zone_id=m.zone_id,
        )
        for m in general_mems
    ]

    return RestoreResponse(
        people=people_out,
        zones=zones_out,
        general_memories=memories_out,
    )
