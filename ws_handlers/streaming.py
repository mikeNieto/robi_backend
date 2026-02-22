"""
ws_handlers/streaming.py — Handler WebSocket principal para /ws/interact.

v2.0 — Robi Amigo Familiar

Implementa el flujo completo:
  1. Acepta la conexión y autentica vía API Key.
  2. Bucle de mensajes: gestiona todos los tipos de mensaje del protocolo v2.0.
  3. Nuevos tipos: explore_mode, face_scan_mode, zone_update, person_detected.
  4. Parsea tags del LLM: [emotion:], [emojis:], [actions:], [memory:],
     [person_name:], [zone_learn:], [media_summary:].
  5. Envía emotion + text_chunks progresivamente + response_meta + stream_end.
  6. Background: historial + compactación de memorias.

Uso (registrado en main.py):
    from ws_handlers.streaming import ws_interact

    @app.websocket("/ws/interact")
    async def websocket_interact(websocket: WebSocket):
        await ws_interact(websocket)
"""

import asyncio
import base64
import json
import logging
import re
import time

import db as db_module
from fastapi import WebSocket
from starlette.websockets import WebSocketState

from repositories.memory import MemoryRepository
from repositories.people import PeopleRepository
from repositories.zones import ZonesRepository
from services.agent import create_agent, run_agent_stream
from services.expression import emotion_to_emojis, parse_emotion_tag, parse_emojis_tag
from services.memory_compaction import compact_memories_async
from services.movement import build_move_sequence, parse_actions_tag
from services.history import ConversationHistory
from services.intent import classify_intent
from ws_handlers.auth import authenticate_websocket
from ws_handlers.protocol import (
    make_capture_request,
    make_emotion,
    make_error,
    make_exploration_actions,
    make_face_scan_actions,
    make_response_meta,
    make_stream_end,
    make_text_chunk,
    new_session_id,
)

logger = logging.getLogger(__name__)

# Tamaño máximo del buffer de cabecera para capturar todos los tags de control:
# [emotion:TAG][emojis:CODE,...][actions:step|...]
_MAX_HEADER_BUFFER = 500

# Tags al final de la respuesta del LLM (se extraen y se eliminan del texto visible)
_MEDIA_SUMMARY_RE = re.compile(
    r"\[media_summary:\s*.*?\]",
    re.DOTALL | re.IGNORECASE,
)
_MEDIA_SUMMARY_OPEN = "[media_summary:"

# [memory:TIPO:contenido]  — LLM decide guardar un recuerdo
_MEMORY_TAG_RE = re.compile(
    r"\[memory:([a-z_]+):([^\]]+)\]",
    re.IGNORECASE,
)

# [person_name:NOMBRE]  — LLM extrae nombre del audio de presentación
_PERSON_NAME_TAG_RE = re.compile(
    r"\[person_name:([^\]]+)\]",
    re.IGNORECASE,
)

# [zone_learn:NOMBRE:CATEGORIA:descripción]  — LLM registra/actualiza zona
_ZONE_LEARN_TAG_RE = re.compile(
    r"\[zone_learn:([^:]+):([^:]+):([^\]]+)\]",
    re.IGNORECASE,
)

# Secuencia de giro ESP32 para face_scan_mode
_FACE_SCAN_SEQUENCE: list[dict] = [
    {"action": "turn_right_deg", "degrees": 45, "duration_ms": 500},
    {"action": "pause", "duration_ms": 300},
    {"action": "turn_left_deg", "degrees": 90, "duration_ms": 800},
    {"action": "pause", "duration_ms": 300},
    {"action": "turn_right_deg", "degrees": 45, "duration_ms": 500},
]


# ── Entry point ───────────────────────────────────────────────────────────────


