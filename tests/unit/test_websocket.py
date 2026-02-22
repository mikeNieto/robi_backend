"""
tests/unit/test_websocket.py — Tests unitarios para ws_handlers/ (v2.0)

Cubre:
  - ws_handlers/protocol.py: funciones builder de mensajes
  - ws_handlers/auth.py: authenticate_websocket
  - ws_handlers/streaming.py: _process_interaction, _load_robi_context, ws_interact flow
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.websockets import WebSocketState

import db as db_module
from db import init_db
from services.history import ConversationHistory
from ws_handlers.auth import authenticate_websocket
from ws_handlers.protocol import (
    make_auth_ok,
    make_capture_request,
    make_emotion,
    make_error,
    make_exploration_actions,
    make_face_scan_actions,
    make_low_battery_alert,
    make_response_meta,
    make_stream_end,
    make_text_chunk,
    new_session_id,
)
from ws_handlers.streaming import (
    _load_robi_context,
    _process_interaction,
    ws_interact,
)

# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
async def setup_db():
    """Inicializa la BD in-memory para los tests que la necesiten."""
    init_db("sqlite+aiosqlite:///:memory:")
    from db import create_all_tables

    await create_all_tables()
    yield
    if db_module.engine is not None:
        await db_module.engine.dispose()


def make_mock_ws(
    *,
    receive_text_values: list[str] | None = None,
    receive_messages: list[dict] | None = None,
) -> MagicMock:
    """
    Crea un MagicMock de WebSocket con estado CONNECTED.
    receive_text_values: lista de strings devueltos por receive_text()
    receive_messages: lista de dicts devueltos por receive() (streaming loop)
    """
    ws = MagicMock()
    ws.client_state = WebSocketState.CONNECTED
    ws.accept = AsyncMock()
    ws.send_text = AsyncMock()
    ws.close = AsyncMock()

    if receive_text_values is not None:
        ws.receive_text = AsyncMock(side_effect=receive_text_values)

    if receive_messages is not None:
        messages = list(receive_messages)
        if not any(m.get("type") == "websocket.disconnect" for m in messages):
            messages.append({"type": "websocket.disconnect"})
        ws.receive = AsyncMock(side_effect=messages)

    return ws


# ═══════════════════════════════════════════════════════════════════════════════
# SECCIÓN 1 — Protocol builders
# ═══════════════════════════════════════════════════════════════════════════════


class TestProtocolBuilders:
    def test_make_auth_ok(self):
        result = json.loads(make_auth_ok("sess-123"))
        assert result == {"type": "auth_ok", "session_id": "sess-123"}

    def test_make_emotion_minimal(self):
        result = json.loads(make_emotion("req-1", "happy"))
        assert result["type"] == "emotion"
        assert result["request_id"] == "req-1"
        assert result["emotion"] == "happy"
        assert "person_identified" not in result
        assert "confidence" not in result

    def test_make_emotion_with_person_and_confidence(self):
        result = json.loads(
            make_emotion(
                "req-2", "love", person_identified="persona_ana_001", confidence=0.95
            )
        )
        assert result["person_identified"] == "persona_ana_001"
        assert result["confidence"] == pytest.approx(0.95)

    def test_make_text_chunk(self):
        result = json.loads(make_text_chunk("req-3", "Hola mundo"))
        assert result == {
            "type": "text_chunk",
            "request_id": "req-3",
            "text": "Hola mundo",
        }

    def test_make_stream_end_default(self):
        result = json.loads(make_stream_end("req-4"))
        assert result["type"] == "stream_end"
        assert result["request_id"] == "req-4"
        assert result["processing_time_ms"] == 0

    def test_make_stream_end_with_time(self):
        result = json.loads(make_stream_end("req-5", processing_time_ms=850))
        assert result["processing_time_ms"] == 850

    def test_make_error_minimal(self):
        result = json.loads(make_error("GEMINI_TIMEOUT", "Timeout"))
        assert result["type"] == "error"
        assert result["error_code"] == "GEMINI_TIMEOUT"
        assert result["recoverable"] is False
        assert "request_id" not in result

    def test_make_error_with_request_id_and_recoverable(self):
        result = json.loads(
            make_error("AGENT_ERROR", "Fallo", request_id="req-6", recoverable=True)
        )
        assert result["request_id"] == "req-6"
        assert result["recoverable"] is True

    def test_make_response_meta_structure(self):
        result = json.loads(make_response_meta("req-7", "Hola!", ["1F600", "1F603"]))
        assert result["type"] == "response_meta"
        assert result["response_text"] == "Hola!"
        assert result["expression"]["emojis"] == ["1F600", "1F603"]
        assert result["expression"]["transition"] == "bounce"
        assert result["actions"] == []

    def test_make_response_meta_with_actions(self):
        actions = [{"type": "move", "params": {"direction": "forward"}}]
        result = json.loads(make_response_meta("req-8", "text", [], actions=actions))
        assert len(result["actions"]) == 1
        assert result["actions"][0]["type"] == "move"

    def test_make_response_meta_with_person_name(self):
        result = json.loads(
            make_response_meta("req-9", "Hola Ana!", ["1F600"], person_name="Ana")
        )
        assert result["person_name"] == "Ana"

    def test_make_capture_request_photo(self):
        result = json.loads(make_capture_request("req-10"))
        assert result["type"] == "capture_request"
        assert result["capture_type"] == "photo"

    def test_make_capture_request_video(self):
        result = json.loads(make_capture_request("req-11", "video"))
        assert result["capture_type"] == "video"

    def test_make_exploration_actions(self):
        actions = [
            {"action": "turn_right_deg", "params": {"degrees": 90}, "duration_ms": 1000}
        ]
        result = json.loads(
            make_exploration_actions(
                "req-e1", actions, exploration_speech="Explorando..."
            )
        )
        assert result["type"] == "exploration_actions"
        assert result["request_id"] == "req-e1"
        assert result["actions"] == actions
        assert result["exploration_speech"] == "Explorando..."

    def test_make_exploration_actions_empty_speech(self):
        result = json.loads(make_exploration_actions("req-e2", []))
        assert result["type"] == "exploration_actions"
        assert result["exploration_speech"] == ""

    def test_make_face_scan_actions(self):
        actions = [
            {"action": "turn_left_deg", "params": {"degrees": 45}, "duration_ms": 500}
        ]
        result = json.loads(make_face_scan_actions("req-f1", actions))
        assert result["type"] == "face_scan_actions"
        assert result["request_id"] == "req-f1"
        assert result["actions"] == actions

    def test_make_low_battery_alert_robot(self):
        result = json.loads(make_low_battery_alert(12, "robot"))
        assert result["type"] == "low_battery_alert"
        assert result["battery_level"] == 12
        assert result["source"] == "robot"

    def test_make_low_battery_alert_phone(self):
        result = json.loads(make_low_battery_alert(8, "phone"))
        assert result["source"] == "phone"

    def test_new_session_id_is_uuid(self):
        import uuid

        sid = new_session_id()
        parsed = uuid.UUID(sid)
        assert str(parsed) == sid

    def test_new_session_id_unique(self):
        ids = {new_session_id() for _ in range(20)}
        assert len(ids) == 20


# ═══════════════════════════════════════════════════════════════════════════════
# SECCIÓN 2 — authenticate_websocket
# ═══════════════════════════════════════════════════════════════════════════════


class TestAuthenticateWebSocket:
    async def test_valid_api_key(self):
        """API Key correcta → devuelve session_id y envía auth_ok."""
        ws = make_mock_ws(
            receive_text_values=[
                json.dumps(
                    {"type": "auth", "api_key": "test-api-key-for-unit-tests-only"}
                )
            ]
        )
        session_id = await authenticate_websocket(ws)
        assert session_id is not None
        ws.send_text.assert_called_once()
        sent = json.loads(ws.send_text.call_args[0][0])
        assert sent["type"] == "auth_ok"
        assert sent["session_id"] == session_id

    async def test_invalid_api_key(self):
        """API Key incorrecta → devuelve None y cierra con 1008."""
        ws = make_mock_ws(
            receive_text_values=[json.dumps({"type": "auth", "api_key": "wrong-key"})]
        )
        result = await authenticate_websocket(ws)
        assert result is None
        ws.close.assert_called_once_with(code=1008)

    async def test_timeout(self):
        """Timeout esperando auth → devuelve None y cierra."""
        ws = make_mock_ws(receive_text_values=[])
        ws.receive_text = AsyncMock(side_effect=asyncio.TimeoutError)
        result = await authenticate_websocket(ws, timeout=0.001)
        assert result is None
        ws.close.assert_called_once_with(code=1008)

    async def test_wrong_message_type(self):
        """Primer mensaje no es 'auth' → devuelve None y cierra."""
        ws = make_mock_ws(
            receive_text_values=[
                json.dumps({"type": "interaction_start", "person_id": None})
            ]
        )
        result = await authenticate_websocket(ws)
        assert result is None
        ws.close.assert_called_once_with(code=1008)

    async def test_invalid_json(self):
        """Mensaje no es JSON válido → devuelve None y cierra."""
        ws = make_mock_ws(receive_text_values=["not json at all!"])
        result = await authenticate_websocket(ws)
        assert result is None
        ws.close.assert_called_once_with(code=1008)

    async def test_session_id_is_uuid(self):
        """El session_id devuelto es un UUID válido."""
        import uuid

        ws = make_mock_ws(
            receive_text_values=[
                json.dumps(
                    {"type": "auth", "api_key": "test-api-key-for-unit-tests-only"}
                )
            ]
        )
        session_id = await authenticate_websocket(ws)
        assert session_id is not None
        uuid.UUID(session_id)


# ═══════════════════════════════════════════════════════════════════════════════
# SECCIÓN 3 — _load_robi_context
# ═══════════════════════════════════════════════════════════════════════════════


class TestLoadRobiContext:
    async def test_returns_empty_dict_when_db_not_initialized(self):
        """Si AsyncSessionLocal es None → devuelve {}."""
        original = db_module.AsyncSessionLocal
        db_module.AsyncSessionLocal = None
        try:
            ctx = await _load_robi_context("persona_001")
            assert ctx == {}
        finally:
            db_module.AsyncSessionLocal = original

    async def test_returns_dict_for_known_person(self):
        """Con BD inicializada y persona desconocida → devuelve dict sin error."""
        ctx = await _load_robi_context("persona_no_existe_xyz")
        assert isinstance(ctx, dict)

    async def test_returns_dict_for_none_person(self):
        """person_id=None (persona desconocida) → devuelve dict sin error."""
        ctx = await _load_robi_context(None)
        assert isinstance(ctx, dict)

    async def test_returns_dict_with_expected_keys(self):
        """El contexto devuelto contiene claves estándar cuando existen memorias."""
        ctx = await _load_robi_context(None)
        # Puede ser vacío, pero si tiene claves deben ser de los tipos esperados
        for key in ctx:
            assert key in ("general", "person", "zone_info")

    async def test_handles_db_error_gracefully(self):
        """Si la BD lanza excepción → devuelve {} sin propagarla."""
        with patch(
            "ws_handlers.streaming.db_module.AsyncSessionLocal",
            side_effect=RuntimeError("db error"),
        ):
            ctx = await _load_robi_context("persona_001")
            assert ctx == {}


# ═══════════════════════════════════════════════════════════════════════════════
# SECCIÓN 4 — _process_interaction (streaming + tags)
# ═══════════════════════════════════════════════════════════════════════════════


def make_async_gen(*chunks: str):
    """Crea un async generator que yield los chunks dados."""

    async def _gen(*args, **kwargs):
        for c in chunks:
            yield c

    return _gen


class TestProcessInteraction:
    async def _run_process(
        self,
        *chunks: str,
        ws: MagicMock | None = None,
        person_id: str | None = "person_test",
    ) -> MagicMock:
        """Helper: lanza _process_interaction con el mock de agente dado."""
        if ws is None:
            ws = make_mock_ws()

        history_service = ConversationHistory()

        with (
            patch("ws_handlers.streaming.run_agent_stream", make_async_gen(*chunks)),
            patch("ws_handlers.streaming._save_history_bg", new_callable=AsyncMock),
            patch(
                "ws_handlers.streaming._load_robi_context",
                new_callable=AsyncMock,
                return_value={},
            ),
            patch(
                "ws_handlers.streaming.compact_memories_async", new_callable=AsyncMock
            ),
        ):
            await _process_interaction(
                websocket=ws,
                person_id=person_id,
                request_id="req-test",
                user_input="Hola",
                input_type="text",
                history_service=history_service,
                session_id="sess-test",
                agent=None,
            )
            await asyncio.sleep(0)
        return ws

    async def test_emotion_tag_happy(self):
        """El agent emite [emotion:happy] → se envía emotion con 'happy'."""
        ws = await self._run_process("[emotion:happy] Hola amigo!")

        sent_texts = [json.loads(c[0][0]) for c in ws.send_text.call_args_list]
        emotions = [m for m in sent_texts if m["type"] == "emotion"]
        assert len(emotions) == 1
        assert emotions[0]["emotion"] == "happy"

    async def test_emotion_tag_precedes_text_chunk(self):
        """El mensaje 'emotion' se envía ANTES del primer 'text_chunk'."""
        ws = await self._run_process("[emotion:neutral] Hola!")

        types_in_order = [
            json.loads(c[0][0])["type"] for c in ws.send_text.call_args_list
        ]
        if "emotion" in types_in_order and "text_chunk" in types_in_order:
            assert types_in_order.index("emotion") < types_in_order.index("text_chunk")

    async def test_no_emotion_tag_defaults_to_neutral(self):
        """Sin emotion tag → se emite emotion='neutral'."""
        ws = await self._run_process("Buenas noches, estoy bien.")

        sent = [json.loads(c[0][0]) for c in ws.send_text.call_args_list]
        emotions = [m for m in sent if m["type"] == "emotion"]
        assert emotions[0]["emotion"] == "neutral"

    async def test_emotion_tag_split_across_chunks(self):
        """Emotion tag partido en varios chunks → se detecta correctamente."""
        ws = await self._run_process("[emot", "ion:sad]", " Lo siento.")

        sent = [json.loads(c[0][0]) for c in ws.send_text.call_args_list]
        emotions = [m for m in sent if m["type"] == "emotion"]
        assert emotions[0]["emotion"] == "sad"

    async def test_text_chunks_sent(self):
        """Después de la emoción se envían text_chunks."""
        ws = await self._run_process("[emotion:happy] Parte uno.", " Parte dos.")

        sent = [json.loads(c[0][0]) for c in ws.send_text.call_args_list]
        chunks = [m["text"] for m in sent if m["type"] == "text_chunk"]
        full_text = "".join(chunks)
        assert "Parte uno." in full_text

    async def test_stream_end_is_sent(self):
        """Siempre se envía stream_end al final."""
        ws = await self._run_process("[emotion:neutral] Hola.")

        sent = [json.loads(c[0][0]) for c in ws.send_text.call_args_list]
        ends = [m for m in sent if m["type"] == "stream_end"]
        assert len(ends) == 1
        assert ends[0]["request_id"] == "req-test"

    async def test_response_meta_is_sent(self):
        """Se envía response_meta con emojis antes del stream_end."""
        ws = await self._run_process("[emotion:excited] ¡Qué emoción!")

        sent = [json.loads(c[0][0]) for c in ws.send_text.call_args_list]
        metas = [m for m in sent if m["type"] == "response_meta"]
        assert len(metas) == 1
        assert isinstance(metas[0]["expression"]["emojis"], list)
        assert len(metas[0]["expression"]["emojis"]) > 0

    async def test_response_meta_before_stream_end(self):
        """response_meta siempre precede a stream_end."""
        ws = await self._run_process("[emotion:neutral] OK.")

        types = [json.loads(c[0][0])["type"] for c in ws.send_text.call_args_list]
        assert types.index("response_meta") < types.index("stream_end")

    async def test_person_identified_in_emotion(self):
        """Con person_id definido, emotion incluye person_identified."""
        ws = await self._run_process(
            "[emotion:happy] Hola Ana!", person_id="persona_ana_001"
        )

        sent = [json.loads(c[0][0]) for c in ws.send_text.call_args_list]
        emotions = [m for m in sent if m["type"] == "emotion"]
        assert emotions[0].get("person_identified") == "persona_ana_001"

    async def test_empty_stream_emits_neutral(self):
        """Stream vacío → se emite emotion neutral y stream_end."""
        ws = await self._run_process()

        sent = [json.loads(c[0][0]) for c in ws.send_text.call_args_list]
        emotions = [m for m in sent if m["type"] == "emotion"]
        ends = [m for m in sent if m["type"] == "stream_end"]
        assert len(emotions) == 1
        assert len(ends) == 1

    async def test_agent_error_sends_error_message(self):
        """Si el agente lanza excepción → se envía error y no stream_end."""
        ws = make_mock_ws()

        async def failing_stream(*args, **kwargs):
            raise RuntimeError("agente caído")
            yield  # pragma: no cover

        history_service = ConversationHistory()
        with (
            patch("ws_handlers.streaming.run_agent_stream", failing_stream),
            patch(
                "ws_handlers.streaming._load_robi_context",
                new_callable=AsyncMock,
                return_value={},
            ),
            patch(
                "ws_handlers.streaming.compact_memories_async", new_callable=AsyncMock
            ),
        ):
            await _process_interaction(
                websocket=ws,
                person_id=None,
                request_id="req-err",
                user_input="Hola",
                input_type="text",
                history_service=history_service,
                session_id="sess-err",
                agent=None,
            )

        sent = [json.loads(c[0][0]) for c in ws.send_text.call_args_list]
        errors = [m for m in sent if m["type"] == "error"]
        assert len(errors) >= 1
        assert errors[0]["error_code"] == "AGENT_ERROR"

    async def test_memory_tag_stripped_from_response_meta(self):
        """[memory:...] es eliminado del response_meta.response_text."""
        ws = await self._run_process(
            "[emotion:happy] ¡Hola! [memory:preference:Le gusta el café] Hasta luego."
        )
        sent = [json.loads(c[0][0]) for c in ws.send_text.call_args_list]
        meta = next((m for m in sent if m["type"] == "response_meta"), None)
        assert meta is not None
        assert "[memory:" not in meta["response_text"]

    async def test_person_name_tag_reflected_in_response_meta(self):
        """[person_name:Ana] → response_meta.person_name contiene 'Ana'."""
        ws = await self._run_process(
            "[emotion:happy][emojis:1F600] Hola [person_name:Ana], ¿cómo estás?"
        )
        sent = [json.loads(c[0][0]) for c in ws.send_text.call_args_list]
        metas = [m for m in sent if m["type"] == "response_meta"]
        assert len(metas) == 1
        assert metas[0].get("person_name") == "Ana"


# ═══════════════════════════════════════════════════════════════════════════════
# SECCIÓN 4b — _process_interaction con media (audio/imagen/video)
# ═══════════════════════════════════════════════════════════════════════════════


class TestProcessInteractionMedia:
    """Tests del flujo media: extracción de [media_summary:] y uso en historial."""

    async def _run_process_audio(self, *chunks: str) -> tuple[MagicMock, AsyncMock]:
        """Helper: lanza _process_interaction con audio_data y devuelve (ws, save_history_mock)."""
        ws = make_mock_ws()
        history_service = ConversationHistory()
        save_history_mock = AsyncMock()

        with (
            patch("ws_handlers.streaming.run_agent_stream", make_async_gen(*chunks)),
            patch("ws_handlers.streaming._save_history_bg", save_history_mock),
            patch(
                "ws_handlers.streaming._load_robi_context",
                new_callable=AsyncMock,
                return_value={},
            ),
            patch(
                "ws_handlers.streaming.compact_memories_async", new_callable=AsyncMock
            ),
        ):
            await _process_interaction(
                websocket=ws,
                person_id=None,
                request_id="req-audio",
                user_input=None,
                input_type="audio",
                audio_data=b"\x00\x01\x02",
                history_service=history_service,
                session_id="sess-test",
                agent=None,
            )
            await asyncio.sleep(0)

        return ws, save_history_mock

    async def test_media_summary_tag_not_sent_as_text_chunk(self):
        """El tag [media_summary:...] NO debe aparecer en los text_chunks enviados."""
        ws, _ = await self._run_process_audio(
            "[emotion:happy][media_summary: el usuario dice buenos días] ¡Buenos días!"
        )
        sent = [json.loads(c[0][0]) for c in ws.send_text.call_args_list]
        chunks_text = "".join(m["text"] for m in sent if m["type"] == "text_chunk")
        assert "[media_summary:" not in chunks_text
        assert "¡Buenos días!" in chunks_text

    async def test_media_summary_used_as_user_history(self):
        """El contenido del [media_summary:...] se usa como mensaje del usuario en el historial."""
        _, save_history_mock = await self._run_process_audio(
            "[emotion:happy][media_summary: el usuario saluda y pregunta cómo está] ¡Hola!"
        )
        assert save_history_mock.called
        kwargs = save_history_mock.call_args.kwargs
        assert kwargs["user_message"] == "el usuario saluda y pregunta cómo está"

    async def test_media_summary_split_across_chunks(self):
        """El tag [media_summary:...] llegando partido en varios chunks se extrae correctamente."""
        ws, save_history_mock = await self._run_process_audio(
            "[emotion:neutral][media_summary: ",
            "usuario envía nota de voz",
            "] Entendido.",
        )
        kwargs = save_history_mock.call_args.kwargs
        assert kwargs["user_message"] == "usuario envía nota de voz"
        sent = [json.loads(c[0][0]) for c in ws.send_text.call_args_list]
        chunks_text = "".join(m["text"] for m in sent if m["type"] == "text_chunk")
        assert "[media_summary:" not in chunks_text

    async def test_fallback_placeholder_when_no_summary_tag(self):
        """Si el LLM no emite [media_summary:...] el historial usa '[audio]'."""
        _, save_history_mock = await self._run_process_audio(
            "[emotion:happy] El audio no tenía etiqueta de resumen."
        )
        kwargs = save_history_mock.call_args.kwargs
        assert kwargs["user_message"] == "[audio]"

    async def test_text_input_not_affected(self):
        """Las interacciones de texto siguen usando user_input como mensaje de historial."""
        ws = make_mock_ws()
        history_service = ConversationHistory()
        save_history_mock = AsyncMock()

        with (
            patch(
                "ws_handlers.streaming.run_agent_stream",
                make_async_gen("[emotion:neutral] OK."),
            ),
            patch("ws_handlers.streaming._save_history_bg", save_history_mock),
            patch(
                "ws_handlers.streaming._load_robi_context",
                new_callable=AsyncMock,
                return_value={},
            ),
            patch(
                "ws_handlers.streaming.compact_memories_async", new_callable=AsyncMock
            ),
        ):
            await _process_interaction(
                websocket=ws,
                person_id=None,
                request_id="req-text",
                user_input="¿Qué hora es?",
                input_type="text",
                history_service=history_service,
                session_id="sess-test",
                agent=None,
            )
            await asyncio.sleep(0)

        kwargs = save_history_mock.call_args.kwargs
        assert kwargs["user_message"] == "¿Qué hora es?"

    async def test_emotion_still_sent_for_audio(self):
        """Con audio, el tag [emotion:...] se extrae y envía correctamente."""
        ws, _ = await self._run_process_audio(
            "[emotion:excited][media_summary: usuario pide canción] ¡Vamos!"
        )
        sent = [json.loads(c[0][0]) for c in ws.send_text.call_args_list]
        emotions = [m for m in sent if m["type"] == "emotion"]
        assert len(emotions) == 1
        assert emotions[0]["emotion"] == "excited"

    async def test_stream_end_still_sent_for_audio(self):
        """Con audio, stream_end se envía al final."""
        ws, _ = await self._run_process_audio(
            "[emotion:neutral][media_summary: audio de prueba] Respuesta."
        )
        sent = [json.loads(c[0][0]) for c in ws.send_text.call_args_list]
        ends = [m for m in sent if m["type"] == "stream_end"]
        assert len(ends) == 1


# ═══════════════════════════════════════════════════════════════════════════════
# SECCIÓN 4c — Emojis contextuales y acciones físicas
# ═══════════════════════════════════════════════════════════════════════════════


class TestContextualEmojisAndActions:
    """Tests para la extracción de [emojis:...] y [actions:...] del stream."""

    async def _run(self, *chunks: str) -> MagicMock:
        ws = make_mock_ws()
        history_service = ConversationHistory()
        with (
            patch("ws_handlers.streaming.run_agent_stream", make_async_gen(*chunks)),
            patch("ws_handlers.streaming._save_history_bg", new_callable=AsyncMock),
            patch(
                "ws_handlers.streaming._load_robi_context",
                new_callable=AsyncMock,
                return_value={},
            ),
            patch(
                "ws_handlers.streaming.compact_memories_async", new_callable=AsyncMock
            ),
        ):
            await _process_interaction(
                websocket=ws,
                person_id=None,
                request_id="req-ctx",
                user_input="Test question",
                input_type="text",
                history_service=history_service,
                session_id="sess-test",
                agent=None,
            )
            await asyncio.sleep(0)
        return ws

    async def test_contextual_emojis_in_response_meta(self):
        """[emojis:1F1EB-1F1F7,2708] → response_meta contiene esos códigos."""
        ws = await self._run(
            "[emotion:excited][emojis:1F1EB-1F1F7,2708] Francia tiene la Torre Eiffel."
        )
        sent = [json.loads(c[0][0]) for c in ws.send_text.call_args_list]
        meta = next(m for m in sent if m["type"] == "response_meta")
        assert "1F1EB-1F1F7" in meta["expression"]["emojis"]
        assert "2708" in meta["expression"]["emojis"]

    async def test_contextual_emojis_tag_not_in_text_chunks(self):
        """[emojis:...] NO debe aparecer en los text_chunks enviados al cliente."""
        ws = await self._run("[emotion:happy][emojis:1F600,1F525] Respuesta normal.")
        sent = [json.loads(c[0][0]) for c in ws.send_text.call_args_list]
        chunks_text = "".join(m["text"] for m in sent if m["type"] == "text_chunk")
        assert "[emojis:" not in chunks_text
        assert "Respuesta normal." in chunks_text

    async def test_actions_in_response_meta(self):
        """[actions:wave:800|nod:300] → response_meta.actions contiene la secuencia."""
        ws = await self._run(
            "[emotion:greeting][emojis:1F44B][actions:wave:800|nod:300] ¡Hola!"
        )
        sent = [json.loads(c[0][0]) for c in ws.send_text.call_args_list]
        meta = next(m for m in sent if m["type"] == "response_meta")
        assert len(meta["actions"]) == 1
        seq = meta["actions"][0]
        assert seq["step_count"] >= 1
        assert seq["total_duration_ms"] >= 800

    async def test_actions_tag_not_in_text_chunks(self):
        """[actions:...] NO debe aparecer en los text_chunks."""
        ws = await self._run(
            "[emotion:greeting][emojis:1F44B][actions:wave:800] ¡Hola!"
        )
        sent = [json.loads(c[0][0]) for c in ws.send_text.call_args_list]
        chunks_text = "".join(m["text"] for m in sent if m["type"] == "text_chunk")
        assert "[actions:" not in chunks_text
        assert "¡Hola!" in chunks_text

    async def test_no_contextual_emojis_falls_back_to_emotion(self):
        """Sin [emojis:...] el response_meta usa los emojis de emoción."""
        ws = await self._run("[emotion:happy] Respuesta sin emojis contextuales.")
        sent = [json.loads(c[0][0]) for c in ws.send_text.call_args_list]
        meta = next(m for m in sent if m["type"] == "response_meta")
        assert "1F600" in meta["expression"]["emojis"]

    async def test_no_actions_gives_empty_list(self):
        """Sin [actions:...] response_meta.actions es lista vacía."""
        ws = await self._run("[emotion:neutral] Sin acciones.")
        sent = [json.loads(c[0][0]) for c in ws.send_text.call_args_list]
        meta = next(m for m in sent if m["type"] == "response_meta")
        assert meta["actions"] == []

    async def test_emojis_and_actions_split_across_chunks(self):
        """Tags partidos en múltiples chunks → se extraen correctamente."""
        ws = await self._run(
            "[emotion:happy]",
            "[emojis:1F1FA-1F1F8",
            ",2708]",
            "[actions:wave:500]",
            " Texto final.",
        )
        sent = [json.loads(c[0][0]) for c in ws.send_text.call_args_list]
        meta = next(m for m in sent if m["type"] == "response_meta")
        assert "1F1FA-1F1F8" in meta["expression"]["emojis"]
        assert len(meta["actions"]) == 1
        chunks_text = "".join(m["text"] for m in sent if m["type"] == "text_chunk")
        assert "[emojis:" not in chunks_text
        assert "[actions:" not in chunks_text
        assert "Texto final." in chunks_text

    async def test_emotion_combined_with_contextual_emojis(self):
        """Los emojis finales combinan los contextuales + emojis de emoción."""
        ws = await self._run("[emotion:excited][emojis:2708,1F30D] ¡Vamos a volar!")
        sent = [json.loads(c[0][0]) for c in ws.send_text.call_args_list]
        meta = next(m for m in sent if m["type"] == "response_meta")
        emojis = meta["expression"]["emojis"]
        assert emojis[0] == "2708"
        assert emojis[1] == "1F30D"
        assert "1F929" in emojis


# ═══════════════════════════════════════════════════════════════════════════════
# SECCIÓN 5 — ws_interact (flujo completo)
# ═══════════════════════════════════════════════════════════════════════════════


class TestWsInteract:
    async def test_auth_failure_returns_early(self):
        """Si auth falla, ws_interact regresa sin procesar más mensajes."""
        ws = make_mock_ws(
            receive_text_values=[json.dumps({"type": "auth", "api_key": "bad-key"})],
            receive_messages=[{"type": "websocket.disconnect"}],
        )
        await ws_interact(ws)
        ws.receive.assert_not_called()

    async def test_disconnect_after_auth(self):
        """Cliente desconecta inmediatamente tras auth → no hay error."""
        ws = make_mock_ws(
            receive_text_values=[
                json.dumps(
                    {"type": "auth", "api_key": "test-api-key-for-unit-tests-only"}
                )
            ],
            receive_messages=[{"type": "websocket.disconnect"}],
        )
        await ws_interact(ws)

    async def test_text_message_triggers_processing(self):
        """Mensaje de texto → se procesan text_chunks y stream_end."""
        ws = make_mock_ws(
            receive_text_values=[
                json.dumps(
                    {"type": "auth", "api_key": "test-api-key-for-unit-tests-only"}
                )
            ],
            receive_messages=[
                {
                    "type": "websocket.receive",
                    "text": json.dumps(
                        {
                            "type": "text",
                            "request_id": "req-full",
                            "content": "Hola Robi",
                        }
                    ),
                    "bytes": None,
                },
                {"type": "websocket.disconnect"},
            ],
        )

        with (
            patch(
                "ws_handlers.streaming.run_agent_stream",
                make_async_gen("[emotion:greeting] ¡Hola!"),
            ),
            patch("ws_handlers.streaming.create_agent", return_value=None),
            patch("ws_handlers.streaming._save_history_bg", new_callable=AsyncMock),
            patch(
                "ws_handlers.streaming._load_robi_context",
                new_callable=AsyncMock,
                return_value={},
            ),
            patch(
                "ws_handlers.streaming.compact_memories_async", new_callable=AsyncMock
            ),
        ):
            await ws_interact(ws)

        all_sent = [json.loads(c[0][0]) for c in ws.send_text.call_args_list]
        types = [m["type"] for m in all_sent]
        assert "auth_ok" in types
        assert "emotion" in types
        assert "stream_end" in types

    async def test_invalid_json_message_sends_error(self):
        """Mensaje de texto no-JSON → se envía error y se continúa."""
        ws = make_mock_ws(
            receive_text_values=[
                json.dumps(
                    {"type": "auth", "api_key": "test-api-key-for-unit-tests-only"}
                )
            ],
            receive_messages=[
                {
                    "type": "websocket.receive",
                    "text": "{ not valid json",
                    "bytes": None,
                },
                {"type": "websocket.disconnect"},
            ],
        )

        await ws_interact(ws)

        all_sent = [json.loads(c[0][0]) for c in ws.send_text.call_args_list]
        errors = [m for m in all_sent if m["type"] == "error"]
        assert len(errors) >= 1

    async def test_binary_audio_accumulates_without_processing(self):
        """Frames binarios se acumulan sin procesar hasta audio_end."""
        ws = make_mock_ws(
            receive_text_values=[
                json.dumps(
                    {"type": "auth", "api_key": "test-api-key-for-unit-tests-only"}
                )
            ],
            receive_messages=[
                {
                    "type": "websocket.receive",
                    "text": json.dumps(
                        {
                            "type": "interaction_start",
                            "request_id": "r1",
                            "person_id": None,
                        }
                    ),
                    "bytes": None,
                },
                {"type": "websocket.receive", "text": None, "bytes": b"\x00\x01\x02"},
                {"type": "websocket.disconnect"},
            ],
        )

        with (
            patch("ws_handlers.streaming.run_agent_stream", make_async_gen()),
            patch("ws_handlers.streaming.create_agent", return_value=None),
        ):
            await ws_interact(ws)

        all_sent = [json.loads(c[0][0]) for c in ws.send_text.call_args_list]
        types = [m["type"] for m in all_sent]
        assert "auth_ok" in types
        assert "stream_end" not in types

    async def test_explore_mode_sends_exploration_actions(self):
        """Mensaje explore_mode → se envía exploration_actions."""
        ws = make_mock_ws(
            receive_text_values=[
                json.dumps(
                    {"type": "auth", "api_key": "test-api-key-for-unit-tests-only"}
                )
            ],
            receive_messages=[
                {
                    "type": "websocket.receive",
                    "text": json.dumps(
                        {
                            "type": "explore_mode",
                            "request_id": "req-explore",
                            "duration_minutes": 5,
                        }
                    ),
                    "bytes": None,
                },
                {"type": "websocket.disconnect"},
            ],
        )

        with (
            patch(
                "ws_handlers.streaming.run_agent_stream",
                make_async_gen("[emotion:curious] ¡Voy a explorar!"),
            ),
            patch("ws_handlers.streaming.create_agent", return_value=None),
            patch("ws_handlers.streaming._save_history_bg", new_callable=AsyncMock),
            patch(
                "ws_handlers.streaming._load_robi_context",
                new_callable=AsyncMock,
                return_value={},
            ),
            patch(
                "ws_handlers.streaming.compact_memories_async", new_callable=AsyncMock
            ),
        ):
            await ws_interact(ws)

        all_sent = [json.loads(c[0][0]) for c in ws.send_text.call_args_list]
        types = [m["type"] for m in all_sent]
        assert "exploration_actions" in types

    async def test_face_scan_mode_sends_face_scan_actions(self):
        """Mensaje face_scan_mode → se envía face_scan_actions."""
        ws = make_mock_ws(
            receive_text_values=[
                json.dumps(
                    {"type": "auth", "api_key": "test-api-key-for-unit-tests-only"}
                )
            ],
            receive_messages=[
                {
                    "type": "websocket.receive",
                    "text": json.dumps(
                        {
                            "type": "face_scan_mode",
                            "request_id": "req-face",
                        }
                    ),
                    "bytes": None,
                },
                {"type": "websocket.disconnect"},
            ],
        )

        await ws_interact(ws)

        all_sent = [json.loads(c[0][0]) for c in ws.send_text.call_args_list]
        types = [m["type"] for m in all_sent]
        assert "face_scan_actions" in types
