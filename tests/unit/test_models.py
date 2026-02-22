"""
Tests unitarios v2.0 — Modelos + Base de Datos

Cubre:
  - Entidades de dominio v2.0 (Person, FaceEmbedding, Zone, ZonePath, Memory)
  - Modelos Pydantic WS messages v2.0 (client + server)
  - Modelos Pydantic responses (RestoreResponse, HealthResponse, ErrorResponse)
  - Creación de tablas SQLAlchemy con SQLite in-memory
  - Constraints (unique, nullable, foreign key)
"""

import json
import pytest
import pytest_asyncio
from datetime import datetime

from db import (
    ConversationHistoryRow,
    FaceEmbeddingRow,
    MemoryRow,
    PersonRow,
    ZoneRow,
    ZonePathRow,
    create_all_tables,
    drop_all_tables,
    init_db,
)
from models.entities import (
    ConversationMessage,
    Memory,
    Person,
    Zone,
    ZonePath,
    ZONE_CATEGORIES,
    MEMORY_TYPES,
)
from models.responses import (
    ErrorResponse,
    HealthResponse,
    RestoreMemoryResponse,
    RestorePersonResponse,
    RestoreResponse,
    RestoreZonePathResponse,
    RestoreZoneResponse,
)
from models.ws_messages import (
    AudioEndMessage,
    AuthMessage,
    AuthOkMessage,
    EmotionMessage,
    ExploreModeMessage,
    ExpressionPayload,
    FaceScanModeMessage,
    InteractionStartMessage,
    LowBatteryAlertMessage,
    PersonDetectedMessage,
    ResponseMetaMessage,
    StreamEndMessage,
    TextChunkMessage,
    TextMessage,
    WsErrorMessage,
    ZoneUpdateMessage,
)
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


# ── Fixture: BD in-memory ─────────────────────────────────────────────────────

TEST_DB_URL = "sqlite+aiosqlite:///:memory:"


@pytest_asyncio.fixture
async def db_session():
    """Sesión con BD in-memory; tablas creadas y destruidas por test."""
    init_db(TEST_DB_URL)
    await create_all_tables()
    from db import AsyncSessionLocal

    assert AsyncSessionLocal is not None
    async with AsyncSessionLocal() as session:
        yield session
    await drop_all_tables()


# ═══════════════════════════════════════════════════════
# Dataclasses de dominio
# ═══════════════════════════════════════════════════════


class TestEntities:
    def test_person_defaults(self):
        p = Person(person_id="persona_ana_001", name="Ana")
        assert p.id is None
        assert p.interaction_count == 0
        assert p.notes == ""
        assert isinstance(p.first_seen, datetime)
        assert isinstance(p.last_seen, datetime)

    def test_memory_defaults_v2(self):
        m = Memory(memory_type="general", content="Hay un gato en casa")
        assert m.person_id is None
        assert m.zone_id is None
        assert m.importance == 5
        assert m.expires_at is None

    def test_memory_with_person(self):
        m = Memory(
            memory_type="person_fact",
            content="Ana le gusta el café",
            person_id="persona_ana_001",
            importance=7,
        )
        assert m.person_id == "persona_ana_001"

    def test_zone_defaults(self):
        z = Zone(name="sala", category="living_area")
        assert z.accessible is True
        assert z.current_robi_zone is False
        assert z.description == ""

    def test_zone_path_fields(self):
        zp = ZonePath(from_zone_id=1, to_zone_id=2, direction_hint="norte")
        assert zp.distance_cm is None

    def test_conversation_message_defaults(self):
        msg = ConversationMessage(session_id="sess-1", role="user", content="Hola")
        assert msg.is_compacted is False
        assert msg.message_index == 0

    def test_zone_categories_constant(self):
        assert "living_area" in ZONE_CATEGORIES
        assert "bedroom" in ZONE_CATEGORIES

    def test_memory_types_constant(self):
        assert "general" in MEMORY_TYPES
        assert "person_fact" in MEMORY_TYPES
        assert "zone_info" in MEMORY_TYPES
        assert "experience" in MEMORY_TYPES


# ═══════════════════════════════════════════════════════
# Modelos Pydantic — Responses v2.0
# ═══════════════════════════════════════════════════════


