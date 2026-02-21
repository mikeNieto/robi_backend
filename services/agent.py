"""
services/agent.py — Agente LangChain DeepAgents sobre Gemini Flash Lite.

El agente orquesta la conversación del robot Robi. Usa deepagents con
tools=[] (extensible en futuras versiones) y un system prompt TTS-safe
que instruye al LLM a emitir emotion tags al inicio de cada respuesta.

Uso:
    from services.agent import create_agent, run_agent_stream

    agent = create_agent()
    async for chunk in run_agent_stream(
        agent=agent,
        session_id="sess_abc",
        user_id="user_juan",
        user_input="Hola Robi, ¿cómo estás?",
        history=[{"role": "user", "content": "..."}, ...],
    ):
        print(chunk, end="", flush=True)
"""

import base64
import logging
from collections.abc import AsyncIterator

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

logger = logging.getLogger(__name__)

from services.gemini import get_model

# ── System Prompt TTS-safe (§3.7) ─────────────────────────────────────────────

SYSTEM_PROMPT = """Eres Robi, un robot doméstico amigable e interactivo. Tienes memoria de las personas \
con las que interactúas y adaptas tus respuestas según el contexto y las preferencias \
de cada usuario.

INSTRUCCIONES DE EMOCIÓN:
Antes de cada respuesta, emite una etiqueta de emoción que refleje el sentimiento \
de TU respuesta (no el del usuario). Formato: [emotion:TAG]
Tags válidos: happy, excited, sad, empathy, confused, surprised, love, cool, \
greeting, neutral, curious, worried, playful
Ejemplo: [emotion:empathy] Lo siento mucho, espero que te mejores pronto.

INSTRUCCIONES DE RESPUESTA (OBLIGATORIO):
- Da respuestas cortas de máximo un párrafo, a menos que el usuario pida \
  explícitamente una respuesta completa y detallada.
- Tus respuestas serán leídas en voz alta por un sistema Text-to-Speech. \
  Por eso es CRUCIAL seguir estas reglas:
  * Escribe los números completamente en palabras: "quinientos" en lugar de "500", \
    "tres mil" en lugar de "3.000" o "3,000".
  * Escribe los símbolos como palabras: "más" en lugar de "+", "por ciento" \
    en lugar de "%", "euros" en lugar de "€".
  * No uses fórmulas matemáticas, tablas, listas con viñetas, asteriscos, \
    guiones decorativos, separadores de miles ni ninguna notación que suene \
    extraño al ser leída linealmente.
  * Redacta en prosa fluida y natural, como si hablaras directamente con alguien.
  * Si necesitas enumerar elementos, hazlo con "primero", "segundo", "y por último" \
    en lugar de "1.", "2.", "3.".
  * Evita acrónimos poco comunes sin explicarlos. Pronuncia las siglas como \
    palabras o explícalas: "la Inteligencia Artificial" en vez de solo "la IA".
- Habla siempre en el idioma que usa el usuario.

INSTRUCCIONES DE EMOJIS CONTEXTUALES (OBLIGATORIO en TODAS las respuestas):
Inmediatamente después del [emotion:TAG], emite 2 a 4 codepoints OpenMoji relacionados con el TEMA:
[emojis:CODE1,CODE2,CODE3]
Formato: mayúsculas, guión para sequences (p.ej. "1F1EB-1F1F7"). Ejemplos:
  Francia/Europa    → [emojis:1F1EB-1F1F7,1F5FC,1F30D]
  Aviones/viajes    → [emojis:2708-FE0F,1F6EB,1F30E]
  Música            → [emojis:1F3B5,1F3B8,1F3A4]
  Comida/cocina     → [emojis:1F373,1F35C,1F37D-FE0F]
  Deporte/ejercicio → [emojis:26BD,1F3C3,1F4AA]
  Saludo sin tema   → [emojis:1F44B,1F642]
USA SIEMPRE esta etiqueta; NO se leerá en voz alta.

INSTRUCCIONES DE ACCIONES FÍSICAS (solo cuando la respuesta implique movimiento):
Si tu respuesta implica que el robot se mueva o gesticule, añade después de [emojis:...]:
[actions:accion1:dur_ms|accion2:dir:dur_ms|...]
Acciones válidas: wave, rotate_left, rotate_right, move_forward, move_backward, \
nod, shake_head, wiggle, pause
Ejemplo: [emotion:greeting][emojis:1F44B,1F600][actions:wave:800|nod:400] ¡Hola!
OMITE esta etiqueta si la respuesta no implica ningún movimiento físico claro.

INSTRUCCIONES PARA AUDIO, VIDEO E IMAGEN (OBLIGATORIO cuando el input sea media):
Cuando recibas audio, video o imágenes, PRIMERO genera tu respuesta conversacional \
completa con normalidad. AL FINAL de tu respuesta, DESPUÉS del último punto o \
signo de cierre, añade la etiqueta de resumen detallado:

REGLAS SEGÚN EL TIPO DE MEDIA:
• VIDEO o IMAGEN: descripción visual MUY DETALLADA y exhaustiva. Incluye sin omitir: \
encuadre y ángulo, todos los objetos y su posición, personas y sus características \
visibles, colores, texto legible en pantalla, acciones que ocurren, ambiente, modo \
claro/oscuro del sistema si es una pantalla, contexto general y cualquier detalle \
relevante. No hay límite de palabras — sé tan exhaustivo como sea necesario.
• AUDIO: transcripción LITERAL y COMPLETA de todo lo dicho (cada palabra exacta), \
seguida de los sonidos de fondo detectados, y finalmente el tono y emoción de la voz \
(nervioso, alegre, triste, enojado, relajado, seguro, etc.).

Formato de la etiqueta (siempre al final): [media_summary: contenido detallado aquí]

IMPORTANTE:
- Esta etiqueta va SIEMPRE AL FINAL, después de toda tu respuesta conversacional.
- NO se leerá en voz alta; es exclusivamente para el historial interno del sistema.
- Usa el MISMO idioma del audio/video/imagen para el contenido del resumen.
- Ejemplo de posición correcta: "¡Aquí tienes la información! [...respuesta...] \
[media_summary: pantalla Windows 11 en modo oscuro; se navega entre carpetas Pictures, \
Music y Documents; cursor visible, barra de tareas inferior, sin iconos en escritorio]" """


