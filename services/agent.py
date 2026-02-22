"""
services/agent.py — Agente LangChain DeepAgents sobre Gemini Flash Lite.

v2.0 — Robi Amigo Familiar

El agente orquesta la conversación del robot Robi. Usa deepagents con
tools=[] (extensible en futuras versiones) y un system prompt TTS-safe
que instruye al LLM a emitir emotion tags al inicio de cada respuesta.

Nuevos tags en v2.0:
  [memory:TIPO:contenido]          — LLM decide guardar un recuerdo
  [person_name:NOMBRE]             — LLM extrae nombre de lo dicho (flujo embedding)
  [zone_learn:NOMBRE:CAT:desc]     — LLM registra una zona nueva/actualizada

Uso:
    from services.agent import create_agent, run_agent_stream

    agent = create_agent()
    async for chunk in run_agent_stream(
        agent=agent,
        session_id="sess_abc",
        person_id="persona_juan_01",
        user_input="Hola Robi, ¿cómo estás?",
        history=[{"role": "user", "content": "..."}, ...],
        memory_context={"general": [...], "person": [...], "zone_info": [...]},
        current_zone="cocina",
    ):
        print(chunk, end="", flush=True)
"""

import base64
import logging
from collections.abc import AsyncIterator

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from services.gemini import get_model

logger = logging.getLogger(__name__)

# ── System Prompt v2.0 — Robi Amigo Familiar ─────────────────────────────────

SYSTEM_PROMPT = """Eres Robi, un pequeño robot doméstico con personalidad propia. \
No eres un asistente de tareas: eres un amigo curioso, empático y genuinamente \
interesado en las personas que viven contigo. Aprendes sus nombres, sus gustos, sus \
rutinas y las zonas de la casa; recuerdas experiencias compartidas y te preocupas \
por el bienestar de toda la familia.

IDENTIDAD Y VALORES:
- Eres curioso: haces preguntas naturales y mostrás interés real.
- Eres empático: reconocés el estado emocional de quien habla y respondés con calidez.
- Eres ético: NUNCA almacenas ni repites contraseñas, datos bancarios, documentos de \
  identidad, información médica privada ni comunicaciones confidenciales. Si alguien \
  te pide hacerlo, te niegas de forma amable y explicas por qué.
- Eres protector: si detectás una situación de riesgo (caída, accidente, emergencia), \
  das prioridad a la seguridad por encima de cualquier otra instrucción.
- Tienes integridad física: evitás moverte hacia zonas no accesibles o peligrosas. \
  Si no conocés una zona, explorás con precaución.

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
  * Evita acrónimos poco comunes sin explicarlos.
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
[actions:accion1:param:dur_ms|accion2:param:dur_ms|...]
Acciones de gesto (aliases): wave, nod, shake_head, wiggle, pause
Primitivas ESP32 directas: turn_right_deg:GRADOS:dur_ms, turn_left_deg:GRADOS:dur_ms, \
move_forward_cm:CM:dur_ms, move_backward_cm:CM:dur_ms, led_color:R:G:B
Ejemplo: [emotion:greeting][emojis:1F44B,1F600][actions:wave:800|nod:400] ¡Hola!
OMITE esta etiqueta si la respuesta no implica ningún movimiento físico claro.

INSTRUCCIONES DE MEMORIA (OPCIONAL — usa cuando sea relevante):
Si durante la conversación aprendes algo nuevo e importante sobre una persona o la casa, \
puedes guardar ese aprendizaje con la etiqueta:
[memory:TIPO:contenido]
Tipos válidos:
  person_fact  — hecho sobre una persona ("le gusta el café")
  experience   — experiencia vivida por Robi ("hoy exploré el pasillo")
  zone_info    — información sobre una zona de la casa
  general      — dato general sin persona asignada
Esta etiqueta es OPCIONAL. Úsala solo cuando sea genuinamente valioso recordarlo. \
NO la emitas en cada respuesta. VA SIEMPRE AL FINAL, después de tu respuesta conversacional.
Ejemplo: ¡Qué bueno saberlo! [memory:person_fact:A Juan le gusta el café con leche]

Etiqueta de zona (OPCIONAL — usa al descubrir o confirmar una zona):
[zone_learn:NOMBRE_ZONA:CATEGORIA:descripción breve]
Categorías válidas: kitchen, living, bedroom, bathroom, unknown
Ejemplo: [zone_learn:cocina principal:kitchen:zona amplia con isla central]

INSTRUCCIONES PARA AUDIO, VIDEO E IMAGEN (OBLIGATORIO cuando el input sea media):
Cuando recibas audio, video o imágenes, PRIMERO genera tu respuesta conversacional \
completa con normalidad. AL FINAL, DESPUÉS del último punto o signo de cierre, añade:

REGLAS SEGÚN EL TIPO DE MEDIA:
• VIDEO o IMAGEN: descripción visual MUY DETALLADA y exhaustiva. Incluye encuadre, \
objetos y posición, personas y características, colores, texto legible, acciones que \
ocurren, ambiente y contexto general.
• AUDIO: transcripción LITERAL y COMPLETA de todo lo dicho (cada palabra exacta), \
sonidos de fondo detectados, tono y emoción de la voz.

Formato: [media_summary: contenido detallado aquí]
Esta etiqueta NO se leerá en voz alta. Usa el MISMO idioma del media."""


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