async def ws_interact(websocket: WebSocket) -> None:
    """
    Handler principal para el endpoint WebSocket /ws/interact.

    Acepta la conexión, autentica, y entra en el bucle de mensajes.
    Gestiona múltiples interacciones por sesión hasta que el cliente desconecta.
    """
    await websocket.accept()

    # Autenticar — retorna session_id o None (ya closed)
    session_id = await authenticate_websocket(websocket)
    if session_id is None:
        return

    # Objetos de sesión (one per WS connection)
    history_service = ConversationHistory()
    agent = create_agent()

    # Estado de la interacción actual
    person_id: str | None = None  # slug de la persona identificada
    current_zone: str | None = None  # zona actual de Robi
    request_id: str = ""
    audio_buffer: bytes = b""
    pending_face_embedding: str | None = None  # base64 embedding pendiente de asociar

    logger.info("ws: sesión iniciada session_id=%s", session_id)

    try:
        while True:
            # Recibir siguiente mensaje (texto JSON o binario)
            data = await websocket.receive()

            msg_type = data.get("type", "")

            # Desconexión limpia del cliente
            if msg_type == "websocket.disconnect":
                logger.info("ws: cliente desconectado session_id=%s", session_id)
                break

            # ── Mensajes binarios (audio frames) ─────────────────────────────
            raw_bytes = data.get("bytes")
            if raw_bytes:
                audio_buffer += raw_bytes
                continue

            # ── Mensajes de texto (JSON) ──────────────────────────────────────
            raw_text = data.get("text")
            if not raw_text:
                continue

            try:
                msg = json.loads(raw_text)
            except (json.JSONDecodeError, ValueError):
                await _send_safe(
                    websocket,
                    make_error("INVALID_MESSAGE", "Mensaje no es JSON válido"),
                )
                continue

            client_type = msg.get("type", "")

            if client_type == "interaction_start":
                person_id = msg.get("person_id") or None
                request_id = msg.get("request_id") or new_session_id()
                audio_buffer = b""  # limpiar buffer de interacción anterior
                pending_face_embedding = msg.get("face_embedding") or None
                logger.debug(
                    "ws: interaction_start person_id=%s request_id=%s has_embedding=%s",
                    person_id,
                    request_id,
                    pending_face_embedding is not None,
                )

            elif client_type == "text":
                user_input = msg.get("content", "")
                if not request_id:
                    request_id = msg.get("request_id") or new_session_id()
                else:
                    request_id = msg.get("request_id") or request_id
                face_emb = msg.get("face_embedding") or pending_face_embedding
                pending_face_embedding = None

                if user_input:
                    await _process_interaction(
                        websocket=websocket,
                        person_id=person_id,
                        request_id=request_id,
                        user_input=user_input,
                        input_type="text",
                        history_service=history_service,
                        session_id=session_id,
                        agent=agent,
                        face_embedding_b64=face_emb,
                        current_zone=current_zone,
                    )

            elif client_type == "audio_end":
                if not request_id:
                    request_id = msg.get("request_id") or new_session_id()
                else:
                    request_id = msg.get("request_id") or request_id
                face_emb = msg.get("face_embedding") or pending_face_embedding
                pending_face_embedding = None

                if audio_buffer:
                    audio_data = audio_buffer
                    audio_buffer = b""
                    await _process_interaction(
                        websocket=websocket,
                        person_id=person_id,
                        request_id=request_id,
                        user_input=None,
                        input_type="audio",
                        audio_data=audio_data,
                        history_service=history_service,
                        session_id=session_id,
                        agent=agent,
                        face_embedding_b64=face_emb,
                        current_zone=current_zone,
                    )
                else:
                    await _send_safe(
                        websocket,
                        make_error(
                            "EMPTY_AUDIO",
                            "No se recibieron datos de audio",
                            request_id=request_id,
                            recoverable=True,
                        ),
                    )

            elif client_type == "image":
                if not request_id:
                    request_id = msg.get("request_id") or new_session_id()
                else:
                    request_id = msg.get("request_id") or request_id
                raw_b64 = msg.get("data", "")
                image_bytes: bytes | None = None
                if raw_b64:
                    try:
                        image_bytes = base64.b64decode(raw_b64)
                    except Exception:
                        image_bytes = None
                inline_text: str | None = msg.get("text") or None
                face_emb = msg.get("face_embedding") or pending_face_embedding
                pending_face_embedding = None
                await _process_interaction(
                    websocket=websocket,
                    person_id=person_id,
                    request_id=request_id,
                    user_input=inline_text,
                    input_type="vision",
                    image_data=image_bytes,
                    history_service=history_service,
                    session_id=session_id,
                    agent=agent,
                    face_embedding_b64=face_emb,
                    current_zone=current_zone,
                )

            elif client_type == "video":
                if not request_id:
                    request_id = msg.get("request_id") or new_session_id()
                else:
                    request_id = msg.get("request_id") or request_id
                raw_b64 = msg.get("data", "")
                video_bytes: bytes | None = None
                if raw_b64:
                    try:
                        video_bytes = base64.b64decode(raw_b64)
                    except Exception:
                        video_bytes = None
                inline_text_v: str | None = msg.get("text") or None
                await _process_interaction(
                    websocket=websocket,
                    person_id=person_id,
                    request_id=request_id,
                    user_input=inline_text_v,
                    input_type="vision",
                    video_data=video_bytes,
                    history_service=history_service,
                    session_id=session_id,
                    agent=agent,
                    current_zone=current_zone,
                )

            elif client_type == "multimodal":
                if not request_id:
                    request_id = msg.get("request_id") or new_session_id()
                else:
                    request_id = msg.get("request_id") or request_id

                def _b64_decode(field: str) -> bytes | None:
                    raw = msg.get(field)
                    if not raw:
                        return None
                    try:
                        return base64.b64decode(raw)
                    except Exception:
                        return None

                mm_text: str | None = msg.get("text") or None
                mm_audio = _b64_decode("audio")
                mm_image = _b64_decode("image")
                mm_video = _b64_decode("video")
                mm_audio_mime: str = msg.get("audio_mime", "audio/webm")
                mm_image_mime: str = msg.get("image_mime", "image/jpeg")
                mm_video_mime: str = msg.get("video_mime", "video/mp4")
                face_emb_mm = msg.get("face_embedding") or pending_face_embedding
                pending_face_embedding = None

                input_type_mm = (
                    "vision"
                    if (mm_image or mm_video)
                    else ("audio" if mm_audio else "text")
                )
                await _process_interaction(
                    websocket=websocket,
                    person_id=person_id,
                    request_id=request_id,
                    user_input=mm_text,
                    input_type=input_type_mm,
                    audio_data=mm_audio,
                    image_data=mm_image,
                    video_data=mm_video,
                    audio_mime_type=mm_audio_mime,
                    image_mime_type=mm_image_mime,
                    video_mime_type=mm_video_mime,
                    history_service=history_service,
                    session_id=session_id,
                    agent=agent,
                    face_embedding_b64=face_emb_mm,
                    current_zone=current_zone,
                )

            # ── Mensajes nuevos v2.0 ───────────────────────────────────────────

            elif client_type == "explore_mode":
                # Android indica que Robi entra en modo exploración autónoma.
                # El agente genera speech + acciones a partir de la zona actual.
                req_id = msg.get("request_id") or new_session_id()
                duration = msg.get("duration_minutes", 5)
                explore_input = (
                    f"Entra en modo exploración autónoma. Tienes {duration} minutos "
                    f"para explorar y descubrir zonas nuevas de la casa. "
                    f"Genera un texto curioso de lo que vas a explorar y sugiere "
                    f"acciones de movimiento en el tag [actions:]."
                )
                context = await _load_robi_context(person_id)
                explore_full = ""
                explore_actions: list[dict] = []
                async for chunk in run_agent_stream(
                    user_input=explore_input,
                    history=[],
                    session_id=session_id,
                    person_id=person_id,
                    agent=agent,
                    memory_context=context,
                    current_zone=current_zone,
                ):
                    explore_full += chunk
                # Extraer acciones del texto generado
                _, remaining_exp = parse_emotion_tag(explore_full)
                _, remaining_exp = parse_emojis_tag(remaining_exp)
                e_steps, remaining_exp = parse_actions_tag(remaining_exp)
                if e_steps:
                    explore_actions = [build_move_sequence("Exploración", e_steps)]
                await _send_safe(
                    websocket,
                    make_exploration_actions(
                        request_id=req_id,
                        actions=explore_actions,
                        exploration_speech=remaining_exp.strip(),
                    ),
                )

            elif client_type == "face_scan_mode":
                # Android inicia escaneo facial activo — Robi gira con secuencia predefinida.
                req_id = msg.get("request_id") or new_session_id()
                scan_seq = [build_move_sequence("Escaneo facial", _FACE_SCAN_SEQUENCE)]
                await _send_safe(
                    websocket,
                    make_face_scan_actions(request_id=req_id, actions=scan_seq),
                )

            elif client_type == "zone_update":
                # Android informa de la zona actual de Robi.
                req_id = msg.get("request_id") or new_session_id()
                zone_name = msg.get("zone_name", "")
                category = msg.get("category", "unknown")
                action = msg.get("action", "enter")  # enter | leave | discover
                if zone_name:
                    if action in ("enter", "discover"):
                        current_zone = zone_name
                    elif action == "leave":
                        current_zone = None
                    asyncio.create_task(
                        _save_zone_bg(
                            zone_name=zone_name, category=category, action=action
                        ),
                        name=f"zone-{req_id}",
                    )
                logger.info(
                    "ws: zone_update zone=%s action=%s session=%s",
                    zone_name,
                    action,
                    session_id,
                )

            elif client_type == "person_detected":
                # Android informa de una persona detectada.
                req_id = msg.get("request_id") or new_session_id()
                known = msg.get("known", False)
                detected_pid = msg.get("person_id") or None
                confidence = msg.get("confidence", 0.0)
                if known and detected_pid:
                    person_id = detected_pid
                    logger.info(
                        "ws: persona conocida detectada person_id=%s conf=%.2f session=%s",
                        person_id,
                        confidence,
                        session_id,
                    )
                else:
                    # Cara desconocida: Robi debe preguntar el nombre
                    context = await _load_robi_context(None)
                    ask_input = (
                        "Acabo de detectar a una persona que no conozco. "
                        "Salúdala con curiosidad y pregúntale su nombre de forma amigable."
                    )
                    await _process_interaction(
                        websocket=websocket,
                        person_id=None,
                        request_id=req_id,
                        user_input=ask_input,
                        input_type="text",
                        history_service=history_service,
                        session_id=session_id,
                        agent=agent,
                        memory_context=context,
                        current_zone=current_zone,
                    )

    except Exception as exc:
        logger.error(
            "ws: error inesperado en sesión %s: %s", session_id, exc, exc_info=True
        )
        if websocket.client_state == WebSocketState.CONNECTED:
            await _send_safe(
                websocket,
                make_error(
                    "INTERNAL_ERROR",
                    "Error interno del servidor",
                    recoverable=False,
                ),
            )
    finally:
        logger.info("ws: sesión cerrada session_id=%s", session_id)


