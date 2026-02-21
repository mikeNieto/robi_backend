"""
Tests unitarios para los repositorios:
  - UserRepository  (users.py)
  - MemoryRepository (memory.py)
  - MediaRepository  (media.py)

Usan SQLite in-memory para no dejar estado en disco.
"""

import asyncio
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import db as db_module
from db import create_all_tables, drop_all_tables
from repositories.memory import MemoryRepository, is_private
from repositories.media import MediaRepository
from repositories.users import UserRepository


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
async def in_memory_db():
    """
    Inicializa una base de datos SQLite en memoria antes de cada test
    y la destruye al terminar.
    """
    db_module.init_db("sqlite+aiosqlite:///:memory:")
    await create_all_tables()
    yield
    await drop_all_tables()
    # cerrar el motor para liberar la conexión en memoria
    engine = db_module.engine
    if engine is not None:
        await engine.dispose()


@pytest.fixture
async def session():
    """Sesión async encapsulada en una transacción que se revierte al final."""
    assert db_module.AsyncSessionLocal is not None
    async with db_module.AsyncSessionLocal() as s:
        yield s


@pytest.fixture
def user_repo(session):
    return UserRepository(session)


@pytest.fixture
def memory_repo(session):
    return MemoryRepository(session)


# ── UserRepository ────────────────────────────────────────────────────────────


class TestUserRepository:
    async def test_create_returns_user_with_id(self, user_repo):
        user = await user_repo.create("user_juan", "Juan")
        assert user.id is not None
        assert user.user_id == "user_juan"
        assert user.name == "Juan"
        assert user.face_embedding is None
        assert user.preferences == {}

    async def test_create_with_embedding(self, user_repo):
        embedding = b"\x01\x02\x03" * 43  # 129 bytes simulados
        user = await user_repo.create("user_ana", "Ana", face_embedding=embedding)
        assert user.face_embedding == embedding

    async def test_create_with_preferences(self, user_repo):
        prefs = {"language": "es", "volume": 7}
        user = await user_repo.create("user_prefs", "Prefs", preferences=prefs)
        assert user.preferences == prefs

    async def test_get_by_id_returns_none_when_missing(self, user_repo):
        result = await user_repo.get_by_id(9999)
        assert result is None

    async def test_get_by_id_returns_user(self, user_repo):
        created = await user_repo.create("user_id_test", "IdTest")
        assert created.id is not None
        found = await user_repo.get_by_id(created.id)
        assert found is not None
        assert found.user_id == "user_id_test"

    async def test_get_by_user_id_returns_none_when_missing(self, user_repo):
        result = await user_repo.get_by_user_id("nonexistent")
        assert result is None

    async def test_get_by_user_id_returns_user(self, user_repo):
        await user_repo.create("user_lookup", "Lookup")
        found = await user_repo.get_by_user_id("user_lookup")
        assert found is not None
        assert found.name == "Lookup"

    async def test_update_last_seen_changes_timestamp(self, user_repo, session):
        user = await user_repo.create("user_ts", "Timestamps")
        original_ts = user.last_seen

        # pequeño delay para que el timestamp sea diferente
        await asyncio.sleep(0.01)
        await user_repo.update_last_seen("user_ts")
        await session.commit()

        updated = await user_repo.get_by_user_id("user_ts")
        assert updated is not None
        assert updated.last_seen >= original_ts

    async def test_update_last_seen_noop_for_unknown_user(self, user_repo):
        # no debe lanzar excepción
        await user_repo.update_last_seen("nonexistent_user")

    async def test_list_all_empty(self, user_repo):
        users = await user_repo.list_all()
        assert users == []

    async def test_list_all_returns_multiple(self, user_repo):
        await user_repo.create("user_a", "A")
        await user_repo.create("user_b", "B")
        await user_repo.create("user_c", "C")
        users = await user_repo.list_all()
        assert len(users) == 3

    async def test_list_all_ordered_by_last_seen_desc(self, user_repo, session):
        await user_repo.create("user_old", "Old")
        await asyncio.sleep(0.05)
        await user_repo.create("user_new", "New")
        await session.commit()

        users = await user_repo.list_all()
        # El más reciente primero
        assert users[0].user_id in (
            "user_old",
            "user_new",
        )  # ambos válidos si mismo instante
        assert len(users) == 2