class TestResponseModels:
    def test_health_response_version_2(self):
        r = HealthResponse()
        assert r.status == "ok"
        assert r.version == "2.0"

    def test_error_response_serializes(self):
        err = ErrorResponse(
            error_code="GEMINI_TIMEOUT",
            message="Timeout",
            recoverable=True,
            retry_after=5,
            timestamp="2026-02-20T12:00:00Z",
        )
        data = json.loads(err.model_dump_json())
        assert data["error"] is True
        assert data["error_code"] == "GEMINI_TIMEOUT"
        assert data["retry_after"] == 5

    def test_restore_person_response(self):
        r = RestorePersonResponse(
            person_id="persona_ana_001",
            name="Ana",
            first_seen=datetime(2026, 1, 1),
            last_seen=datetime(2026, 2, 1),
            interaction_count=10,
            notes="Le gusta el café",
            face_embeddings=["abc==", "def=="],
        )
        assert len(r.face_embeddings) == 2

    def test_restore_zone_response(self):
        path = RestoreZonePathResponse(to_zone_id=2, direction_hint="norte")
        zone = RestoreZoneResponse(
            id=1,
            name="sala",
            category="living_area",
            known_since=datetime(2026, 1, 1),
            accessible=True,
            is_current=True,
            paths=[path],
        )
        assert zone.is_current is True
        assert len(zone.paths) == 1

    def test_restore_memory_response(self):
        m = RestoreMemoryResponse(
            id=5,
            memory_type="general",
            content="La casa tiene 3 habitaciones",
            importance=6,
            created_at=datetime(2026, 1, 1),
        )
        assert m.person_id is None
        assert m.zone_id is None

    def test_restore_response_full(self):
        r = RestoreResponse(people=[], zones=[], general_memories=[])
        data = json.loads(r.model_dump_json())
        assert "people" in data
        assert "zones" in data
        assert "general_memories" in data


# ═══════════════════════════════════════════════════════
# Modelos WS — mensajes del cliente
# ═══════════════════════════════════════════════════════


class TestWsClientMessages:
    def test_auth_message(self):
        msg = AuthMessage(type="auth", api_key="secret", device_id="dev-1")
        assert msg.type == "auth"

    def test_interaction_start_with_person_id(self):
        msg = InteractionStartMessage(
            type="interaction_start",
            request_id="req-1",
            person_id="persona_ana_001",
        )
        assert msg.person_id == "persona_ana_001"
        assert msg.face_embedding is None

    def test_interaction_start_with_embedding(self):
        msg = InteractionStartMessage(
            type="interaction_start",
            request_id="req-1",
            face_embedding="abc123==",
        )
        assert msg.face_embedding == "abc123=="

    def test_interaction_start_anon(self):
        msg = InteractionStartMessage(
            type="interaction_start",
            request_id="req-1",
        )
        assert msg.person_id is None

    def test_audio_end_with_face_embedding(self):
        msg = AudioEndMessage(
            type="audio_end",
            request_id="req-1",
            face_embedding="xyz==",
        )
        assert msg.face_embedding == "xyz=="

    def test_text_message_no_embedding(self):
        msg = TextMessage(type="text", request_id="req-2", content="¿Qué hora es?")
        assert msg.face_embedding is None

    def test_explore_mode_message(self):
        msg = ExploreModeMessage(
            type="explore_mode",
            request_id="req-3",
            duration_minutes=10,
        )
        assert 1 <= msg.duration_minutes <= 60

    def test_explore_mode_bounds(self):
        with pytest.raises(Exception):
            ExploreModeMessage(
                type="explore_mode", request_id="req", duration_minutes=0
            )
        with pytest.raises(Exception):
            ExploreModeMessage(
                type="explore_mode", request_id="req", duration_minutes=61
            )

    def test_face_scan_mode_message(self):
        msg = FaceScanModeMessage(type="face_scan_mode", request_id="req-4")
        assert msg.type == "face_scan_mode"

    def test_zone_update_message(self):
        msg = ZoneUpdateMessage(
            type="zone_update",
            request_id="req-5",
            zone_name="cocina",
            category="kitchen",
            action="enter",
        )
        assert msg.action == "enter"

    def test_zone_update_invalid_action(self):
        with pytest.raises(Exception):
            ZoneUpdateMessage(
                type="zone_update",
                request_id="req",
                zone_name="sala",
                action="fly",  # type: ignore[arg-type]
            )

    def test_person_detected_known(self):
        msg = PersonDetectedMessage(
            type="person_detected",
            request_id="req-6",
            known=True,
            person_id="persona_ana_001",
            confidence=0.92,
        )
        assert msg.known is True
        assert msg.confidence == 0.92

    def test_person_detected_unknown(self):
        msg = PersonDetectedMessage(
            type="person_detected",
            request_id="req-7",
            known=False,
            confidence=0.0,
        )
        assert msg.person_id is None


# ═══════════════════════════════════════════════════════
# Modelos WS — mensajes del servidor
# ═══════════════════════════════════════════════════════


