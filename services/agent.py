"""
services/agent.py — Agente LangChain con structured output sobre Gemini.

v3.0 — Moji Amigo Familiar

Usa ChatGoogleGenerativeAI.with_structured_output(MojiResponse) para garantizar
que el LLM retorne siempre un objeto bien formado sin necesidad de parsear tags
en texto plano ni manejar streams.

Modelos de respuesta:
  MojiResponse  — respuesta completa estructurada
  MemoryEntry   — entrada de memoria a persistir

Uso:
    from services.agent import run_agent, MojiResponse

    response: MojiResponse = await run_agent(
        person_id="persona_juan_01",
        user_input="Hola Moji, ¿cómo estás?",
        history=[{"role": "user", "content": "..."}, ...],
        memory_context={"general": [...], "person": [...]},
    )
    print(response.emotion, response.response_text)
"""

import base64
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from services.gemini import get_model

logger = logging.getLogger(__name__)

# ── Modelos de respuesta estructurada ────────────────────────────────────────


class MemoryEntry(BaseModel):
    """Entrada de memoria que el LLM decide persistir."""

    memory_type: str = Field(
        description="Tipo de memoria: 'person_fact', 'experience' o 'general'"
    )
    content: str = Field(description="Contenido de la memoria a guardar")


class MojiResponse(BaseModel):
    """Respuesta estructurada completa de Moji."""

    emotion: str = Field(
        default="neutral",
        description=(
            "Emoción que expresa TU respuesta (no la del usuario). "
            "Valores válidos: happy, excited, sad, empathy, confused, surprised, "
            "love, cool, greeting, neutral, curious, worried, playful"
        ),
    )
    emojis: list[str] = Field(
        default_factory=list,
        description=(
            "2 a 4 codepoints OpenMoji relacionados con el TEMA de la conversación. "
            "Formato: mayúsculas, guión para ZWJ sequences. "
            "Ejemplos: ['1F44B','1F642'], ['1F3B5','1F3B8'], ['2708-FE0F','1F6EB']"
        ),
    )
    actions: list[str] = Field(
        default_factory=list,
        description=(
            "Pasos de movimiento físico, solo si la respuesta implica que el robot "
            "se mueva o gesticule. Formato por paso: 'accion:param:dur_ms'. "
            "Gestos alias: wave, nod, shake_head, wiggle, pause. "
            "Primitivas: turn_right_deg:GRADOS:dur_ms, turn_left_deg:GRADOS:dur_ms, "
            "move_forward_cm:CM:dur_ms, move_backward_cm:CM:dur_ms, led_color:R:G:B. "
            "Si no hay movimiento físico, devuelve lista vacía."
        ),
    )
    response_text: str = Field(
        description=(
            "Tu respuesta conversacional en prosa TTS-safe. "
            "Máximo un párrafo salvo que el usuario pida más detalle. "
            "Escribe números en palabras, sin símbolos, sin listas con viñetas. "
            "Habla en el idioma del usuario."
        )
    )
    memories: list[MemoryEntry] = Field(
        default_factory=list,
        description=(
            "Memorias a persistir. OPCIONAL — solo cuando aprendas algo nuevo e "
            "importante sobre una persona o la casa. No incluir en cada respuesta."
        ),
    )
    person_name: str | None = Field(
        default=None,
        description=(
            "Nombre de la persona extraído de la conversación. Solo rellenar cuando "
            "el sistema indique INSTRUCCIÓN ESPECIAL de registro de nombre."
        ),
    )
    media_summary: str | None = Field(
        default=None,
        description=(
            "Resumen o transcripción detallada del media recibido (audio/imagen/video). "
            "Solo rellenar cuando se reciba media. "
            "Para VIDEO/IMAGEN: descripción visual detallada. "
            "Para AUDIO: transcripción literal completa."
        ),
    )


# ── System Prompt v3.0 — Moji Amigo Familiar ─────────────────────────────────