# ── Procesamiento de una interacción ─────────────────────────────────────────


async def _process_interaction(
    *,
    websocket: WebSocket,
    person_id: str | None,
    request_id: str,
    user_input: str | None,
    input_type: str,  # "text" | "audio" | "vision"
    history_service: ConversationHistory,
    session_id: str,
    agent,
    audio_data: bytes | None = None,
    audio_mime_type: str = "audio/webm",
    image_data: bytes | None = None,
    image_mime_type: str = "image/jpeg",
    video_data: bytes | None = None,
    video_mime_type: str = "video/mp4",
    face_embedding_b64: str | None = None,
    memory_context: dict | None = None,
    current_zone: str | None = None,
) -> None:
    """
    Procesa una interacción completa:
      load context → run agent stream → emit emotion + text_chunks
      → extract tags (memory/person_name/zone_learn) → emit meta
    """
    start_time = time.monotonic()

    # 1. Cargar contexto de memorias (si no se pasó ya)
    if memory_context is None:
        memory_context = await _load_robi_context(person_id)

    # 2. Obtener historial de la sesión
    history = history_service.get_history(session_id)

    # 3. Preparar el input para el agente
    #    • Para texto: enriquecer con contexto de memoria en el mismo string.
    #    • Para media (audio/imagen/video): pasar el contexto de memoria separado
    #      como texto y el media como bytes — Gemini lo recibe todo junto.
    has_media = (
        audio_data is not None or image_data is not None or video_data is not None
    )
    has_face_embedding = face_embedding_b64 is not None

    if has_media:
        enriched_input: str | None = None
        if user_input:
            enriched_input = user_input  # texto adicional junto al media
    else:
        enriched_input = user_input or ""

    # 4. Streaming del agente
    full_response = ""
    emotion_tag = "neutral"
    emotion_sent = False
    prefix_buf = ""
    contextual_emojis: list[str] = []
    response_actions: list[dict] = []
    media_summary: str = ""
    pending_buf: str = ""
    extracted_person_name: str | None = None  # de [person_name:NOMBRE]

    try:
        async for chunk in run_agent_stream(
            user_input=enriched_input,
            history=history,
            session_id=session_id,
            person_id=person_id,
            agent=agent,
            audio_data=audio_data,
            audio_mime_type=audio_mime_type,
            image_data=image_data,
            image_mime_type=image_mime_type,
            video_data=video_data,
            video_mime_type=video_mime_type,
            memory_context=memory_context,
            current_zone=current_zone,
            has_face_embedding=has_face_embedding,
        ):
            if not chunk:
                continue

            if not emotion_sent:
                # FASE 1: acumular la cabecera completa de tags de control.
                # El LLM emite: [emotion:TAG][emojis:CODE,...][actions:step|...] texto
                # En media: añade [media_summary: ...] antes del texto de respuesta.
                prefix_buf += chunk
                buffer_full = len(prefix_buf) >= _MAX_HEADER_BUFFER

                # Esperar hasta tener al menos un ] en el buffer
                if "]" not in prefix_buf and not buffer_full:
                    continue

                # ── Parsear emotion tag ───────────────────────────────────────
                emotion_tag, remaining = parse_emotion_tag(prefix_buf)

                # remaining vacío: puede haber más tags en el próximo chunk
                if not remaining and not buffer_full:
                    continue
                # remaining empieza con [ pero sin ] todavía: tag incompleto
                if (
                    remaining.startswith("[")
                    and "]" not in remaining
                    and not buffer_full
                ):
                    continue

                # ── Extraer [emojis:CODE,...] si está al inicio ───────────────
                e_emojis, remaining = parse_emojis_tag(remaining)
                if e_emojis:
                    contextual_emojis = e_emojis

                if not remaining and not buffer_full:
                    continue
                if (
                    remaining.startswith("[")
                    and "]" not in remaining
                    and not buffer_full
                ):
                    continue

                # ── Extraer [actions:step|...] si está al inicio ──────────────
                e_actions, remaining = parse_actions_tag(remaining)
                if e_actions:
                    response_actions = e_actions

                if not remaining and not buffer_full:
                    continue
                if (
                    remaining.startswith("[")
                    and "]" not in remaining
                    and not buffer_full
                ):
                    continue

                # ── Cabecera lista — enviar emotion (§3.4 step 1) ────────────
                await _send_safe(
                    websocket,
                    make_emotion(
                        request_id=request_id,
                        emotion=emotion_tag,
                        person_identified=person_id,
                    ),
                )
                emotion_sent = True

                # El texto restante del prefijo se enruta al pending_buf para ser
                # procesado igual que el resto del stream en FASE 3.
                if remaining:
                    pending_buf += remaining

            else:
                # FASE 3: streaming con detección de [media_summary:...] al final.
                # pending_buf permite manejar el tag aunque llegue fragmentado en chunks.
                pending_buf += chunk
                lower_buf = pending_buf.lower()
                tag_pos = lower_buf.find(_MEDIA_SUMMARY_OPEN)
                if tag_pos >= 0:
                    # Encontrado el inicio del tag — flush todo lo anterior al cliente
                    before = pending_buf[:tag_pos]
                    if before:
                        await _send_safe(websocket, make_text_chunk(request_id, before))
                        full_response += before
                    pending_buf = pending_buf[tag_pos:]
                    # ¿Tenemos ya el cierre ]?
                    close_pos = pending_buf.find("]", len(_MEDIA_SUMMARY_OPEN))
                    if close_pos >= 0:
                        media_summary = pending_buf[
                            len(_MEDIA_SUMMARY_OPEN) : close_pos
                        ].strip()
                        after_tag = pending_buf[close_pos + 1 :].lstrip()
                        pending_buf = ""
                        if after_tag:
                            await _send_safe(
                                websocket, make_text_chunk(request_id, after_tag)
                            )
                            full_response += after_tag
                    # else: tag aún incompleto, seguir acumulando en pending_buf
                else:
                    # Sin inicio de tag — flush todo excepto margen de seguridad
                    margin = len(_MEDIA_SUMMARY_OPEN)
                    safe_len = max(0, len(pending_buf) - margin)
                    if safe_len > 0:
                        to_send = pending_buf[:safe_len]
                        await _send_safe(
                            websocket, make_text_chunk(request_id, to_send)
                        )
                        full_response += to_send
                        pending_buf = pending_buf[safe_len:]

    except Exception as exc:
        logger.error(
            "ws: error en agente session_id=%s request_id=%s: %s",
            session_id,
            request_id,
            exc,
            exc_info=True,
        )
        await _send_safe(
            websocket,
            make_error(
                "AGENT_ERROR",
                "Error procesando la solicitud",
                request_id=request_id,
                recoverable=True,
            ),
        )
        return

    # Si el stream terminó sin emitir la emoción (respuesta vacía), emitir neutral
    if not emotion_sent:
        if prefix_buf:
            emotion_tag, remaining = parse_emotion_tag(prefix_buf)
            full_response += remaining
        await _send_safe(
            websocket,
            make_emotion(
                request_id=request_id,
                emotion=emotion_tag,
                person_identified=person_id,
            ),
        )
        if full_response:
            await _send_safe(websocket, make_text_chunk(request_id, full_response))

    # Flush del pending_buf al finalizar el stream
    if pending_buf:
        match = re.search(
            r"\[media_summary:\s*(.*?)\]", pending_buf, re.DOTALL | re.IGNORECASE
        )
        if match:
            if not media_summary:
                media_summary = match.group(1).strip()
            before_tag = pending_buf[: match.start()].rstrip()
            after_tag = pending_buf[match.end() :].lstrip()
            clean = (
                before_tag + (" " if before_tag and after_tag else "") + after_tag
            ).strip()
        else:
            clean = pending_buf.strip()
        if clean:
            await _send_safe(websocket, make_text_chunk(request_id, clean))
            full_response += clean
        pending_buf = ""

    # ── Extraer tags finales del LLM del full_response ────────────────────────
    # [person_name:NOMBRE]
    pn_match = _PERSON_NAME_TAG_RE.search(full_response)
    if pn_match:
        extracted_person_name = pn_match.group(1).strip()
        full_response = _PERSON_NAME_TAG_RE.sub("", full_response).strip()
        if has_face_embedding and face_embedding_b64 and extracted_person_name:
            asyncio.create_task(
                _save_person_name_bg(
                    name=extracted_person_name,
                    person_id=person_id,
                    face_embedding_b64=face_embedding_b64,
                ),
                name=f"person-{request_id}",
            )

    # [memory:TIPO:contenido]
    for mem_match in _MEMORY_TAG_RE.finditer(full_response):
        mem_type = mem_match.group(1).strip()
        mem_content = mem_match.group(2).strip()
        asyncio.create_task(
            _save_memory_bg(
                memory_type=mem_type,
                content=mem_content,
                person_id=person_id,
            ),
            name=f"memory-{request_id}",
        )
    full_response = _MEMORY_TAG_RE.sub("", full_response).strip()

    # [zone_learn:NOMBRE:CATEGORIA:descripción]
    for zl_match in _ZONE_LEARN_TAG_RE.finditer(full_response):
        zl_name = zl_match.group(1).strip()
        zl_cat = zl_match.group(2).strip()
        zl_desc = zl_match.group(3).strip()
        asyncio.create_task(
            _save_zone_bg(
                zone_name=zl_name,
                category=zl_cat,
                action="discover",
                description=zl_desc,
            ),
            name=f"zone-learn-{request_id}",
        )
    full_response = _ZONE_LEARN_TAG_RE.sub("", full_response).strip()
    intent = classify_intent(full_response)
    if intent == "photo_request":
        await _send_safe(websocket, make_capture_request(request_id, "photo"))
    elif intent == "video_request":
        await _send_safe(websocket, make_capture_request(request_id, "video"))

    # 6. Construir y enviar response_meta
    # Emojis: primero los contextuales del tema (sugeridos por el LLM),
    # luego hasta 2 de emoción por defecto como respaldo visual.
    emotion_emojis = emotion_to_emojis(emotion_tag)
    emojis = (
        (contextual_emojis + emotion_emojis[:2])
        if contextual_emojis
        else emotion_emojis
    )
    # Acciones: envolver los pasos en build_move_sequence si el LLM los sugirió
    actions: list[dict] = []
    if response_actions:
        actions = [
            build_move_sequence("Movimiento sugerido por Robi", response_actions)
        ]
    processing_ms = int((time.monotonic() - start_time) * 1000)

    await _send_safe(
        websocket,
        make_response_meta(
            request_id=request_id,
            response_text=full_response,
            emojis=emojis,
            actions=actions,
            person_name=extracted_person_name,
        ),
    )

    # 7. Enviar stream_end
    await _send_safe(
        websocket,
        make_stream_end(request_id=request_id, processing_time_ms=processing_ms),
    )

    # 8. Background: guardar mensajes en historial + compactar
    #    Para media: usamos el resumen extraído del LLM como turno del usuario
    #    (si el LLM no emitió [media_summary:...] caemos al placeholder genérico).
    if not has_media:
        history_user_msg = user_input or ""
    elif media_summary:
        history_user_msg = media_summary
    else:
        logger.warning(
            "ws: LLM no emitió [media_summary:] para interacción media "
            "session_id=%s request_id=%s — usando placeholder",
            session_id,
            request_id,
        )
        history_user_msg = "[audio]" if audio_data is not None else "[imagen/video]"
    asyncio.create_task(
        _save_history_bg(
            history_service=history_service,
            session_id=session_id,
            user_message=history_user_msg,
            assistant_message=full_response,
            person_id=person_id,
        )
    )

    # 9. Background: compactación de memorias
    asyncio.create_task(
        compact_memories_async(person_id=person_id),
        name=f"compact-{session_id}",
    )