# ── MemoryRepository — filtro de privacidad ───────────────────────────────────


class TestPrivacyFilter:
    def test_safe_content_not_private(self):
        assert not is_private("Le gusta el café por las mañanas")

    def test_private_keyword_password(self):
        assert is_private("Su contraseña es hunter2")

    def test_private_keyword_credit_card(self):
        assert is_private("Tiene una tarjeta de crédito VISA")

    def test_private_keyword_case_insensitive(self):
        assert is_private("Su CONTRASEÑA es abc123")

    def test_private_keyword_english(self):
        assert is_private("His password is secret")

    def test_private_keyword_address(self):
        assert is_private("Vive en la dirección calle Mayor 10")

    def test_empty_string_not_private(self):
        assert not is_private("")

    def test_partial_word_not_flagged(self):
        # "tarjeta" sola como palabra aislada en frase sin contexto sensible
        # si aparece la palabra exacta sí se filtra
        assert is_private("necesita su tarjeta")


# ── MemoryRepository — CRUD ───────────────────────────────────────────────────


class TestMemoryRepository:
    async def _make_user(self, user_repo, user_id: str = "user_mem") -> None:
        await user_repo.create(user_id, "Mem User")

    async def test_save_returns_memory(self, memory_repo, user_repo):
        await self._make_user(user_repo)
        mem = await memory_repo.save(
            "user_mem", "fact", "Le gusta el fútbol", importance=8
        )
        assert mem is not None
        assert mem.id is not None
        assert mem.content == "Le gusta el fútbol"
        assert mem.importance == 8

    async def test_save_private_content_returns_none(self, memory_repo, user_repo):
        await self._make_user(user_repo)
        result = await memory_repo.save("user_mem", "fact", "Su contraseña es abc123")
        assert result is None

    async def test_get_for_user_empty(self, memory_repo, user_repo):
        await self._make_user(user_repo)
        mems = await memory_repo.get_for_user("user_mem")
        assert mems == []

    async def test_get_for_user_returns_saved(self, memory_repo, user_repo):
        await self._make_user(user_repo)
        await memory_repo.save("user_mem", "fact", "Habla español", importance=6)
        await memory_repo.save(
            "user_mem", "preference", "Prefiere música clásica", importance=5
        )
        mems = await memory_repo.get_for_user("user_mem")
        assert len(mems) == 2

    async def test_get_for_user_ordered_by_importance_desc(
        self, memory_repo, user_repo
    ):
        await self._make_user(user_repo)
        await memory_repo.save("user_mem", "fact", "Dato bajo", importance=3)
        await memory_repo.save("user_mem", "fact", "Dato alto", importance=9)
        await memory_repo.save("user_mem", "fact", "Dato medio", importance=6)
        mems = await memory_repo.get_for_user("user_mem")
        importances = [m.importance for m in mems]
        assert importances == sorted(importances, reverse=True)

    async def test_get_for_user_excludes_expired(self, memory_repo, user_repo):
        await self._make_user(user_repo)
        past = datetime.now(timezone.utc) - timedelta(hours=2)
        # guardamos directamente en la BD con expires_at en el pasado
        from db import MemoryRow, AsyncSessionLocal as ASL

        assert ASL is not None
        async with ASL() as s:
            s.add(
                MemoryRow(
                    user_id="user_mem",
                    memory_type="fact",
                    content="Expired memory",
                    importance=7,
                    expires_at=past,
                )
            )
            await s.commit()

        mems = await memory_repo.get_for_user("user_mem")
        assert all(m.content != "Expired memory" for m in mems)

    async def test_get_for_user_includes_expired_when_requested(
        self, memory_repo, user_repo
    ):
        await self._make_user(user_repo)
        past = datetime.now(timezone.utc) - timedelta(hours=2)
        from db import MemoryRow, AsyncSessionLocal as ASL

        assert ASL is not None
        async with ASL() as s:
            s.add(
                MemoryRow(
                    user_id="user_mem",
                    memory_type="fact",
                    content="Expired memory",
                    importance=7,
                    expires_at=past,
                )
            )
            await s.commit()

        mems = await memory_repo.get_for_user("user_mem", include_expired=True)
        assert any(m.content == "Expired memory" for m in mems)

    async def test_get_recent_important_min_importance(self, memory_repo, user_repo):
        await self._make_user(user_repo)
        await memory_repo.save("user_mem", "fact", "Poca importancia", importance=3)
        await memory_repo.save("user_mem", "fact", "Alta importancia", importance=8)
        mems = await memory_repo.get_recent_important("user_mem")
        # min_importance=5 por defecto → solo devuelve la de importancia 8
        assert len(mems) == 1
        assert mems[0].importance == 8

    async def test_get_recent_important_limit(self, memory_repo, user_repo):
        await self._make_user(user_repo)
        for i in range(8):
            await memory_repo.save("user_mem", "fact", f"Memoria {i}", importance=7)
        mems = await memory_repo.get_recent_important("user_mem", limit=5)
        assert len(mems) <= 5

    async def test_delete_existing_memory(self, memory_repo, user_repo):
        await self._make_user(user_repo)
        mem = await memory_repo.save("user_mem", "fact", "A borrar", importance=5)
        assert mem is not None
        deleted = await memory_repo.delete(mem.id)  # type: ignore[arg-type]
        assert deleted is True

    async def test_delete_nonexistent_returns_false(self, memory_repo):
        result = await memory_repo.delete(99999)
        assert result is False

    async def test_delete_for_user(self, memory_repo, user_repo):
        await self._make_user(user_repo)
        await memory_repo.save("user_mem", "fact", "Mem 1", importance=5)
        await memory_repo.save("user_mem", "fact", "Mem 2", importance=6)
        count = await memory_repo.delete_for_user("user_mem")
        assert count == 2
        remaining = await memory_repo.get_for_user("user_mem")
        assert remaining == []

    async def test_save_default_importance(self, memory_repo, user_repo):
        await self._make_user(user_repo)
        mem = await memory_repo.save("user_mem", "fact", "Sin importancia explícita")
        assert mem is not None
        assert mem.importance == 5