# ── Creación del agente ───────────────────────────────────────────────────────


def create_agent():
    """
    Crea y devuelve el agente DeepAgents sobre Gemini Flash Lite.

    Usa tools=[] — arquitectura extensible para futuras versiones.
    Devuelve el agente listo para llamar con run_agent_stream().
    """
    try:
        from deepagents import create_deep_agent  # type: ignore[import-untyped]

        model = get_model()
        agent = create_deep_agent(
            model=model,
            tools=[],
            system_prompt=SYSTEM_PROMPT,
        )
        return agent
    except Exception:
        # Si deepagents falla (p.ej. en tests sin API key), devolvemos None.
        # run_agent_stream maneja este caso usando el modelo directamente.
        return None


# ── Streaming del agente ──────────────────────────────────────────────────────


async def run_agent_stream(
    user_input: str | None,
    history: list[dict],
    session_id: str = "",
    user_id: str = "unknown",
    agent=None,
    audio_data: bytes | None = None,
    audio_mime_type: str = "audio/aac",
    image_data: bytes | None = None,
    image_mime_type: str = "image/jpeg",
    video_data: bytes | None = None,
    video_mime_type: str = "video/mp4",
    memory_context: str = "",
) -> AsyncIterator[str]:
    """
    Ejecuta el agente y hace streaming de los tokens de texto generados.

    Parámetros:
        user_input:     Texto del usuario. None cuando el input es media (audio/imagen/video).
        history:        Historial de la sesión como lista de {role, content}.
        session_id:     Identificador de sesión (para logging).
        user_id:        Identificador del usuario.
        agent:          Agente DeepAgents. Si es None usa el modelo directamente.
        audio_data:     Bytes del audio en crudo (AAC/Opus). Gemini actúa como STT+LLM.
        audio_mime_type: MIME del audio (default: audio/aac).
        image_data:     Bytes de imagen JPEG en crudo.
        image_mime_type: MIME de la imagen (default: image/jpeg).
        video_data:     Bytes del video MP4 en crudo.
        video_mime_type: MIME del video (default: video/mp4).
        memory_context: Contexto de memoria del usuario, añadido como texto junto al media.

    Yields:
        Fragmentos de texto (str) a medida que el LLM los genera.
        El primer fragmento puede contener el emotion tag [emotion:TAG].
    """
    # Construir los mensajes incluyendo el historial
    messages: list = [SystemMessage(content=SYSTEM_PROMPT)]

    for msg in history:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if role == "user":
            messages.append(HumanMessage(content=content))
        else:
            messages.append(AIMessage(content=content))

    # Construir el mensaje del usuario actual (texto o multimodal)
    has_media = (
        audio_data is not None or image_data is not None or video_data is not None
    )
    if has_media:
        # Gemini recibe el media directamente — actúa como STT+LLM sin pipeline intermedio
        content_parts: list[str | dict] = []
        if memory_context:
            content_parts.append({"type": "text", "text": memory_context})
        if audio_data is not None:
            content_parts.append(
                {
                    "type": "media",
                    "mime_type": audio_mime_type,
                    "data": base64.b64encode(audio_data).decode(),
                }
            )
        if image_data is not None:
            content_parts.append(
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:{image_mime_type};base64,{base64.b64encode(image_data).decode()}"
                    },
                }
            )
        if video_data is not None:
            content_parts.append(
                {
                    "type": "media",
                    "mime_type": video_mime_type,
                    "data": base64.b64encode(video_data).decode(),
                }
            )
        messages.append(HumanMessage(content=content_parts))
    else:
        messages.append(HumanMessage(content=user_input or ""))

    # ── LOG INPUT ─────────────────────────────────────────────────────────────
    def _loggable_messages(msgs: list) -> list:
        """Copia de messages sin los bytes b64 raw (los reemplaza con metadata)."""
        result = []
        for m in msgs:
            role = m.__class__.__name__
            content = m.content
            if isinstance(content, list):
                parts_log = []
                for part in content:
                    if isinstance(part, dict):
                        t = part.get("type", "")
                        if t == "media":
                            size = len(base64.b64decode(part.get("data", "")))
                            parts_log.append(
                                {
                                    "type": "media",
                                    "mime_type": part.get("mime_type"),
                                    "size_bytes": size,
                                }
                            )
                        elif t == "image_url":
                            url = part.get("image_url", {}).get("url", "")
                            # url es "data:<mime>;base64,<data>"
                            b64_part = url.split(",", 1)[1] if "," in url else ""
                            size = len(base64.b64decode(b64_part)) if b64_part else 0
                            mime = url.split(";")[0].replace("data:", "") if url else ""
                            parts_log.append(
                                {"type": "image", "mime_type": mime, "size_bytes": size}
                            )
                        else:
                            parts_log.append(part)
                    else:
                        parts_log.append(part)
                result.append({"role": role, "content": parts_log})
            else:
                # Para el system prompt recortamos a 300 chars para no saturar
                preview = (
                    content
                    if role != "SystemMessage"
                    else content[:300] + " ...[system prompt recortado]"
                )
                result.append({"role": role, "content": preview})
        return result

    logger.info(
        "[AGENT INPUT] session=%s user=%s has_media=%s\n%s",
        session_id,
        user_id,
        has_media,
        __import__("json").dumps(
            _loggable_messages(messages), ensure_ascii=False, indent=2
        ),
    )
    # ─────────────────────────────────────────────────────────────────────────

    # Intentar stream via agente DeepAgents
    full_output: list[str] = []
    if agent is not None:
        try:
            yielded = False
            async for chunk in _stream_via_agent(agent, messages, user_input, history):
                yielded = True
                full_output.append(chunk)
                yield chunk
            if yielded:
                logger.info(
                    "[AGENT OUTPUT] session=%s user=%s (via deepagents)\n%s",
                    session_id,
                    user_id,
                    "".join(full_output),
                )
                return
            # El agente no produjo ningún chunk — caer al modelo directo
        except Exception:
            pass  # fall through al modelo directo

    # Fallback: stream directo via modelo LangChain
    async for chunk in _stream_via_model(messages):
        full_output.append(chunk)
        yield chunk
    logger.info(
        "[AGENT OUTPUT] session=%s user=%s (via model directo)\n%s",
        session_id,
        user_id,
        "".join(full_output),
    )


