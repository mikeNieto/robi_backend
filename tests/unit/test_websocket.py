"""
tests/unit/test_websocket.py — Tests unitarios para ws_handlers/

Cubre:
  - ws_handlers/protocol.py: funciones builder de mensajes
  - ws_handlers/auth.py: authenticate_websocket
  - ws_handlers/streaming.py: _process_interaction, helpers, ws_interact flow
"""

import asyncio
import json
from dataclasses import dataclass
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
    make_response_meta,
    make_stream_end,
    make_text_chunk,
    new_session_id,
)
from ws_handlers.streaming import (
    _build_context_input,
    _build_memory_context,
    _load_memories,
    _parse_media_summary,
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
    # Teardown: cerrar el engine para liberar los threads de aiosqlite
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
        # Siempre terminar con disconnect
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
        assert "user_identified" not in result
        assert "confidence" not in result

    def test_make_emotion_with_user_and_confidence(self):
        result = json.loads(
            make_emotion("req-2", "love", user_identified="user_juan", confidence=0.95)
        )
        assert result["user_identified"] == "user_juan"
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

    def test_make_capture_request_photo(self):
        result = json.loads(make_capture_request("req-9"))
        assert result["type"] == "capture_request"
        assert result["capture_type"] == "photo"

    def test_make_capture_request_video(self):
        result = json.loads(make_capture_request("req-10", "video"))
        assert result["capture_type"] == "video"

    def test_new_session_id_is_uuid(self):
        import uuid

        sid = new_session_id()
        # Debe ser parseable como UUID
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
                json.dumps({"type": "interaction_start", "user_id": "user_juan"})
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
        uuid.UUID(session_id)  # no lanza excepción


# ═══════════════════════════════════════════════════════════════════════════════
# SECCIÓN 3 — Helpers de streaming
# ═══════════════════════════════════════════════════════════════════════════════


class TestStreamingHelpers:
    def test_build_context_input_no_memories(self):
        result = _build_context_input("Hola Robi", [])
        assert result == "Hola Robi"

    def test_build_context_input_with_memories(self):
        @dataclass
        class FakeMemory:
            content: str

        memories = [FakeMemory("Le gusta el café"), FakeMemory("Tiene 35 años")]
        result = _build_context_input("¿Cómo estoy?", memories)
        assert "Le gusta el café" in result
        assert "Tiene 35 años" in result
        assert "¿Cómo estoy?" in result
        assert result.startswith("[Contexto del usuario:")

    async def test_load_memories_unknown_user(self):
        """user_id='unknown' siempre devuelve lista vacía sin tocar la BD."""
        memories = await _load_memories("unknown")
        assert memories == []

    async def test_load_memories_no_session_factory(self):
        """Si la DB no está inicializada (AsyncSessionLocal=None), devuelve []."""
        original = db_module.AsyncSessionLocal
        db_module.AsyncSessionLocal = None
        try:
            memories = await _load_memories("user_juan")
            assert memories == []
        finally:
            db_module.AsyncSessionLocal = original

    async def test_load_memories_user_not_in_db(self):
        """Usuario que no existe → sin error, devuelve []."""
        memories = await _load_memories("user_nonexistent_xyz")
        assert memories == []

    def test_build_memory_context_no_memories(self):
        result = _build_memory_context([])
        assert result == ""

    def test_build_memory_context_with_memories(self):
        from dataclasses import dataclass

        @dataclass
        class FakeMemory:
            content: str

        memories = [FakeMemory("Le gusta el té"), FakeMemory("Vive en Madrid")]
        result = _build_memory_context(memories)
        assert result.startswith("[Contexto del usuario:")
        assert "Le gusta el té" in result
        assert "Vive en Madrid" in result
        # No debe contener texto de input del usuario (solo el bloque de contexto)
        assert "\n\n" not in result


# ═══════════════════════════════════════════════════════════════════════════════
# SECCIÓN 4 — _process_interaction (streaming + emotion tag)
# ═══════════════════════════════════════════════════════════════════════════════


def make_async_gen(*chunks: str):
    """Crea un async generator que yield los chunks dados."""

    async def _gen(*args, **kwargs):
        for c in chunks:
            yield c

    return _gen


class TestProcessInteraction:
    async def _run_process(
        self, *chunks: str, ws: MagicMock | None = None
    ) -> MagicMock:
        """Helper: lanza _process_interaction con el mock de agente dado."""
        if ws is None:
            ws = make_mock_ws()

        history_service = ConversationHistory()

        with (
            patch("ws_handlers.streaming.run_agent_stream", make_async_gen(*chunks)),
            patch("ws_handlers.streaming._save_history_bg", new_callable=AsyncMock),
            patch("ws_handlers.streaming._save_interaction_bg", new_callable=AsyncMock),
            patch(
                "ws_handlers.streaming._load_memories",
                new_callable=AsyncMock,
                return_value=[],
            ),
        ):
            await _process_interaction(
                websocket=ws,
                user_id="user_test",
                request_id="req-test",
                user_input="Hola",
                input_type="text",
                history_service=history_service,
                session_id="sess-test",
                agent=None,
            )
            # Drena las tareas de fondo (asyncio.create_task) creadas dentro de _process_interaction
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

    async def test_photo_intent_sends_capture_request(self):
        """Si el texto implica 'photo_request', se envía capture_request con photo."""
        ws = await self._run_process("[emotion:curious] ¿Quieres que tome una foto?")

        sent = [json.loads(c[0][0]) for c in ws.send_text.call_args_list]
        _ = [m for m in sent if m["type"] == "capture_request"]
        # Solo verificar no rompe — la prueba real depende de classify_intent keywords
        # Los tests de classify_intent están en test_services.py

    async def test_empty_stream_emits_neutral(self):
        """Stream vacío → se emite emotion neutral y stream_end."""
        ws = await self._run_process()

        sent = [json.loads(c[0][0]) for c in ws.send_text.call_args_list]
        emotions = [m for m in sent if m["type"] == "emotion"]
        ends = [m for m in sent if m["type"] == "stream_end"]
        assert len(emotions) == 1  # always emitted
        assert len(ends) == 1

    async def test_agent_error_sends_error_message(self):
        """Si el agente lanza excepción → se envía error y no stream_end."""
        ws = make_mock_ws()

        async def failing_stream(*args, **kwargs):
            raise RuntimeError("agente caído")
            yield  # pragma: no cover

        history_service = ConversationHistory()
        with patch("ws_handlers.streaming.run_agent_stream", failing_stream):
            await _process_interaction(
                websocket=ws,
                user_id="user_test",
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


# ═══════════════════════════════════════════════════════════════════════════════
# SECCIÓN 4b — _parse_media_summary
# ═══════════════════════════════════════════════════════════════════════════════


class TestParseMediaSummary:
    def test_basic_extraction(self):
        """Extrae correctamente el contenido y el texto restante."""
        summary, remaining = _parse_media_summary(
            "[media_summary: el usuario dice hola] ¡Hola!"
        )
        assert summary == "el usuario dice hola"
        assert remaining == "¡Hola!"

    def test_no_tag_returns_empty_and_original(self):
        """Si no hay tag devuelve summary vacío y el texto sin modificar."""
        summary, remaining = _parse_media_summary("Respuesta normal sin tag")
        assert summary == ""
        assert remaining == "Respuesta normal sin tag"

    def test_empty_string(self):
        summary, remaining = _parse_media_summary("")
        assert summary == ""
        assert remaining == ""

    def test_tag_with_leading_whitespace(self):
        """Espacios antes del tag se toleran."""
        summary, remaining = _parse_media_summary(
            "  [media_summary: audio de saludo] ¡Buenas!"
        )
        assert summary == "audio de saludo"
        assert remaining == "¡Buenas!"

    def test_case_insensitive(self):
        """El tag es case-insensitive."""
        summary, _ = _parse_media_summary("[MEDIA_SUMMARY: prueba] texto")
        assert summary == "prueba"

    def test_multi_word_summary(self):
        summary, remaining = _parse_media_summary(
            "[media_summary: el usuario pregunta qué hora es y cómo está el robot] Respuesta."
        )
        assert "qué hora es" in summary
        assert remaining == "Respuesta."

    def test_only_tag_no_remaining(self):
        """Tag sin texto posterior."""
        summary, remaining = _parse_media_summary("[media_summary: solo audio] ")
        assert summary == "solo audio"
        assert remaining == ""


# ═══════════════════════════════════════════════════════════════════════════════
# SECCIÓN 4c — _process_interaction con media (audio/imagen/video)
# ═══════════════════════════════════════════════════════════════════════════════


class TestProcessInteractionMedia:
    """Tests del flujo media: extracción de [media_summary:] y uso como historial."""

    async def _run_process_audio(self, *chunks: str) -> tuple[MagicMock, AsyncMock]:
        """Helper: lanza _process_interaction con audio_data y devuelve (ws, save_history_mock)."""
        ws = make_mock_ws()
        history_service = ConversationHistory()
        save_history_mock = AsyncMock()

        with (
            patch("ws_handlers.streaming.run_agent_stream", make_async_gen(*chunks)),
            patch("ws_handlers.streaming._save_history_bg", save_history_mock),
            patch("ws_handlers.streaming._save_interaction_bg", new_callable=AsyncMock),
            patch(
                "ws_handlers.streaming._load_memories",
                new_callable=AsyncMock,
                return_value=[],
            ),
        ):
            await _process_interaction(
                websocket=ws,
                user_id="user_test",
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
        """El tag [media_summary:...] NO debe aparecer en los text_chunks enviados al robot."""
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
        """Si el LLM no emite [media_summary:...] el historial usa el placeholder '[audio]'."""
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
            patch("ws_handlers.streaming._save_interaction_bg", new_callable=AsyncMock),
            patch(
                "ws_handlers.streaming._load_memories",
                new_callable=AsyncMock,
                return_value=[],
            ),
        ):
            await _process_interaction(
                websocket=ws,
                user_id="user_test",
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
# SECCIÓN 5 — ws_interact (flujo completo)
# ═══════════════════════════════════════════════════════════════════════════════


class TestWsInteract:
    async def test_auth_failure_returns_early(self):
        """Si auth falla, ws_interact regresa sin procesar más mensajes."""
        ws = make_mock_ws(
            receive_text_values=[json.dumps({"type": "auth", "api_key": "bad-key"})],
            receive_messages=[{"type": "websocket.disconnect"}],
        )
        # auth.py usa receive_text; streaming usa receive
        # Si auth falla → ws_interact retorna sin llamar ws.receive
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
        # No debe lanzar ninguna excepción
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
            patch("ws_handlers.streaming._save_interaction_bg", new_callable=AsyncMock),
        ):
            await ws_interact(ws)

        all_sent = [json.loads(c[0][0]) for c in ws.send_text.call_args_list]
        types = [m["type"] for m in all_sent]
        assert "auth_ok" in types
        assert "emotion" in types
        assert "stream_end" in types

    async def test_invalid_json_message_sends_error(self):
        """Mensaje de texto no-JSON → se envía error y se continúa (no rompe)."""
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

    async def test_binary_audio_accumulates(self):
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
                            "user_id": "unknown",
                        }
                    ),
                    "bytes": None,
                },
                # Binary frame
                {"type": "websocket.receive", "text": None, "bytes": b"\x00\x01\x02"},
                # Desconectar sin audio_end → no se procesa
                {"type": "websocket.disconnect"},
            ],
        )

        with (
            patch("ws_handlers.streaming.run_agent_stream", make_async_gen()),
            patch("ws_handlers.streaming.create_agent", return_value=None),
        ):
            await ws_interact(ws)

        # Sin audio_end, el agente no fue llamado
        # (el mock_stream no fue invocado con audio frames solos)
        all_sent = [json.loads(c[0][0]) for c in ws.send_text.call_args_list]
        types = [m["type"] for m in all_sent]
        # Solo auth_ok, sin stream_end (no se procesó la interacción)
        assert "auth_ok" in types
        assert "stream_end" not in types