# ── Background tasks ──────────────────────────────────────────────────────────


async def _save_history_bg(
    history_service: ConversationHistory,
    session_id: str,
    user_message: str,
    assistant_message: str,
    person_id: str | None = None,
) -> None:
    """Guarda los mensajes de la interacción en el historial y compacta si es necesario."""
    try:
        await history_service.add_message(
            session_id, "user", user_message, person_id=person_id
        )
        await history_service.add_message(session_id, "assistant", assistant_message)
        await history_service.compact_if_needed(session_id)
    except Exception as exc:
        logger.warning(
            "ws: error guardando historial session_id=%s: %s", session_id, exc
        )


async def _save_memory_bg(
    memory_type: str,
    content: str,
    person_id: str | None = None,
    zone_id: int | None = None,
) -> None:
    """Persiste una memoria extraída del tag [memory:TIPO:contenido] en background."""
    if db_module.AsyncSessionLocal is None:
        return
    try:
        async with db_module.AsyncSessionLocal() as session:
            repo = MemoryRepository(session)
            await repo.save(
                memory_type=memory_type,
                content=content,
                person_id=person_id,
                zone_id=zone_id,
            )
            await session.commit()
    except Exception as exc:
        logger.warning("ws: error guardando memoria type=%s: %s", memory_type, exc)


