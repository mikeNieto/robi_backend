"""
Tests unitarios — Paso 1: Modelos + Base de Datos

Cubre:
  - Serialización / validación de modelos Pydantic (requests, responses, ws_messages)
  - Creación de tablas SQLAlchemy con SQLite in-memory
  - Constraints básicos (unique, nullable, foreign key)
  - Dataclasses de dominio (entities)
"""

import json
import pytest
import pytest_asyncio
from datetime import datetime

from db import (
    ConversationHistoryRow,
    InteractionRow,
    MemoryRow,
    UserRow,
    create_all_tables,
    drop_all_tables,
    init_db,
)
from models.entities import ConversationMessage, Interaction, Memory, User
from models.requests import FaceRegisterRequest, MemorySaveRequest
from models.responses import (
    ErrorResponse,
    HealthResponse,
    MemoryItemResponse,
    UserResponse,
)
from models.ws_messages import (
    AudioEndMessage,
    AuthMessage,
    AuthOkMessage,
    EmotionMessage,
    InteractionStartMessage,
    ResponseMetaMessage,
    StreamEndMessage,
    TextChunkMessage,
    TextMessage,
    WsErrorMessage,
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
    def test_user_defaults(self):
        u = User(user_id="user_juan_1", name="Juan")
        assert u.face_embedding is None
        assert u.preferences == {}
        assert u.id is None
        assert isinstance(u.created_at, datetime)

    def test_memory_defaults(self):
        m = Memory(
            user_id="user_juan_1", memory_type="fact", content="Le gusta el café"
        )
        assert m.importance == 5
        assert m.expires_at is None

    def test_interaction_defaults(self):
        i = Interaction(
            user_id="user_juan_1", request_type="audio", summary="Preguntó la hora"
        )
        assert isinstance(i.timestamp, datetime)

    def test_conversation_message_defaults(self):
        msg = ConversationMessage(session_id="sess-1", role="user", content="Hola")
        assert msg.is_compacted is False
        assert msg.message_index == 0


# ═══════════════════════════════════════════════════════
# Modelos Pydantic — Requests
# ═══════════════════════════════════════════════════════


class TestRequestModels:
    def test_face_register_valid(self):
        req = FaceRegisterRequest(
            user_id="user_juan_1",
            name="Juan",
            embedding_b64="abc123==",
        )
        assert req.user_id == "user_juan_1"

    def test_face_register_empty_user_id_fails(self):
        with pytest.raises(Exception):
            FaceRegisterRequest(user_id="", name="Juan", embedding_b64="abc")

    def test_memory_save_valid(self):
        req = MemorySaveRequest(
            memory_type="preference", content="Le gusta el café", importance=8
        )
        assert req.importance == 8

    def test_memory_save_invalid_type(self):
        with pytest.raises(Exception):
            MemorySaveRequest(memory_type="secret", content="Contraseña", importance=5)

    def test_memory_save_importance_out_of_range(self):
        with pytest.raises(Exception):
            MemorySaveRequest(memory_type="fact", content="x", importance=11)


# ═══════════════════════════════════════════════════════
# Modelos Pydantic — Responses
# ═══════════════════════════════════════════════════════


class TestResponseModels:
    def test_health_response_defaults(self):
        r = HealthResponse()
        assert r.status == "ok"
        assert r.version == "1.4"

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

    def test_user_response_serializes(self):
        u = UserResponse(
            user_id="user_juan_1",
            name="Juan",
            created_at="2026-01-01T00:00:00Z",
            last_seen="2026-02-20T10:00:00Z",
            has_face_embedding=True,
        )
        assert u.has_face_embedding is True

    def test_memory_item_importance_bounds(self):
        with pytest.raises(Exception):
            MemoryItemResponse(
                id=1,
                memory_type="fact",
                content="x",
                importance=0,
                timestamp="2026-01-01T00:00:00Z",
            )


# ═══════════════════════════════════════════════════════
# Modelos WS — mensajes del cliente
# ═══════════════════════════════════════════════════════


class TestWsClientMessages:
    def test_auth_message(self):
        msg = AuthMessage(type="auth", api_key="secret", device_id="dev-1")
        assert msg.type == "auth"

    def test_interaction_start_unknown_user(self):
        msg = InteractionStartMessage(
            type="interaction_start",
            request_id="req-1",
            user_id="unknown",
            face_recognized=False,
        )
        assert msg.face_confidence is None

    def test_audio_end(self):
        msg = AudioEndMessage(type="audio_end", request_id="req-1")
        assert msg.request_id == "req-1"

    def test_text_message(self):
        msg = TextMessage(type="text", request_id="req-2", content="¿Qué hora es?")
        assert msg.content == "¿Qué hora es?"

    def test_discriminated_union_auth(self):
        """Pydantic puede parsear el tipo correcto a partir del campo type."""
        from pydantic import TypeAdapter

        ta = TypeAdapter(AuthMessage | InteractionStartMessage | AudioEndMessage)
        raw = {"type": "auth", "api_key": "k", "device_id": "d"}
        parsed = ta.validate_python(raw)
        assert isinstance(parsed, AuthMessage)


# ═══════════════════════════════════════════════════════
# Modelos WS — mensajes del servidor
# ═══════════════════════════════════════════════════════


class TestWsServerMessages:
    def test_auth_ok(self):
        msg = AuthOkMessage(session_id="sess-abc")
        assert msg.type == "auth_ok"
        data = json.loads(msg.model_dump_json())
        assert data["type"] == "auth_ok"

    def test_emotion_message(self):
        msg = EmotionMessage(
            request_id="req-1",
            emotion="happy",
            user_identified="user_juan_1",
            confidence=0.95,
        )
        assert msg.type == "emotion"

    def test_text_chunk(self):
        msg = TextChunkMessage(request_id="req-1", text="Hola Juan, ")
        assert msg.text == "Hola Juan, "

    def test_response_meta_serializes(self):
        from models.ws_messages import ExpressionPayload

        msg = ResponseMetaMessage(
            request_id="req-1",
            response_text="Hola!",
            expression=ExpressionPayload(emojis=["1F44B", "1F603"]),
            actions=[
                {
                    "type": "move",
                    "params": {"direction": "forward", "duration_ms": 1000},
                }
            ],
        )
        data = json.loads(msg.model_dump_json())
        assert data["type"] == "response_meta"
        assert "1F44B" in data["expression"]["emojis"]

    def test_stream_end(self):
        msg = StreamEndMessage(request_id="req-1", processing_time_ms=820)
        assert msg.processing_time_ms == 820

    def test_ws_error(self):
        msg = WsErrorMessage(error_code="AUTH_FAILED", message="Clave inválida")
        assert msg.recoverable is False
        assert msg.type == "error"


# ═══════════════════════════════════════════════════════
# Base de Datos — creación de tablas y constraints
# ═══════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_tables_created(db_session: AsyncSession):
    """Las 4 tablas deben existir tras create_all_tables()."""
    for table in ("users", "memories", "interactions", "conversation_history"):
        result = await db_session.execute(
            text(
                f"SELECT name FROM sqlite_master WHERE type='table' AND name='{table}'"
            )
        )
        assert result.scalar() == table, f"La tabla '{table}' no fue creada"


@pytest.mark.asyncio
async def test_user_unique_constraint(db_session: AsyncSession):
    """Insertar dos usuarios con el mismo user_id debe fallar por UNIQUE."""
    from sqlalchemy.exc import IntegrityError

    u1 = UserRow(user_id="user_dup", name="Dup 1", preferences="{}")
    u2 = UserRow(user_id="user_dup", name="Dup 2", preferences="{}")
    db_session.add(u1)
    await db_session.flush()
    db_session.add(u2)
    with pytest.raises(IntegrityError):
        await db_session.flush()


@pytest.mark.asyncio
async def test_insert_user_and_memory(db_session: AsyncSession):
    user = UserRow(user_id="user_test_1", name="Test User", preferences="{}")
    db_session.add(user)
    await db_session.flush()

    mem = MemoryRow(
        user_id="user_test_1",
        memory_type="fact",
        content="Le gusta el café",
        importance=7,
    )
    db_session.add(mem)
    await db_session.flush()
    assert mem.id is not None


@pytest.mark.asyncio
async def test_insert_interaction(db_session: AsyncSession):
    user = UserRow(user_id="user_test_2", name="Another", preferences="{}")
    db_session.add(user)
    await db_session.flush()

    inter = InteractionRow(
        user_id="user_test_2",
        request_type="audio",
        summary="Preguntó por el clima",
    )
    db_session.add(inter)
    await db_session.flush()
    assert inter.id is not None


@pytest.mark.asyncio
async def test_insert_conversation_history(db_session: AsyncSession):
    row = ConversationHistoryRow(
        session_id="sess-unit-1",
        role="user",
        content="Hola Robi",
        message_index=0,
    )
    db_session.add(row)
    await db_session.flush()
    assert row.id is not None


@pytest.mark.asyncio
async def test_memory_importance_nullable_expires(db_session: AsyncSession):
    """expires_at es nullable; importance tiene default 5."""
    user = UserRow(user_id="user_test_3", name="Test 3", preferences="{}")
    db_session.add(user)
    await db_session.flush()

    mem = MemoryRow(
        user_id="user_test_3", memory_type="preference", content="Prefiere jazz"
    )
    db_session.add(mem)
    await db_session.flush()
    assert mem.importance == 5
    assert mem.expires_at is None


@pytest.mark.asyncio
async def test_conversation_compacted_flag(db_session: AsyncSession):
    row = ConversationHistoryRow(
        session_id="sess-compact",
        role="assistant",
        content="[Resumen: El usuario habló de música y clima]",
        message_index=0,
        is_compacted=True,
    )
    db_session.add(row)
    await db_session.flush()
    assert row.is_compacted is True
