"""
Tests unitarios v2.0 — Routers REST

Estrategia:
  - SQLite in-memory vía init_db() + create_all_tables()
  - httpx.AsyncClient con ASGITransport levanta la app sin red.

Endpoints cubiertos:
  - GET /api/health
  - GET /api/restore
"""

import base64

import pytest
from httpx import ASGITransport, AsyncClient

import db as db_module
from db import create_all_tables, drop_all_tables

API_KEY = "test-api-key-for-unit-tests-only"
HEADERS = {"X-API-Key": API_KEY}


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
async def in_memory_db():
    """BD SQLite en memoria, creada y destruida por cada test."""
    db_module.init_db("sqlite+aiosqlite:///:memory:")
    await create_all_tables()
    yield
    await drop_all_tables()
    if db_module.engine is not None:
        await db_module.engine.dispose()


@pytest.fixture
async def client():
    """Cliente HTTP apuntando a la app FastAPI via ASGI (sin red)."""
    from main import app
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


# ── GET /api/health ────────────────────────────────────────────────────────────


class TestHealth:
    async def test_health_returns_ok(self, client):
        resp = await client.get("/api/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert body["version"] == "2.0"

    async def test_health_no_api_key_required(self, client):
        """Health check accesible sin API Key."""
        resp = await client.get("/api/health")
        assert resp.status_code == 200

    async def test_health_with_api_key_also_works(self, client):
        resp = await client.get("/api/health", headers=HEADERS)
        assert resp.status_code == 200


# ── GET /api/restore ───────────────────────────────────────────────────────────


class TestRestore:
    async def test_restore_empty_db(self, client):
        """Restore con BD vacía devuelve listas vacías."""
        resp = await client.get("/api/restore", headers=HEADERS)
        assert resp.status_code == 200
        body = resp.json()
        assert body["people"] == []
        assert body["zones"] == []
        assert body["general_memories"] == []

    async def test_restore_requires_api_key(self, client):
        resp = await client.get("/api/restore")
        assert resp.status_code == 401

    async def test_restore_with_person(self, client):
        """Restore debe devolver personas registradas en la BD."""
        from db import PersonRow, AsyncSessionLocal
        assert AsyncSessionLocal is not None
        async with AsyncSessionLocal() as s:
            s.add(PersonRow(person_id="persona_test_001", name="Test"))
            await s.commit()

        resp = await client.get("/api/restore", headers=HEADERS)
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["people"]) == 1
        assert body["people"][0]["person_id"] == "persona_test_001"
        assert body["people"][0]["name"] == "Test"
        assert body["people"][0]["face_embeddings"] == []

    async def test_restore_with_zone(self, client):
        """Restore debe devolver zonas con sus paths."""
        from db import ZoneRow, ZonePathRow, AsyncSessionLocal
        assert AsyncSessionLocal is not None
        async with AsyncSessionLocal() as s:
            z1 = ZoneRow(name="sala", category="living_area")
            z2 = ZoneRow(name="cocina", category="kitchen")
            s.add_all([z1, z2])
            await s.flush()
            s.add(ZonePathRow(from_zone_id=z1.id, to_zone_id=z2.id, direction_hint="norte"))
            await s.commit()

        resp = await client.get("/api/restore", headers=HEADERS)
        assert resp.status_code == 200
        body = resp.json()
        zones = {z["name"]: z for z in body["zones"]}
        assert "sala" in zones
        assert "cocina" in zones
        assert len(zones["sala"]["paths"]) == 1
        assert zones["sala"]["paths"][0]["direction_hint"] == "norte"

    async def test_restore_with_general_memory(self, client):
        """Restore incluye memorias generales (sin persona)."""
        from db import MemoryRow, AsyncSessionLocal
        assert AsyncSessionLocal is not None
        async with AsyncSessionLocal() as s:
            s.add(MemoryRow(
                memory_type="general",
                content="La casa tiene jardín",
                importance=5,
            ))
            await s.commit()

        resp = await client.get("/api/restore", headers=HEADERS)
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["general_memories"]) == 1
        assert body["general_memories"][0]["content"] == "La casa tiene jardín"

    async def test_restore_excludes_person_memories(self, client):
        """Restore NO incluye memorias con person_id (solo generales)."""
        from db import PersonRow, MemoryRow, AsyncSessionLocal
        assert AsyncSessionLocal is not None
        async with AsyncSessionLocal() as s:
            s.add(PersonRow(person_id="p1", name="P1"))
            await s.flush()
            s.add(MemoryRow(
                memory_type="person_fact",
                content="Le gusta el jazz",
                importance=7,
                person_id="p1",
            ))
            s.add(MemoryRow(
                memory_type="general",
                content="Recuerdo general",
                importance=5,
            ))
            await s.commit()

        resp = await client.get("/api/restore", headers=HEADERS)
        body = resp.json()
        mems = body["general_memories"]
        assert len(mems) == 1
        assert mems[0]["content"] == "Recuerdo general"

    async def test_restore_with_embedding(self, client):
        """Restore devuelve embeddings faciales en base64."""
        from db import PersonRow, FaceEmbeddingRow, AsyncSessionLocal
        assert AsyncSessionLocal is not None
        async with AsyncSessionLocal() as s:
            s.add(PersonRow(person_id="p_emb", name="Embed"))
            await s.flush()
            s.add(FaceEmbeddingRow(person_id="p_emb", embedding=b"\x01" * 128))
            await s.commit()

        resp = await client.get("/api/restore", headers=HEADERS)
        body = resp.json()
        person = next(p for p in body["people"] if p["person_id"] == "p_emb")
        assert len(person["face_embeddings"]) == 1
        # Debe ser base64 válido
        decoded = base64.b64decode(person["face_embeddings"][0])
        assert len(decoded) == 128

    async def test_restore_current_zone_flagged(self, client):
        """La zona actual de Robi tiene is_current=True."""
        from db import ZoneRow, AsyncSessionLocal
        assert AsyncSessionLocal is not None
        async with AsyncSessionLocal() as s:
            z = ZoneRow(name="sala", category="living_area", current_robi_zone=True)
            s.add(z)
            await s.commit()

        resp = await client.get("/api/restore", headers=HEADERS)
        body = resp.json()
        sala = next(z for z in body["zones"] if z["name"] == "sala")
        assert sala["is_current"] is True
