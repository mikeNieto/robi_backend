"""
Tests unitarios v2.0 — Repositorios

Cubre:
  - PeopleRepository (people.py)
  - ZonesRepository  (zones.py)
  - MemoryRepository (memory.py)
  - MediaRepository  (media.py)

Usan SQLite in-memory para no dejar estado en disco.
"""

import time
from pathlib import Path

import pytest

import db as db_module
from db import create_all_tables, drop_all_tables
from repositories.memory import MemoryRepository
from repositories.media import MediaRepository
from repositories.people import PeopleRepository
from repositories.zones import ZonesRepository


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
async def in_memory_db():
    db_module.init_db("sqlite+aiosqlite:///:memory:")
    await create_all_tables()
    yield
    await drop_all_tables()
    engine = db_module.engine
    if engine is not None:
        await engine.dispose()


@pytest.fixture
async def session():
    assert db_module.AsyncSessionLocal is not None
    async with db_module.AsyncSessionLocal() as s:
        yield s


@pytest.fixture
def people_repo(session):
    return PeopleRepository(session)


@pytest.fixture
def zones_repo(session):
    return ZonesRepository(session)


@pytest.fixture
def memory_repo(session):
    return MemoryRepository(session)


# ── PeopleRepository ──────────────────────────────────────────────────────────


class TestPeopleRepository:
    async def test_create_returns_person(self, people_repo):
        p = await people_repo.create("persona_ana_001", "Ana")
        assert p.person_id == "persona_ana_001"
        assert p.name == "Ana"
        assert p.id is not None
        assert p.interaction_count == 0

    async def test_get_or_create_new(self, people_repo):
        p, created = await people_repo.get_or_create("persona_bob_002", "Bob")
        assert created is True
        assert p.person_id == "persona_bob_002"

    async def test_get_or_create_existing(self, people_repo):
        await people_repo.create("persona_ana_001", "Ana")
        p, created = await people_repo.get_or_create("persona_ana_001", "Ana Updated")
        assert created is False
        assert p.name == "Ana"  # no debe sobrescribir el nombre

    async def test_update_name(self, people_repo, session):
        await people_repo.create("persona_test_003", "Test")
        await people_repo.update_name("persona_test_003", "TestUpdated")
        await session.commit()
        p = await people_repo.get_by_person_id("persona_test_003")
        assert p is not None
        assert p.name == "TestUpdated"

    async def test_get_by_person_id_missing(self, people_repo):
        result = await people_repo.get_by_person_id("no_existe")
        assert result is None

    async def test_list_all_empty(self, people_repo):
        people = await people_repo.list_all()
        assert people == []

    async def test_list_all_returns_all(self, people_repo):
        await people_repo.create("p1", "Uno")
        await people_repo.create("p2", "Dos")
        people = await people_repo.list_all()
        assert len(people) == 2

    async def test_delete_person(self, people_repo, session):
        await people_repo.create("p_del", "Delete Me")
        await session.commit()
        deleted = await people_repo.delete("p_del")
        assert deleted is True

    async def test_delete_missing_person(self, people_repo):
        result = await people_repo.delete("no_existe")
        assert result is False

    async def test_add_and_get_embedding(self, people_repo, session):
        await people_repo.create("p_emb", "Embedding")
        embedding = b"\x01\x02\x03" * 43
        await people_repo.add_embedding("p_emb", embedding)
        await session.commit()
        embeddings = await people_repo.get_embeddings("p_emb")
        assert len(embeddings) == 1
        assert embeddings[0].embedding == embedding

    async def test_get_all_embeddings_multiple_people(self, people_repo, session):
        await people_repo.create("p1e", "Uno")
        await people_repo.create("p2e", "Dos")
        await people_repo.add_embedding("p1e", b"\x01" * 128)
        await people_repo.add_embedding("p2e", b"\x02" * 128)
        await session.commit()
        all_embs = await people_repo.get_all_embeddings()
        assert len(all_embs) == 2

    async def test_get_embeddings_empty(self, people_repo):
        await people_repo.create("p_no_emb", "Sin Embedding")
        embs = await people_repo.get_embeddings("p_no_emb")
        assert embs == []


# ── ZonesRepository ───────────────────────────────────────────────────────────


