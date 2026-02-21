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
from collections.abc import AsyncIterator

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

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

INSTRUCCIONES PARA AUDIO, VIDEO E IMAGEN (OBLIGATORIO cuando el input sea media):
Cuando recibas audio, video o imágenes como input, añade INMEDIATAMENTE después del \
[emotion:TAG] una etiqueta de resumen con el formato exacto:
[media_summary: descripción breve y clara en máximo 15 palabras de lo que contiene el audio/video/imagen]
Esta etiqueta es para uso interno del sistema y NO se leerá en voz alta; \
mejora el historial de conversación.
Ejemplo completo: [emotion:happy][media_summary: el usuario saluda y pregunta cómo está Robi] ¡Hola! Estoy muy bien, ¿y tú?
IMPORTANTE: usa el MISMO idioma del audio/video/imagen para el contenido del media_summary."""


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

    # Intentar stream via agente DeepAgents
    if agent is not None:
        try:
            async for chunk in _stream_via_agent(agent, messages, user_input, history):
                yield chunk
            return
        except Exception:
            pass  # fall through al modelo directo

    # Fallback: stream directo via modelo LangChain
    async for chunk in _stream_via_model(messages):
        yield chunk


async def _stream_via_agent(
    agent, messages: list, user_input: str | None, history: list[dict]
) -> AsyncIterator[str]:
    """Stream usando el harness DeepAgents (si está disponible)."""
    # deepagents puede exponer astream o ainvoke dependiendo de la versión
    if hasattr(agent, "astream"):
        async for event in agent.astream({"messages": messages}):
            # DeepAgents/LangGraph emite dicts con distintos formatos
            if isinstance(event, dict):
                for value in event.values():
                    if hasattr(value, "messages"):
                        for msg in value.messages:
                            if hasattr(msg, "content") and msg.content:
                                yield str(msg.content)
                    elif hasattr(value, "content") and value.content:
                        yield str(value.content)
            elif hasattr(event, "content") and event.content:
                yield str(event.content)
    elif hasattr(agent, "ainvoke"):
        result = await agent.ainvoke({"messages": messages})
        if isinstance(result, dict):
            for value in result.values():
                if hasattr(value, "messages") and value.messages:
                    last = value.messages[-1]
                    if hasattr(last, "content"):
                        yield str(last.content)
        elif hasattr(result, "content"):
            yield str(result.content)
    else:
        raise NotImplementedError("El agente no expone astream ni ainvoke")


async def _stream_via_model(messages: list) -> AsyncIterator[str]:
    """Stream directo usando ChatGoogleGenerativeAI (fallback sin agente)."""
    model = get_model()
    async for chunk in model.astream(messages):
        if hasattr(chunk, "content") and chunk.content:
            yield str(chunk.content)