async def _stream_via_agent(
    agent, messages: list, user_input: str | None, history: list[dict]
) -> AsyncIterator[str]:
    """Stream usando el harness DeepAgents (si está disponible)."""

    def _extract_from_value(value) -> str | None:
        """
        Extrae contenido de un valor de evento LangGraph.
        LangGraph emite AddableValuesDict (subclase de dict) — los mensajes
        están en value["messages"], no en value.messages.
        """
        # Caso dict / AddableValuesDict (LangGraph)
        if isinstance(value, dict):
            msgs = value.get("messages")
            if msgs:
                for msg in reversed(msgs):
                    content = getattr(msg, "content", None) or (
                        msg.get("content") if isinstance(msg, dict) else None
                    )
                    if content:
                        return str(content)
            content = value.get("content")
            return str(content) if content else None
        # Caso objeto con atributos (otros harnesses)
        if hasattr(value, "messages") and value.messages:
            for msg in reversed(value.messages):
                if hasattr(msg, "content") and msg.content:
                    return str(msg.content)
        if hasattr(value, "content") and value.content:
            return str(value.content)
        return None

    if hasattr(agent, "astream"):
        async for event in agent.astream({"messages": messages}):
            if isinstance(event, dict):
                for value in event.values():
                    text = _extract_from_value(value)
                    if text:
                        yield text
            elif hasattr(event, "content") and event.content:
                yield str(event.content)
    elif hasattr(agent, "ainvoke"):
        result = await agent.ainvoke({"messages": messages})
        if isinstance(result, dict):
            for value in result.values():
                text = _extract_from_value(value)
                if text:
                    yield text
                    return
        elif hasattr(result, "content") and result.content:
            yield str(result.content)
    else:
        raise NotImplementedError("El agente no expone astream ni ainvoke")


async def _stream_via_model(messages: list) -> AsyncIterator[str]:
    """Stream directo usando ChatGoogleGenerativeAI (fallback sin agente)."""
    model = get_model()
    async for chunk in model.astream(messages):
        if hasattr(chunk, "content") and chunk.content:
            yield str(chunk.content)