class TestZonesRepository:
    async def test_create_zone(self, zones_repo):
        z = await zones_repo.create("sala", "living_area")
        assert z.name == "sala"
        assert z.id is not None
        assert z.current_robi_zone is False

    async def test_get_or_create_new(self, zones_repo):
        z, created = await zones_repo.get_or_create("cocina", "kitchen")
        assert created is True

    async def test_get_or_create_existing(self, zones_repo):
        await zones_repo.create("ba\u00f1o", "bathroom")
        z, created = await zones_repo.get_or_create("ba\u00f1o", "bathroom")
        assert created is False

    async def test_get_by_name(self, zones_repo):
        await zones_repo.create("dormitorio", "bedroom")
        z = await zones_repo.get_by_name("dormitorio")
        assert z is not None
        assert z.name == "dormitorio"

    async def test_get_by_name_missing(self, zones_repo):
        result = await zones_repo.get_by_name("invisible")
        assert result is None

    async def test_list_all(self, zones_repo):
        await zones_repo.create("z1", "living_area")
        await zones_repo.create("z2", "kitchen")
        zones = await zones_repo.list_all()
        assert len(zones) == 2

    async def test_set_current_zone(self, zones_repo, session):
        z1 = await zones_repo.create("sala", "living_area")
        z2 = await zones_repo.create("cocina", "kitchen")
        await zones_repo.set_current_zone(z1.id)
        await session.commit()
        current = await zones_repo.get_current_zone()
        assert current is not None
        assert current.name == "sala"
        # Cambiar a z2
        await zones_repo.set_current_zone(z2.id)
        await session.commit()
        current = await zones_repo.get_current_zone()
        assert current.name == "cocina"

    async def test_set_current_zone_clears_previous(self, zones_repo, session):
        z1 = await zones_repo.create("antes", "living_area")
        z2 = await zones_repo.create("despues", "bedroom")
        await zones_repo.set_current_zone(z1.id)
        await session.commit()
        await zones_repo.set_current_zone(z2.id)
        await session.commit()
        # z1 ya no debe ser la zona actual
        z1_check = await zones_repo.get_by_name("antes")
        assert z1_check is not None
        assert z1_check.current_robi_zone is False

    async def test_add_and_get_paths(self, zones_repo, session):
        z1 = await zones_repo.create("A", "living_area")
        z2 = await zones_repo.create("B", "kitchen")
        await zones_repo.add_path(z1.id, z2.id, direction_hint="norte", distance_cm=300)
        await session.commit()
        paths = await zones_repo.get_paths_from(z1.id)
        assert len(paths) == 1
        assert paths[0].to_zone_id == z2.id
        assert paths[0].distance_cm == 300

    async def test_find_path_direct(self, zones_repo, session):
        z1 = await zones_repo.create("sala", "living_area")
        z2 = await zones_repo.create("cocina", "kitchen")
        await zones_repo.add_path(z1.id, z2.id, direction_hint="norte")
        await session.commit()
        path = await zones_repo.find_path("sala", "cocina")
        assert len(path) == 1
        assert path[0].to_zone_id == z2.id

    async def test_find_path_no_route(self, zones_repo, session):
        await zones_repo.create("isla", "outdoor")
        await zones_repo.create("castillo", "outdoor")
        await session.commit()
        path = await zones_repo.find_path("isla", "castillo")
        assert path == []

    async def test_get_current_zone_none(self, zones_repo):
        result = await zones_repo.get_current_zone()
        assert result is None


# ── MemoryRepository v2.0 ─────────────────────────────────────────────────────