class TestWsServerMessages:
    def test_auth_ok(self):
        msg = AuthOkMessage(session_id="sess-abc")
        data = json.loads(msg.model_dump_json())
        assert data["type"] == "auth_ok"

    def test_emotion_with_person_identified(self):
        msg = EmotionMessage(
            request_id="req-1",
            emotion="happy",
            person_identified="persona_ana_001",
            confidence=0.95,
        )
        assert msg.type == "emotion"
        assert msg.person_identified == "persona_ana_001"

    def test_emotion_no_person(self):
        msg = EmotionMessage(request_id="req-1", emotion="neutral")
        assert msg.person_identified is None

    def test_text_chunk(self):
        msg = TextChunkMessage(request_id="req-1", text="Hola Ana, ")
        assert msg.text == "Hola Ana, "

    def test_response_meta_with_person_name(self):
        msg = ResponseMetaMessage(
            request_id="req-1",
            response_text="¡Hola María!",
            expression=ExpressionPayload(emojis=["1F44B"]),
            actions=[],
            person_name="María",
        )
        data = json.loads(msg.model_dump_json())
        assert data["person_name"] == "María"

    def test_response_meta_person_name_none(self):
        msg = ResponseMetaMessage(
            request_id="req-1",
            response_text="Hola!",
            expression=ExpressionPayload(emojis=[]),
            actions=[],
        )
        assert msg.person_name is None

    def test_stream_end(self):
        msg = StreamEndMessage(request_id="req-1", processing_time_ms=820)
        assert msg.processing_time_ms == 820

    def test_ws_error(self):
        msg = WsErrorMessage(error_code="AUTH_FAILED", message="Clave inválida")
        assert msg.recoverable is False
        assert msg.type == "error"

    def test_low_battery_alert(self):
        msg = LowBatteryAlertMessage(battery_level=12, source="robot")
        assert msg.type == "low_battery_alert"
        assert msg.battery_level == 12


# ═══════════════════════════════════════════════════════
# Base de Datos — creación de tablas y constraints
# ═══════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_tables_created(db_session: AsyncSession):
    """Las 6 tablas deben existir tras create_all_tables()."""
    expected = (
        "people",
        "face_embeddings",
        "zones",
        "zone_paths",
        "memories",
        "conversation_history",
    )
    for table in expected:
        result = await db_session.execute(
            text(
                f"SELECT name FROM sqlite_master WHERE type='table' AND name='{table}'"
            )
        )
        assert result.scalar() == table, f"La tabla '{table}' no fue creada"


@pytest.mark.asyncio
async def test_person_unique_constraint(db_session: AsyncSession):
    """Insertar dos personas con el mismo person_id debe fallar (UNIQUE)."""
    from sqlalchemy.exc import IntegrityError

    p1 = PersonRow(person_id="dup_slug", name="Dup 1")
    p2 = PersonRow(person_id="dup_slug", name="Dup 2")
    db_session.add(p1)
    await db_session.flush()
    db_session.add(p2)
    with pytest.raises(IntegrityError):
        await db_session.flush()


@pytest.mark.asyncio
async def test_insert_person_and_memory(db_session: AsyncSession):
    person = PersonRow(person_id="p_test_1", name="Test")
    db_session.add(person)
    await db_session.flush()

    mem = MemoryRow(
        memory_type="person_fact",
        content="Le gusta el jazz",
        importance=7,
        person_id="p_test_1",
    )
    db_session.add(mem)
    await db_session.flush()
    assert mem.id is not None


@pytest.mark.asyncio
async def test_insert_zone_and_path(db_session: AsyncSession):
    z1 = ZoneRow(name="sala", category="living_area")
    z2 = ZoneRow(name="cocina", category="kitchen")
    db_session.add_all([z1, z2])
    await db_session.flush()

    path = ZonePathRow(from_zone_id=z1.id, to_zone_id=z2.id, direction_hint="norte")
    db_session.add(path)
    await db_session.flush()
    assert path.id is not None


@pytest.mark.asyncio
async def test_memory_nullable_person(db_session: AsyncSession):
    """person_id es nullable: memoria general sin persona asociada."""
    mem = MemoryRow(
        memory_type="general",
        content="La casa tiene jardín",
        importance=5,
    )
    db_session.add(mem)
    await db_session.flush()
    assert mem.person_id is None


@pytest.mark.asyncio
async def test_face_embedding_row(db_session: AsyncSession):
    person = PersonRow(person_id="p_emb_1", name="Embed Test")
    db_session.add(person)
    await db_session.flush()

    emb = FaceEmbeddingRow(
        person_id="p_emb_1",
        embedding=b"\x01\x02\x03" * 43,
    )
    db_session.add(emb)
    await db_session.flush()
    assert emb.id is not None


@pytest.mark.asyncio
async def test_conversation_history_compacted(db_session: AsyncSession):
    row = ConversationHistoryRow(
        session_id="sess-compact",
        role="assistant",
        content="[Resumen compactado]",
        message_index=0,
        is_compacted=True,
    )
    db_session.add(row)
    await db_session.flush()
    assert row.is_compacted is True