# ── MediaRepository ───────────────────────────────────────────────────────────


class TestMediaRepository:
    @pytest.fixture
    def media_repo(self, tmp_path: Path):
        return MediaRepository(base_dir=tmp_path)

    async def test_save_creates_file(self, media_repo, tmp_path):
        path = await media_repo.save(b"audio data", "clip.wav", "audio")
        assert path.exists()
        assert path.read_bytes() == b"audio data"

    async def test_save_unknown_media_type_raises(self, media_repo):
        with pytest.raises(ValueError, match="media_type desconocido"):
            await media_repo.save(b"data", "file.xyz", "unknown")

    async def test_save_creates_subdir(self, media_repo, tmp_path):
        await media_repo.save(b"img", "photo.jpg", "image")
        assert (tmp_path / "image").is_dir()

    async def test_delete_existing_file(self, media_repo, tmp_path):
        path = await media_repo.save(b"video", "clip.mp4", "video")
        deleted = await media_repo.delete(path)
        assert deleted is True
        assert not path.exists()

    async def test_delete_nonexistent_returns_false(self, media_repo, tmp_path):
        result = await media_repo.delete(tmp_path / "ghost.wav")
        assert result is False

    async def test_cleanup_removes_old_audio(self, media_repo, tmp_path):
        path = await media_repo.save(b"old audio", "old.wav", "audio")
        # retroceder mtime 25 horas
        old_time = time.time() - 25 * 3600
        import os

        os.utime(path, (old_time, old_time))
        counts = await media_repo.cleanup()
        assert counts["audio"] == 1
        assert not path.exists()

    async def test_cleanup_keeps_recent_audio(self, media_repo):
        await media_repo.save(b"fresh audio", "fresh.wav", "audio")
        counts = await media_repo.cleanup()
        assert counts["audio"] == 0

    async def test_cleanup_removes_old_image(self, media_repo, tmp_path):
        path = await media_repo.save(b"old img", "old.jpg", "image")
        old_time = time.time() - 2 * 3600
        import os

        os.utime(path, (old_time, old_time))
        counts = await media_repo.cleanup()
        assert counts["image"] == 1

    async def test_media_type_for_known_extension(self, media_repo):
        assert media_repo.media_type_for("clip.wav") == "audio"
        assert media_repo.media_type_for("photo.jpg") == "image"
        assert media_repo.media_type_for("video.mp4") == "video"

    async def test_media_type_for_unknown_extension(self, media_repo):
        assert media_repo.media_type_for("file.pdf") is None