class TestMemoryRepository:
    async def test_save_general_memory(self, memory_repo):
        mem = await memory_repo.save(
            memory_type="general",
            content="La casa tiene jard\u00edn",
        )
        assert mem.id is not None
        assert mem.person_id is None
        assert mem.importance == 5

    async def test_save_person_memory(self, memory_repo, session):
        from db import PersonRow

        session.add(PersonRow(person_id="p_mem", name="MemTest"))
        await session.flush()
        mem = await memory_repo.save(
            memory_type="person_fact",
            content="Le gusta el jazz",
            person_id="p_mem",
            importance=7,
        )
        assert mem.person_id == "p_mem"
        assert mem.importance == 7

    async def test_get_general_returns_only_null_person(self, memory_repo, session):
        from db import PersonRow

        session.add(PersonRow(person_id="p2", name="P2"))
        await session.flush()
        await memory_repo.save("general", "Recuerdo general")
        await memory_repo.save("person_fact", "Sobre persona", person_id="p2")
        general = await memory_repo.get_general()
        assert len(general) == 1
        assert general[0].person_id is None

    async def test_get_for_person(self, memory_repo, session):
        from db import PersonRow

        session.add(PersonRow(person_id="p3", name="P3"))
        await session.flush()
        await memory_repo.save("person_fact", "Hecho X", person_id="p3", importance=8)
        await memory_repo.save("general", "Recuerdo general")
        mems = await memory_repo.get_for_person("p3")
        assert len(mems) == 1
        assert mems[0].content == "Hecho X"

    async def test_get_recent_important_min_importance(self, memory_repo):
        await memory_repo.save("general", "Importante", importance=8)
        await memory_repo.save("general", "Poco importante", importance=3)
        mems = await memory_repo.get_recent_important(min_importance=5)
        assert len(mems) == 1
        assert mems[0].importance == 8

    async def test_get_robi_context_returns_dict(self, memory_repo, session):
        from db import PersonRow

        session.add(PersonRow(person_id="p4", name="P4"))
        await session.flush()
        await memory_repo.save("general", "G1", importance=6)
        await memory_repo.save("person_fact", "PF1", person_id="p4", importance=7)
        await memory_repo.save("zone_info", "Z1", importance=5)
        context = await memory_repo.get_robi_context(person_id="p4")
        assert "general" in context
        assert "person" in context
        assert "zone_info" in context

    async def test_get_robi_context_no_person(self, memory_repo):
        await memory_repo.save("general", "G1", importance=6)
        context = await memory_repo.get_robi_context(person_id=None)
        assert "general" in context

    async def test_delete_existing(self, memory_repo):
        mem = await memory_repo.save("general", "A borrar")
        deleted = await memory_repo.delete(mem.id)  # type: ignore[arg-type]
        assert deleted is True

    async def test_delete_nonexistent(self, memory_repo):
        result = await memory_repo.delete(99999)
        assert result is False

    async def test_delete_for_person(self, memory_repo, session):
        from db import PersonRow

        session.add(PersonRow(person_id="p_del", name="PDel"))
        await session.flush()
        await memory_repo.save("person_fact", "M1", person_id="p_del")
        await memory_repo.save("person_fact", "M2", person_id="p_del")
        count = await memory_repo.delete_for_person("p_del")
        assert count == 2

    async def test_replace_with_compacted(self, memory_repo, session):
        from db import PersonRow

        session.add(PersonRow(person_id="p_cmp", name="PCmp"))
        await session.flush()
        m1 = await memory_repo.save("person_fact", "Old1", person_id="p_cmp")
        m2 = await memory_repo.save("person_fact", "Old2", person_id="p_cmp")
        await memory_repo.replace_with_compacted(
            old_ids=[m1.id, m2.id],  # type: ignore[list-item]
            memory_type="person_fact",
            content="Compactado: Old1 y Old2",
            person_id="p_cmp",
            importance=7,
        )
        mems = await memory_repo.get_for_person("p_cmp")
        assert len(mems) == 1
        assert "Compactado" in mems[0].content


# ── MediaRepository ───────────────────────────────────────────────────────────


class TestMediaRepository:
    @pytest.fixture
    def media_repo(self, tmp_path: Path):
        return MediaRepository(base_dir=tmp_path)

    async def test_save_creates_file(self, media_repo):
        path = await media_repo.save(b"audio data", "clip.wav", "audio")
        assert path.exists()
        assert path.read_bytes() == b"audio data"

    async def test_save_unknown_type_raises(self, media_repo):
        with pytest.raises(ValueError, match="media_type desconocido"):
            await media_repo.save(b"data", "file.xyz", "unknown")

    async def test_save_creates_subdir(self, media_repo, tmp_path):
        await media_repo.save(b"img", "photo.jpg", "image")
        assert (tmp_path / "image").is_dir()

    async def test_delete_existing_file(self, media_repo):
        path = await media_repo.save(b"video", "clip.mp4", "video")
        deleted = await media_repo.delete(path)
        assert deleted is True
        assert not path.exists()

    async def test_delete_nonexistent_returns_false(self, media_repo, tmp_path):
        result = await media_repo.delete(tmp_path / "ghost.wav")
        assert result is False

    async def test_cleanup_removes_old_audio(self, media_repo):
        path = await media_repo.save(b"old audio", "old.wav", "audio")
        old_time = time.time() - 25 * 3600
        import os

        os.utime(path, (old_time, old_time))
        counts = await media_repo.cleanup()
        assert counts["audio"] == 1
        assert not path.exists()

    async def test_cleanup_keeps_recent(self, media_repo):
        await media_repo.save(b"fresh", "fresh.wav", "audio")
        counts = await media_repo.cleanup()
        assert counts["audio"] == 0

    async def test_media_type_for_known_extension(self, media_repo):
        assert media_repo.media_type_for("clip.wav") == "audio"
        assert media_repo.media_type_for("photo.jpg") == "image"
        assert media_repo.media_type_for("video.mp4") == "video"

    async def test_media_type_for_unknown(self, media_repo):
        assert media_repo.media_type_for("file.pdf") is None