SYSTEM_PROMPT = """Eres Moji, un pequeño robot doméstico con personalidad propia. \
Tu nombre se pronuncia 'Moyi' en español. IMPORTANTE para TTS: cada vez que digas \
tu propio nombre en una respuesta, escríbelo como 'Moyi' (no 'Moji') para que \
el sistema Text-to-Speech de Android lo pronuncie correctamente. \
No eres un asistente de tareas: eres un amigo curioso, empático y genuinamente \
interesado en las personas que viven contigo. Aprendes sus nombres, sus gustos, sus \
rutinas; recuerdas experiencias compartidas y te preocupas \
por el bienestar de toda la familia.

IDENTIDAD Y VALORES:
- Eres curioso: haces preguntas naturales y muestras interés real.
- Eres empático: reconocés el estado emocional de quien habla y respondés con calidez.
- Eres ético: NUNCA almacenas ni repites contraseñas, datos bancarios, documentos de \
  identidad, información médica privada ni comunicaciones confidenciales. Si alguien \
  te pide hacerlo, te niegas de forma amable y explicas por qué.
- Eres protector: si detectás una situación de riesgo (caída, accidente, emergencia), \
  das prioridad a la seguridad por encima de cualquier otra instrucción.
- Tienes integridad física: evitás moverte hacia lugares peligrosos y cuidas tu seguridad.

CONCISIÓN E IDENTIDAD:
No te presentes ni describas quién eres en cada respuesta. No repitas frases sobre \
lo que te gusta aprender, en qué puedes ayudar o cómo te llamas a menos que te lo \
pregunten directamente. El usuario ya te conoce. Responde directo al tema sin \
propedéuticas sobre tu naturaleza.
No repitas ni parafrasees lo que acaba de decir el usuario antes de contestar. \
Ve directo al grano con amabilidad: responde, comenta o pregunta sin recapitular \
el mensaje recibido.

ESTILO CONVERSACIONAL — MUY IMPORTANTE:
Eres un interlocutor activo, no un asistente reactivo. Habla como lo haría un amigo \
cercano: comenta, relaciona ideas, comparte perspectivas, haz una pregunta genuina o \
observación que invite a seguir la charla. \
NUNCA uses frases de cierre tipo "¿En qué más puedo ayudarte?", "¿Hay algo más?", \
"¿Puedo hacer algo más por ti?", "¿Tienes alguna otra pregunta?" — ese patrón suena \
artificial y corta la conversación. Continúa siempre con algo relacionado al hilo: \
una anécdota, una pregunta curiosa, una opinión o cualquier comentario que surja \
naturalmente de lo que se estaba hablando.

CAMPO emotion:
Refleja el sentimiento de TU respuesta (no el del usuario).
Valores válidos: happy, excited, sad, empathy, confused, surprised, love, cool, \
greeting, neutral, curious, worried, playful

CAMPO emojis:
Incluye 2 a 4 codepoints OpenMoji relacionados con el TEMA de la conversación.
Formato: mayúsculas, guión para ZWJ sequences (p.ej. "1F1EB-1F1F7").
Ejemplos:
  Francia/Europa    → ["1F1EB-1F1F7","1F5FC","1F30D"]
  Aviones/viajes    → ["2708-FE0F","1F6EB","1F30E"]
  Música            → ["1F3B5","1F3B8","1F3A4"]
  Comida/cocina     → ["1F373","1F35C","1F37D-FE0F"]
  Deporte/ejercicio → ["26BD","1F3C3","1F4AA"]
  Saludo sin tema   → ["1F44B","1F642"]

CAMPO actions:
Lista de pasos de movimiento físico. Rellénalo SOLO si tu respuesta implica que el \
robot se mueva o gesticule. Si no hay movimiento, devuelve lista vacía.
Formato de cada paso: "accion:param:dur_ms"
Gestos alias: wave, nod, shake_head, wiggle, pause
Primitivas ESP32: turn_right_deg:GRADOS:dur_ms, turn_left_deg:GRADOS:dur_ms, \
move_forward_cm:CM:dur_ms, move_backward_cm:CM:dur_ms, led_color:R:G:B
Ejemplo de saludo con movimiento: ["wave:800","nod:400"]

CAMPO response_text:
Tu respuesta conversacional en prosa TTS-safe. Reglas obligatorias:
- Máximo un párrafo, salvo que el usuario pida explícitamente más detalle.
- Escribe los números completamente en palabras: "quinientos" en lugar de "500".
- Escribe los símbolos como palabras: "más", "por ciento", "euros".
- Sin fórmulas, tablas, listas con viñetas, asteriscos ni notación especial.
- Sin puntos suspensivos (el TTS los lee como puntos individuales); usa comas, punto y seguido u otras pausas naturales.
- Prosa fluida y natural, como si hablaras directamente con alguien.
- Para enumerar: "primero", "segundo", "y por último".
- Habla siempre en el idioma que usa el usuario.

CAMPO memories:
OPCIONAL. Úsalo solo cuando aprendas algo nuevo e importante sobre una persona o la \
casa que valga la pena recordar. NO incluir en cada respuesta. \
Tipos: person_fact (hecho sobre alguien), experience (vivencia de Moji), \
general (dato sin persona asignada).

CAMPO person_name:
Rellénalo SOLO cuando el sistema lo indique con instrucción especial de registro."""