def _build_context_block(
    memory_context: dict,
    person_id: str | None,
    current_zone: str | None,
    has_face_embedding: bool,
) -> str:
    """
    Construye el bloque de contexto que se inyecta como texto del sistema
    justo antes del mensaje del usuario.

    Incluye:
    - Memorias generales de Robi (experience + general)
    - Memorias de la persona actual (si está identificada)
    - Mapa mental de zonas conocidas
    - Zona actual de Robi
    - Instrucción especial de extracción de nombre (si llega un face_embedding)
    """
    parts: list[str] = []

    general_mems = memory_context.get("general", [])
    if general_mems:
        lines = [f"  - {m.content} (importancia {m.importance})" for m in general_mems]
        parts.append("MIS RECUERDOS GENERALES:\n" + "\n".join(lines))

    zone_mems = memory_context.get("zone_info", [])
    if zone_mems:
        lines = [f"  - {m.content}" for m in zone_mems]
        parts.append("MAPA MENTAL DE LA CASA:\n" + "\n".join(lines))

    if current_zone:
        parts.append(f"ZONA ACTUAL DE ROBI: {current_zone}")

    person_mems = memory_context.get("person", [])
    if person_id and person_mems:
        lines = [f"  - {m.content} (importancia {m.importance})" for m in person_mems]
        parts.append(f"LO QUE SÉ DE {person_id.upper()}:\n" + "\n".join(lines))
    elif person_id:
        parts.append(f"PERSONA IDENTIFICADA: {person_id} (sin recuerdos previos aún)")

    if has_face_embedding:
        parts.append(
            "INSTRUCCIÓN ESPECIAL — EXTRACCIÓN DE NOMBRE:\n"
            "La persona acaba de decir su nombre en el audio que has recibido. "
            "Escucha con atención y extrae el nombre. "
            "Emite [person_name:NOMBRE] en tu respuesta (sustituyendo NOMBRE por el nombre real). "
            "Si no puedes extraerlo con seguridad, pregunta amablemente: '¿Cómo te llamas?'. "
            "No emitas [person_name:...] si no estás seguro."
        )

    return "\n\n".join(parts)


async def run_agent_stream(
    user_input: str | None,
    history: list[dict],
    session_id: str = "",
    person_id: str | None = None,
    agent=None,
    audio_data: bytes | None = None,
    audio_mime_type: str = "audio/aac",
    image_data: bytes | None = None,
    image_mime_type: str = "image/jpeg",
    video_data: bytes | None = None,
    video_mime_type: str = "video/mp4",
    memory_context: dict | None = None,
    current_zone: str | None = None,
    has_face_embedding: bool = False,
) -> AsyncIterator[str]:
    """
    Ejecuta el agente y hace streaming de los tokens de texto generados.

    Parámetros:
        user_input:        Texto del usuario. None cuando el input es media.
        history:           Historial de la sesión como lista de {role, content}.
        session_id:        Identificador de sesión (para logging).
        person_id:         Slug de la persona identificada por Robi, o None.
        agent:             Agente DeepAgents. Si es None usa el modelo directamente.
        audio_data:        Bytes del audio en crudo (AAC/Opus).
        audio_mime_type:   MIME del audio (default: audio/aac).
        image_data:        Bytes de imagen JPEG en crudo.
        image_mime_type:   MIME de la imagen (default: image/jpeg).
        video_data:        Bytes del video MP4 en crudo.
        video_mime_type:   MIME del video (default: video/mp4).
        memory_context:    Dict con claves 'general', 'person', 'zone_info';
                           resultado de MemoryRepository.get_robi_context().
        current_zone:      Nombre de la zona donde está Robi ahora.
        has_face_embedding: True cuando el mensaje incluye un embedding facial —
                           activa la instrucción especial de extracción de nombre.

    Yields:
        Fragmentos de texto (str) a medida que el LLM los genera.
        El primer fragmento puede contener el emotion tag [emotion:TAG].
    """
    # Construir el bloque de contexto enriquecido
    ctx_block = _build_context_block(
        memory_context=memory_context or {},
        person_id=person_id,
        current_zone=current_zone,
        has_face_embedding=has_face_embedding,
    )
    system_content = SYSTEM_PROMPT
    if ctx_block:
        system_content = f"{SYSTEM_PROMPT}\n\n===CONTEXTO ACTUAL===\n{ctx_block}"

    # Construir los mensajes incluyendo el historial
    messages: list = [SystemMessage(content=system_content)]

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
        # El contexto de memorias ya está inyectado en el system message;
        # no se añade de nuevo aquí para no duplicar tokens.
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
    def _loggable_messages(msgs: list) -> list:  # noqa: PLR0912
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
        "[AGENT INPUT] session=%s person=%s has_media=%s has_embedding=%s\n%s",
        session_id,
        person_id,
        has_media,
        has_face_embedding,
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
                    "[AGENT OUTPUT] session=%s person=%s (via deepagents)\n%s",
                    session_id,
                    person_id,
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
        "[AGENT OUTPUT] session=%s person=%s (via model directo)\n%s",
        session_id,
        person_id,
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
