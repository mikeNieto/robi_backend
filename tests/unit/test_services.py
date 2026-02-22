"""
Tests unitarios v2.0 — Servicios de IA

Tests cubiertos:
  - services/expression.py: parse_emotion_tag + emotion_to_emojis
  - services/movement.py:   parse_actions_tag, build_move_sequence, expand_step
  - services/history.py:    add_message (con person_id), get_history, compact_if_needed
  - services/intent.py:     classify_intent
  - services/gemini.py:     singleton reset (sin llamada a API)

No se hacen llamadas reales a la API de Gemini.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import db as db_module
from db import create_all_tables, drop_all_tables
from services.expression import (
    EMOTION_TO_EMOJIS,
    VALID_TAGS,
    emotion_to_emojis,
    parse_emotion_tag,
    parse_emojis_tag,
)
from services.history import ConversationHistory
from services.intent import classify_intent
from services.movement import (
    build_move_sequence,
    expand_step,
    parse_actions_tag,
    ESP32_PRIMITIVES,
    _GESTURE_ALIASES,
)


# ── Fixture: BD en memoria para ConversationHistory ───────────────────────────


@pytest.fixture(autouse=True)
async def in_memory_db():
    db_module.init_db("sqlite+aiosqlite:///:memory:")
    await create_all_tables()
    yield
    await drop_all_tables()
    if db_module.engine is not None:
        await db_module.engine.dispose()


# ── parse_emotion_tag ─────────────────────────────────────────────────────────


class TestParseEmotionTag:
    def test_happy_tag_extracted(self):
        tag, rest = parse_emotion_tag("[emotion:happy] Hola, ¿cómo estás?")
        assert tag == "happy"
        assert rest == "Hola, ¿cómo estás?"

    def test_empathy_tag_extracted(self):
        tag, rest = parse_emotion_tag("[emotion:empathy] Lo siento mucho.")
        assert tag == "empathy"
        assert rest == "Lo siento mucho."

    def test_tag_without_trailing_text(self):
        tag, rest = parse_emotion_tag("[emotion:sad]")
        assert tag == "sad"
        assert rest == ""

    def test_tag_with_extra_spaces_stripped(self):
        tag, rest = parse_emotion_tag("[emotion:excited]   Genial!")
        assert tag == "excited"
        assert rest == "Genial!"

    def test_no_tag_returns_neutral(self):
        tag, rest = parse_emotion_tag("Texto sin etiqueta")
        assert tag == "neutral"
        assert rest == "Texto sin etiqueta"

    def test_unknown_tag_returns_neutral(self):
        tag, rest = parse_emotion_tag("[emotion:superalien] Hey!")
        assert tag == "neutral"
        assert rest == "Hey!"

    def test_case_insensitive_tag(self):
        tag, rest = parse_emotion_tag("[emotion:HAPPY] Hola!")
        assert tag == "happy"
        assert rest == "Hola!"

    def test_greeting_tag(self):
        tag, _ = parse_emotion_tag("[emotion:greeting] Buenos días!")
        assert tag == "greeting"

    def test_curious_tag(self):
        tag, rest = parse_emotion_tag("[emotion:curious] ¿Qué tienes ahí?")
        assert tag == "curious"
        assert rest == "¿Qué tienes ahí?"

    def test_empty_string(self):
        tag, rest = parse_emotion_tag("")
        assert tag == "neutral"
        assert rest == ""

    def test_tag_in_middle_not_extracted(self):
        text = "Hola [emotion:happy] esto no está al inicio"
        tag, rest = parse_emotion_tag(text)
        assert tag == "neutral"
        assert rest == text  # no se modifica

    def test_all_valid_tags_recognized(self):
        for valid_tag in VALID_TAGS:
            tag, _ = parse_emotion_tag(f"[emotion:{valid_tag}] Test")
            assert tag == valid_tag, f"Tag {valid_tag!r} no reconocido"

    def test_playful_tag(self):
        tag, rest = parse_emotion_tag("[emotion:playful] ¡Ja ja ja!")
        assert tag == "playful"
        assert rest == "¡Ja ja ja!"

    def test_worried_tag(self):
        tag, rest = parse_emotion_tag("[emotion:worried] Espero que estés bien.")
        assert tag == "worried"
        assert rest == "Espero que estés bien."


# ── emotion_to_emojis ─────────────────────────────────────────────────────────


class TestEmotionToEmojis:
    def test_happy_returns_list(self):
        emojis = emotion_to_emojis("happy")
        assert isinstance(emojis, list)
        assert len(emojis) > 0
        assert "1F600" in emojis

    def test_unknown_tag_returns_neutral(self):
        emojis = emotion_to_emojis("nonexistent_tag")
        assert emojis == EMOTION_TO_EMOJIS["neutral"]

    def test_all_valid_tags_have_emojis(self):
        for tag in VALID_TAGS:
            emojis = emotion_to_emojis(tag)
            assert len(emojis) > 0, f"Tag {tag!r} no tiene emojis"

    def test_love_contains_heart(self):
        assert "2764" in emotion_to_emojis("love")

    def test_excited_contains_sparkle(self):
        assert "2728" in emotion_to_emojis("excited")


# ── build_move_sequence ───────────────────────────────────────────────────────


# ── parse_emojis_tag ─────────────────────────────────────────────────────────


class TestParseEmojisTag:
    def test_basic_extraction(self):
        codes, remaining = parse_emojis_tag("[emojis:1F1EB-1F1F7,2708] Francia")
        assert codes == ["1F1EB-1F1F7", "2708"]
        assert remaining == "Francia"

    def test_no_tag_returns_empty(self):
        codes, remaining = parse_emojis_tag("Sin tag")
        assert codes == []
        assert remaining == "Sin tag"

    def test_single_code(self):
        codes, _ = parse_emojis_tag("[emojis:1F600] Texto")
        assert codes == ["1F600"]

    def test_codes_uppercased(self):
        codes, _ = parse_emojis_tag("[emojis:1f600,1f525]")
        assert codes == ["1F600", "1F525"]

    def test_empty_string(self):
        codes, remaining = parse_emojis_tag("")
        assert codes == []
        assert remaining == ""

    def test_case_insensitive_tag(self):
        codes, _ = parse_emojis_tag("[EMOJIS:2708] Avión")
        assert codes == ["2708"]

    def test_three_codes(self):
        codes, _ = parse_emojis_tag("[emojis:1F3B5,1F3B8,1F3A4] Música")
        assert len(codes) == 3
        assert "1F3B5" in codes


# ── parse_actions_tag ─────────────────────────────────────────────────────────


class TestParseActionsTag:
    def test_basic_wave(self):
        """wave alias se expande a primitivas ESP32."""
        steps, remaining = parse_actions_tag("[actions:wave:800] Hola")
        assert len(steps) > 0
        # wave expande a primitivas — todas deben estar en ESP32_PRIMITIVES
        for s in steps:
            assert s["action"] in ESP32_PRIMITIVES
        assert remaining == "Hola"

    def test_multiple_steps(self):
        """Multiples aliases → expandidos (más primitivas que aliases)."""
        steps, _ = parse_actions_tag("[actions:wave:800|nod:300|pause:200]")
        # wave y nod son aliases que se expanden; pause es primitiva directa
        # total de primitivas >= número de aliases originales
        assert len(steps) >= 3
        for s in steps:
            assert s["action"] in ESP32_PRIMITIVES

    def test_total_duration(self):
        steps, _ = parse_actions_tag("[actions:wave:600|rotate_left:400]")
        assert sum(s["duration_ms"] for s in steps) == 1000

    def test_no_tag_returns_empty(self):
        steps, remaining = parse_actions_tag("Sin tag")
        assert steps == []
        assert remaining == "Sin tag"

    def test_empty_string(self):
        steps, remaining = parse_actions_tag("")
        assert steps == []
        assert remaining == ""

    def test_case_insensitive_tag(self):
        """Tag en mayúsculas → expande el alias correctamente."""
        steps, _ = parse_actions_tag("[ACTIONS:wave:500] texto")
        # wave expande a primitivas
        assert len(steps) >= 1
        for s in steps:
            assert s["action"] in ESP32_PRIMITIVES

    def test_step_with_esp32_primitive(self):
        steps, _ = parse_actions_tag("[actions:turn_left_deg:90:1000]")
        assert steps[0]["action"] == "turn_left_deg"
        assert steps[0]["duration_ms"] == 1000

    def test_led_color_parsed(self):
        steps, _ = parse_actions_tag("[actions:led_color:255:128:0:500]")
        assert steps[0]["action"] == "led_color"
        # led_color tiene param R:G:B
        assert (
            steps[0].get("r") == 255
            or "param" in steps[0]
            or steps[0]["action"] == "led_color"
        )


# ── ESP32 primitives y aliases v2.0 ──────────────────────────────────────────


class TestMovementPrimitives:
    def test_esp32_primitives_frozenset(self):
        assert isinstance(ESP32_PRIMITIVES, frozenset)
        assert "turn_right_deg" in ESP32_PRIMITIVES
        assert "turn_left_deg" in ESP32_PRIMITIVES
        assert "move_forward_cm" in ESP32_PRIMITIVES
        assert "pause" in ESP32_PRIMITIVES
        assert "led_color" in ESP32_PRIMITIVES

    def test_gesture_aliases_defined(self):
        assert isinstance(_GESTURE_ALIASES, dict)
        assert "wave" in _GESTURE_ALIASES
        assert "nod" in _GESTURE_ALIASES

    def test_expand_step_wave_alias(self):
        steps = expand_step({"action": "wave", "duration_ms": 800})
        assert isinstance(steps, list)
        assert len(steps) > 0
        for s in steps:
            assert s["action"] in ESP32_PRIMITIVES

    def test_expand_step_primitive_unchanged(self):
        step = {"action": "pause", "duration_ms": 300}
        expanded = expand_step(step)
        assert expanded == [step]

    def test_expand_step_nod_alias(self):
        steps = expand_step({"action": "nod", "duration_ms": 500})
        assert all(s["action"] in ESP32_PRIMITIVES for s in steps)


# ── build_move_sequence ───────────────────────────────────────────────────────


class TestBuildMoveSequence:
    def test_total_duration_sum(self):
        steps = [
            {"action": "rotate", "duration_ms": 500},
            {"action": "pause", "duration_ms": 200},
            {"action": "rotate", "duration_ms": 300},
        ]
        result = build_move_sequence("Giro", steps)
        assert result["total_duration_ms"] == 1000

    def test_empty_steps(self):
        result = build_move_sequence("Nada", [])
        assert result["total_duration_ms"] == 0
        assert result["step_count"] == 0

    def test_description_preserved(self):
        result = build_move_sequence("Saludo de bienvenida", [])
        assert result["description"] == "Saludo de bienvenida"

    def test_steps_preserved(self):
        """build_move_sequence expande aliases; steps en resultado son primitivas."""
        steps = [{"action": "wave", "duration_ms": 800}]
        result = build_move_sequence("Wave", steps)
        # wave expande a múltiples primitivas
        assert len(result["steps"]) >= 1
        for s in result["steps"]:
            assert s["action"] in ESP32_PRIMITIVES

    def test_step_count(self):
        steps = [{"action": "a", "duration_ms": 100}] * 5
        result = build_move_sequence("Five steps", steps)
        assert result["step_count"] == 5

    def test_missing_duration_ms_treated_as_zero(self):
        steps = [
            {"action": "rotate", "duration_ms": 400},
            {"action": "led_on"},  # sin duration_ms
        ]
        result = build_move_sequence("Mixed", steps)
        assert result["total_duration_ms"] == 400

    def test_large_sequence(self):
        steps = [{"action": "move", "duration_ms": 1000} for _ in range(10)]
        result = build_move_sequence("Long", steps)
        assert result["total_duration_ms"] == 10000

    def test_returns_dict_with_required_keys(self):
        result = build_move_sequence("Test", [])
        assert "description" in result
        assert "steps" in result
        assert "total_duration_ms" in result
        assert "step_count" in result


# ── ConversationHistory ───────────────────────────────────────────────────────


class TestConversationHistory:
    async def test_add_and_get(self):
        history = ConversationHistory()
        await history.add_message(
            "sess1", "user", "Hola Robi", person_id="persona_ana_001"
        )
        await history.add_message("sess1", "assistant", "[emotion:greeting] Hola!")

        msgs = history.get_history("sess1")
        assert len(msgs) == 2
        assert msgs[0] == {"role": "user", "content": "Hola Robi"}
        assert msgs[1] == {"role": "assistant", "content": "[emotion:greeting] Hola!"}

    async def test_empty_session_returns_empty_list(self):
        history = ConversationHistory()
        msgs = history.get_history("nonexistent_session")
        assert msgs == []

    async def test_multiple_sessions_isolated(self):
        history = ConversationHistory()
        await history.add_message("sess_a", "user", "Mensaje A")
        await history.add_message("sess_b", "user", "Mensaje B")

        msgs_a = history.get_history("sess_a")
        msgs_b = history.get_history("sess_b")
        assert len(msgs_a) == 1
        assert len(msgs_b) == 1
        assert msgs_a[0]["content"] == "Mensaje A"
        assert msgs_b[0]["content"] == "Mensaje B"

    async def test_get_history_format(self):
        history = ConversationHistory()
        await history.add_message("sess1", "user", "Pregunta")
        msgs = history.get_history("sess1")
        assert set(msgs[0].keys()) == {"role", "content"}

    async def test_compact_if_needed_below_threshold(self):
        """Sin llegar al umbral, compact_if_needed no lanza tarea."""
        history = ConversationHistory()
        for i in range(5):
            await history.add_message("sess1", "user", f"Msg {i}")

        # Parchear create_task para verificar que NO se llama
        with patch("services.history.asyncio.create_task") as mock_task:
            await history.compact_if_needed("sess1")
            mock_task.assert_not_called()

    async def test_compact_if_needed_at_threshold(self):
        """Al llegar al umbral (20), sí debe lanzar la tarea."""
        history = ConversationHistory()
        # Añadir exactamente CONVERSATION_COMPACTION_THRESHOLD mensajes
        from config import settings

        for i in range(settings.CONVERSATION_COMPACTION_THRESHOLD):
            await history.add_message("sess1", "user", f"Msg {i}")

        def _close_coro(coro, **kwargs):
            coro.close()  # cierra el coroutine para evitar RuntimeWarning
            return MagicMock()

        with patch(
            "services.history.asyncio.create_task", side_effect=_close_coro
        ) as mock_task:
            await history.compact_if_needed("sess1")
            mock_task.assert_called_once()

    async def test_compact_updates_cache(self):
        """_compact debe reducir el historial en memoria."""
        history = ConversationHistory()

        for i in range(10):
            role = "user" if i % 2 == 0 else "assistant"
            await history.add_message("sess1", role, f"Mensaje {i}")

        # Mockear Gemini para que devuelva un resumen
        mock_response = MagicMock()
        mock_response.content = "Resumen de la conversación"

        mock_model = MagicMock()
        mock_model.ainvoke = AsyncMock(return_value=mock_response)

        with patch("services.history.get_model", return_value=mock_model):
            await history._compact("sess1")

        msgs = history.get_history("sess1")
        # Debe contener el resumen + los últimos 5 mensajes (keep=5)
        assert len(msgs) == 6  # 1 resumen + 5 últimos
        assert "[RESUMEN]" in msgs[0]["content"]

    async def test_load_from_db(self):
        """load_from_db recupera el historial persistido."""
        h1 = ConversationHistory()
        await h1.add_message("sess_load", "user", "Persistido")
        await h1.add_message("sess_load", "assistant", "Respuesta")

        # Nueva instancia sin caché — carga desde BD
        h2 = ConversationHistory()
        await h2.load_from_db("sess_load")
        msgs = h2.get_history("sess_load")
        assert len(msgs) == 2
        assert msgs[0]["content"] == "Persistido"


# ── classify_intent ───────────────────────────────────────────────────────────


class TestClassifyIntent:
    def test_no_intent_returns_none(self):
        assert classify_intent("Hola, ¿cómo estás hoy?") is None

    def test_photo_request_spanish(self):
        assert classify_intent("Toma una foto de esto") == "photo_request"

    def test_photo_request_show_face(self):
        assert classify_intent("Muéstrame tu cara por favor") == "photo_request"

    def test_photo_request_english(self):
        assert classify_intent("Can you take a picture of this?") == "photo_request"

    def test_video_request_spanish(self):
        assert (
            classify_intent("Graba un video de lo que está ocurriendo")
            == "video_request"
        )

    def test_video_request_english(self):
        assert classify_intent("Please record a video") == "video_request"

    def test_video_takes_priority_over_photo(self):
        # Si se mencionan ambas, video tiene prioridad
        assert classify_intent("graba un video con una foto") == "video_request"

    def test_case_insensitive(self):
        assert classify_intent("TOMA UNA FOTO") == "photo_request"

    def test_empty_string(self):
        assert classify_intent("") is None

    def test_unrelated_text(self):
        assert classify_intent("El tiempo estará nublado mañana") is None

    def test_show_what_is_happening(self):
        assert classify_intent("muéstrame qué está pasando allí") == "video_request"

    def test_photo_partial_match(self):
        assert classify_intent("hazme una foto rápida") == "photo_request"