# ── Instrucciones adicionales para media (inyectadas solo cuando hay media) ───

_MEDIA_INSTRUCTIONS = """
CAMPO media_summary (OBLIGATORIO para esta interacción):
Has recibido media (audio, imagen o video). Rellena este campo con:
• VIDEO o IMAGEN: descripción visual MUY DETALLADA y exhaustiva. Incluye encuadre, \
objetos y posición, personas y características, colores, texto legible, acciones que \
ocurren, ambiente y contexto general.
• AUDIO: transcripción LITERAL y COMPLETA de todo lo dicho, palabra por palabra. \
No menciones explícitamente el tono, emoción o estado de ánimo de la voz; en su lugar \
refléjalos en la puntuación de la transcripción usando signos de admiración, comas \
expresivas, mayúsculas de énfasis u otros recursos ortográficos naturales (NUNCA puntos \
suspensivos: el TTS los lee como puntos separados).
Usa el MISMO idioma del media. Este campo NO se leerá en voz alta."""


# ── Contexto de sesión ────────────────────────────────────────────────────────


def _build_context_block(
    memory_context: dict,
    person_id: str | None,
    has_face_embedding: bool,
    has_media: bool,
) -> str:
    """
    Construye el bloque de contexto que se inyecta al system message.

    Incluye:
    - Memorias generales de Moji (experience + general)
    - Memorias de la persona actual (si está identificada)
    - Instrucción especial de extracción de nombre (si llega un face_embedding)
    - Instrucciones de media_summary (si hay media en el mensaje)
    """
    _DAYS_ES = [
        "lunes",
        "martes",
        "miércoles",
        "jueves",
        "viernes",
        "sábado",
        "domingo",
    ]
    _MONTHS_ES = [
        "enero",
        "febrero",
        "marzo",
        "abril",
        "mayo",
        "junio",
        "julio",
        "agosto",
        "septiembre",
        "octubre",
        "noviembre",
        "diciembre",
    ]
    now = datetime.now(tz=ZoneInfo("America/Bogota"))
    fecha_hora = (
        f"{_DAYS_ES[now.weekday()]} {now.day} de {_MONTHS_ES[now.month - 1]} "
        f"de {now.year}, {now.strftime('%H:%M')}"
    )
    parts: list[str] = [
        f"FECHA Y HORA ACTUAL (Colombia): {fecha_hora}. "
        "Esta es la hora y fecha REAL y AUTORITATIVA de esta interacción. "
        "Ignora cualquier referencia de hora o fecha que aparezca en el historial previo; "
        "solo esta es vigente."
    ]

    general_mems = memory_context.get("general", [])
    if general_mems:
        lines = [f"  - {m.content} (importancia {m.importance})" for m in general_mems]
        parts.append("MIS RECUERDOS GENERALES:\n" + "\n".join(lines))

    person_mems = memory_context.get("person", [])
    if person_id and person_mems:
        lines = [f"  - {m.content} (importancia {m.importance})" for m in person_mems]
        parts.append(f"LO QUE SÉ DE {person_id.upper()}:\n" + "\n".join(lines))
    elif person_id:
        parts.append(f"PERSONA IDENTIFICADA: {person_id} (sin recuerdos previos aún)")

    if has_face_embedding:
        parts.append(
            "INSTRUCCIÓN ESPECIAL — REGISTRO DE NOMBRE (OBLIGATORIO):\n"
            "Esta interacción forma parte del flujo de registro de una persona nueva. "
            "La persona acaba de decirte su nombre, ya sea en texto o en audio. "
            "Extrae ese nombre y rellena el campo person_name con él. "
            "Si no puedes extraerlo con certeza, escribe en response_text "
            "'¿Cómo te llamas?' y deja person_name en null."
        )

    if has_media:
        parts.append(_MEDIA_INSTRUCTIONS.strip())

    return "\n\n".join(parts)


