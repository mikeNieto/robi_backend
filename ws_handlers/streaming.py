"""
ws_handlers/streaming.py — Handler WebSocket principal para /ws/interact.

Implementa el flujo completo de §3.4:
  1. Acepta la conexión y autentica vía API Key.
  2. Bucle de mensajes: acumula audio binario, maneja texto e interaction_start.
  3. Al recibir audio_end o text: carga memoria, historial y lanza el agente.
  4. Parsea [emotion:TAG] del primer token del stream → envía emotion de inmediato.
  5. Envía text_chunk progresivamente.
  6. Al finalizar el stream: classify_intent → capture_request si aplica.
  7. Envía response_meta y stream_end.
  8. Background: guarda messages en el historial + compactación.

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
from datetime import datetime, timezone

import db as db_module
from fastapi import WebSocket
from starlette.websockets import WebSocketState

from db import InteractionRow
from repositories.memory import MemoryRepository
from services.agent import create_agent, run_agent_stream
from services.expression import emotion_to_emojis, parse_emotion_tag, parse_emojis_tag
from services.movement import build_move_sequence, parse_actions_tag
from services.history import ConversationHistory
from services.intent import classify_intent
from ws_handlers.auth import authenticate_websocket
from ws_handlers.protocol import (
    make_capture_request,
    make_emotion,
    make_error,
    make_response_meta,
    make_stream_end,
    make_text_chunk,
    new_session_id,
)

logger = logging.getLogger(__name__)

# Tamaño máximo del buffer de cabecera para capturar todos los tags de control:
# [emotion:TAG][emojis:CODE,...][actions:step|...][media_summary: ...]
_MAX_HEADER_BUFFER = 500

# Regex para eliminar/extraer el tag [media_summary: ...] de texto producido por el LLM
_MEDIA_SUMMARY_RE = re.compile(
    r"\[media_summary:\s*.*?\]",
    re.DOTALL | re.IGNORECASE,
)
# String literal de apertura del tag (para búsqueda rápida case-insensitive en pending_buf)
_MEDIA_SUMMARY_OPEN = "[media_summary:"


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
    user_id: str = "unknown"
    request_id: str = ""
    audio_buffer: bytes = b""

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
                user_id = msg.get("user_id", "unknown")
                request_id = msg.get("request_id") or new_session_id()
                audio_buffer = b""  # limpiar buffer de interacción anterior
                logger.debug(
                    "ws: interaction_start user_id=%s request_id=%s",
                    user_id,
                    request_id,
                )

            elif client_type == "text":
                user_input = msg.get("content", "")
                if not request_id:
                    request_id = msg.get("request_id") or new_session_id()
                else:
                    request_id = msg.get("request_id") or request_id

                if user_input:
                    await _process_interaction(
                        websocket=websocket,
                        user_id=user_id,
                        request_id=request_id,
                        user_input=user_input,
                        input_type="text",
                        history_service=history_service,
                        session_id=session_id,
                        agent=agent,
                    )

            elif client_type == "audio_end":
                if not request_id:
                    request_id = msg.get("request_id") or new_session_id()
                else:
                    request_id = msg.get("request_id") or request_id

                # El audio se procesa si hay datos; por ahora usamos
                # un placeholder ya que el pipeline STT se añadirá posteriormente.
                if audio_buffer:
                    audio_data = audio_buffer
                    audio_buffer = b""
                    await _process_interaction(
                        websocket=websocket,
                        user_id=user_id,
                        request_id=request_id,
                        user_input=None,
                        input_type="audio",
                        audio_data=audio_data,
                        history_service=history_service,
                        session_id=session_id,
                        agent=agent,
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
                # Multimodal: imagen de contexto o registro — se envía directo a Gemini
                if not request_id:
                    request_id = msg.get("request_id") or new_session_id()
                else:
                    request_id = msg.get("request_id") or request_id
                # Decodificar los bytes de imagen del campo base64
                raw_b64 = msg.get("data", "")
                image_bytes: bytes | None = None
                if raw_b64:
                    try:
                        image_bytes = base64.b64decode(raw_b64)
                    except Exception:
                        image_bytes = None
                # Texto opcional enviado junto a la imagen (multimodal texto+imagen)
                inline_text: str | None = msg.get("text") or None
                await _process_interaction(
                    websocket=websocket,
                    user_id=user_id,
                    request_id=request_id,
                    user_input=inline_text,
                    input_type="vision",
                    image_data=image_bytes,
                    history_service=history_service,
                    session_id=session_id,
                    agent=agent,
                )

            elif client_type == "video":
                if not request_id:
                    request_id = msg.get("request_id") or new_session_id()
                else:
                    request_id = msg.get("request_id") or request_id
                # Decodificar los bytes de video del campo base64
                raw_b64 = msg.get("data", "")
                video_bytes: bytes | None = None
                if raw_b64:
                    try:
                        video_bytes = base64.b64decode(raw_b64)
                    except Exception:
                        video_bytes = None
                # Texto opcional enviado junto al video (multimodal texto+video)
                inline_text_v: str | None = msg.get("text") or None
                await _process_interaction(
                    websocket=websocket,
                    user_id=user_id,
                    request_id=request_id,
                    user_input=inline_text_v,
                    input_type="vision",
                    video_data=video_bytes,
                    history_service=history_service,
                    session_id=session_id,
                    agent=agent,
                )

            elif client_type == "multimodal":
                # Mensaje único con cualquier combinación de texto + audio + imagen + video
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

                input_type_mm = (
                    "vision"
                    if (mm_image or mm_video)
                    else ("audio" if mm_audio else "text")
                )
                await _process_interaction(
                    websocket=websocket,
                    user_id=user_id,
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
    user_id: str,
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
) -> None:
    """
    Procesa una interacción completa:
      load memory → run agent stream → emit emotion + text_chunks → emit meta

    Para interacciones de audio/imagen/video el contenido media se envía directamente
    a Gemini que actúa como STT+LLM sin pipeline STT intermedio (§3.4, §1.3).
    """
    start_time = time.monotonic()

    # 1. Cargar memoria del usuario
    memories = await _load_memories(user_id)

    # 2. Obtener historial de la sesión
    history = history_service.get_history(session_id)

    # 3. Preparar el input para el agente
    #    • Para texto: enriquecer con contexto de memoria en el mismo string.
    #    • Para media (audio/imagen/video): pasar el contexto de memoria separado
    #      como texto y el media como bytes — Gemini lo recibe todo junto.
    has_media = (
        audio_data is not None or image_data is not None or video_data is not None
    )

    if has_media:
        enriched_input: str | None = None
        memory_context = _build_memory_context(memories)
        # Si el usuario envió texto junto al media (p.ej. "¿qué ves?"),
        # añadirlo como contexto para que Gemini lo reciba junto a la imagen/audio.
        if user_input:
            user_text_part = f"Mensaje del usuario: {user_input}"
            memory_context = (
                f"{memory_context}\n\n{user_text_part}".strip()
                if memory_context
                else user_text_part
            )
    else:
        enriched_input = _build_context_input(user_input or "", memories)
        memory_context = ""

    # 4. Streaming del agente
    full_response = ""
    emotion_tag = "neutral"
    emotion_sent = False
    prefix_buf = ""
    contextual_emojis: list[
        str
    ] = []  # códigos OpenMoji del tema (sugeridos por el LLM)
    response_actions: list[
        dict
    ] = []  # acciones físicas del robot (sugeridas por el LLM)

    # El LLM emite [media_summary: ...] AL FINAL de su respuesta.
    # Usamos pending_buf para detectar el tag aunque llegue fragmentado entre chunks.
    media_summary: str = ""
    pending_buf: str = ""

    try:
        async for chunk in run_agent_stream(
            user_input=enriched_input,
            history=history,
            session_id=session_id,
            user_id=user_id,
            agent=agent,
            audio_data=audio_data,
            audio_mime_type=audio_mime_type,
            image_data=image_data,
            image_mime_type=image_mime_type,
            video_data=video_data,
            video_mime_type=video_mime_type,
            memory_context=memory_context,
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
                        user_identified=user_id if user_id != "unknown" else None,
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
                user_identified=user_id if user_id != "unknown" else None,
            ),
        )
        if full_response:
            await _send_safe(websocket, make_text_chunk(request_id, full_response))

    # Flush del pending_buf al finalizar el stream — extrae [media_summary:...] si está ahí
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
        )
    )

    # 9. Background: guardar interacción en BD (solo usuarios registrados)
    if user_id != "unknown":
        asyncio.create_task(
            _save_interaction_bg(
                user_id=user_id,
                input_type=input_type,
                summary=full_response[:500],
            )
        )


# ── Background tasks ──────────────────────────────────────────────────────────


async def _save_history_bg(
    history_service: ConversationHistory,
    session_id: str,
    user_message: str,
    assistant_message: str,
) -> None:
    """Guarda los mensajes de la interacción en el historial y compacta si es necesario."""
    try:
        await history_service.add_message(session_id, "user", user_message)
        await history_service.add_message(session_id, "assistant", assistant_message)
        await history_service.compact_if_needed(session_id)
    except Exception as exc:
        logger.warning(
            "ws: error guardando historial session_id=%s: %s", session_id, exc
        )


async def _save_interaction_bg(
    user_id: str,
    input_type: str,
    summary: str,
) -> None:
    """Guarda la interacción en la tabla interactions (solo usuarios registrados)."""
    if db_module.AsyncSessionLocal is None:
        return
    try:
        async with db_module.AsyncSessionLocal() as session:
            row = InteractionRow(
                user_id=user_id,
                request_type=input_type,
                summary=summary,
                timestamp=datetime.now(timezone.utc),
            )
            session.add(row)
            await session.commit()
    except Exception as exc:
        logger.warning("ws: error guardando interacción user_id=%s: %s", user_id, exc)


# ── Helpers ───────────────────────────────────────────────────────────────────


async def _load_memories(user_id: str) -> list:
    """Carga las memorias más importantes del usuario desde la BD."""
    if user_id == "unknown" or db_module.AsyncSessionLocal is None:
        return []
    try:
        async with db_module.AsyncSessionLocal() as session:
            repo = MemoryRepository(session)
            return await repo.get_recent_important(user_id, min_importance=5, limit=5)
    except Exception as exc:
        logger.warning("ws: error cargando memoria user_id=%s: %s", user_id, exc)
        return []


def _build_context_input(user_input: str, memories: list) -> str:
    """
    Añade el contexto de memorias del usuario al input antes de enviarlo al agente.
    Si no hay memorias, devuelve el input sin modificar.
    """
    if not memories:
        return user_input
    memory_lines = [f"- {m.content}" for m in memories]
    memory_block = "\n".join(memory_lines)
    return f"[Contexto del usuario:\n{memory_block}\n]\n\n{user_input}"


def _build_memory_context(memories: list) -> str:
    """
    Devuelve solo el bloque de contexto de memoria como string.
    Se usa en interacciones multimodal (audio/imagen/video) donde el input
    del usuario son bytes y el contexto se pasa como texto separado.
    Si no hay memorias, devuelve cadena vacía.
    """
    if not memories:
        return ""
    memory_lines = [f"- {m.content}" for m in memories]
    memory_block = "\n".join(memory_lines)
    return f"[Contexto del usuario:\n{memory_block}\n]"


async def _send_safe(websocket: WebSocket, text: str) -> None:
    """Envía un mensaje de texto ignorando errores si la conexión está cerrada."""
    try:
        if websocket.client_state == WebSocketState.CONNECTED:
            await websocket.send_text(text)
    except Exception as exc:
        logger.debug("ws: _send_safe ignorando error: %s", exc)