async def _save_person_name_bg(
    name: str,
    person_id: str | None,
    face_embedding_b64: str,
) -> None:
    """
    Registra (o actualiza) la persona en la BD y guarda su embedding facial.
    Llamado cuando el LLM emite [person_name:NOMBRE] junto a un face_embedding.
    """
    if db_module.AsyncSessionLocal is None:
        return
    try:
        slug = (
            person_id
            or f"persona_{name.lower().replace(' ', '_')[:20]}_{id(name) % 10000:04d}"
        )
        embedding_bytes = base64.b64decode(face_embedding_b64)
        async with db_module.AsyncSessionLocal() as session:
            people_repo = PeopleRepository(session)
            person, created = await people_repo.get_or_create(slug, name)
            if not created and person.name != name:
                await people_repo.update_name(slug, name)
            await people_repo.add_embedding(slug, embedding_bytes)
            await session.commit()
        logger.info("ws: persona registrada person_id=%s name=%s", slug, name)
    except Exception as exc:
        logger.warning("ws: error registrando persona name=%s: %s", name, exc)


async def _save_zone_bg(
    zone_name: str,
    category: str,
    action: str,
    description: str = "",
) -> None:
    """Crea o actualiza una zona del mapa mental en background."""
    if db_module.AsyncSessionLocal is None:
        return
    try:
        async with db_module.AsyncSessionLocal() as session:
            zones_repo = ZonesRepository(session)
            zone, _ = await zones_repo.get_or_create(zone_name, category, description)
            if description and not zone.description and zone.id is not None:
                await zones_repo.update(
                    zone.id, description=description, category=category
                )
            if action == "enter" and zone.id is not None:
                await zones_repo.set_current_zone(zone.id)
            await session.commit()
    except Exception as exc:
        logger.warning("ws: error guardando zona zone=%s: %s", zone_name, exc)


# ── Helpers ───────────────────────────────────────────────────────────────────


async def _load_robi_context(person_id: str | None) -> dict:
    """Carga el contexto de memorias de Robi desde la BD (general + persona + zonas)."""
    if db_module.AsyncSessionLocal is None:
        return {}
    try:
        async with db_module.AsyncSessionLocal() as session:
            repo = MemoryRepository(session)
            return await repo.get_robi_context(person_id=person_id)
    except Exception as exc:
        logger.warning("ws: error cargando contexto person_id=%s: %s", person_id, exc)
        return {}


async def _send_safe(websocket: WebSocket, text: str) -> None:
    """Envía un mensaje de texto ignorando errores si la conexión está cerrada."""
    try:
        if websocket.client_state == WebSocketState.CONNECTED:
            await websocket.send_text(text)
    except Exception as exc:
        logger.debug("ws: _send_safe ignorando error: %s", exc)