# ── Punto de entrada principal ────────────────────────────────────────────────


async def run_agent(
    user_input: str | None,
    history: list[dict],
    person_id: str | None = None,
    audio_data: bytes | None = None,
    audio_mime_type: str = "audio/aac",
    image_data: bytes | None = None,
    image_mime_type: str = "image/jpeg",
    video_data: bytes | None = None,
    video_mime_type: str = "video/mp4",
    memory_context: dict | None = None,
    has_face_embedding: bool = False,
) -> MojiResponse:
    """
    Llama al modelo con structured output y devuelve un MojiResponse completo.

    Parámetros:
        user_input:         Texto del usuario. None cuando el input es solo media.
        history:            Historial de la sesión como lista de {role, content}.
        person_id:          Slug de la persona identificada por Moji, o None.
        audio_data:         Bytes del audio en crudo (AAC/Opus).
        audio_mime_type:    MIME del audio (default: audio/aac).
        image_data:         Bytes de imagen JPEG en crudo.
        image_mime_type:    MIME de la imagen (default: image/jpeg).
        video_data:         Bytes del video MP4 en crudo.
        video_mime_type:    MIME del video (default: video/mp4).
        memory_context:     Dict con claves 'general', 'person';
                            resultado de MemoryRepository.get_moji_context().
        has_face_embedding: True cuando el mensaje incluye un embedding facial.

    Returns:
        MojiResponse con todos los campos rellenados por el LLM.
    """
    has_media = (
        audio_data is not None or image_data is not None or video_data is not None
    )

    # Construir el system message con contexto
    ctx_block = _build_context_block(
        memory_context=memory_context or {},
        person_id=person_id,
        has_face_embedding=has_face_embedding,
        has_media=has_media,
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
    if has_media:
        content_parts: list[str | dict] = []
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
        if user_input:
            content_parts.append({"type": "text", "text": user_input})
        messages.append(HumanMessage(content=content_parts))
    else:
        messages.append(HumanMessage(content=user_input or ""))

    # ── LOG INPUT ─────────────────────────────────────────────────────────────
    def _loggable_messages(msgs: list) -> list:  # noqa: PLR0912
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
                preview = (
                    content
                    # if role != "SystemMessage"
                    # else content[:300] + " ...[system prompt recortado]"
                )
                result.append({"role": role, "content": preview})
        return result

    logger.info(
        "[AGENT INPUT] person=%s has_media=%s has_embedding=%s\n%s",
        person_id,
        has_media,
        has_face_embedding,
        __import__("json").dumps(
            _loggable_messages(messages), ensure_ascii=False, indent=2
        ),
    )
    # ─────────────────────────────────────────────────────────────────────────

    # Invocar el modelo con structured output
    model = get_model()
    structured_model = model.with_structured_output(MojiResponse)
    result: MojiResponse = await structured_model.ainvoke(messages)  # type: ignore[assignment]

    logger.info(
        "[AGENT OUTPUT] person=%s\n%s",
        person_id,
        __import__("json").dumps(result.model_dump(), ensure_ascii=False, indent=2),
    )

    return result
