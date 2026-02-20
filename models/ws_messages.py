"""
Modelos de mensajes WebSocket — union discriminada con Literal.

Mensajes del cliente (Android → Backend):
  AuthMessage, InteractionStartMessage, AudioEndMessage,
  TextMessage, ImageMessage, VideoMessage

Mensajes del servidor (Backend → Android):
  AuthOkMessage, UserRegisteredMessage, EmotionMessage,
  TextChunkMessage, ResponseMetaMessage, StreamEndMessage,
  WsErrorMessage
"""

from pydantic import BaseModel, Field
from typing import Literal, Any


# ═══════════════════════════════════════════════════════
# Mensajes del CLIENTE (Android → Backend)
# ═══════════════════════════════════════════════════════


class AuthMessage(BaseModel):
    type: Literal["auth"]
    api_key: str
    device_id: str = ""


class InteractionStartMessage(BaseModel):
    type: Literal["interaction_start"]
    request_id: str
    user_id: str  # "unknown" si no reconocido
    face_recognized: bool = False
    face_confidence: float | None = None  # similitud coseno 0-1; None si no reconocido
    context: dict[str, Any] = Field(default_factory=dict)


class AudioEndMessage(BaseModel):
    type: Literal["audio_end"]
    request_id: str


class TextMessage(BaseModel):
    type: Literal["text"]
    request_id: str
    content: str


class ImageMessage(BaseModel):
    type: Literal["image"]
    request_id: str
    purpose: Literal["registration", "context"]
    data: str  # base64 JPEG


class VideoMessage(BaseModel):
    type: Literal["video"]
    request_id: str
    duration_ms: int
    data: str  # base64 MP4


# Union discriminada de mensajes del cliente
ClientMessage = (
    AuthMessage
    | InteractionStartMessage
    | AudioEndMessage
    | TextMessage
    | ImageMessage
    | VideoMessage
)


# ═══════════════════════════════════════════════════════
# Mensajes del SERVIDOR (Backend → Android)
# ═══════════════════════════════════════════════════════


class AuthOkMessage(BaseModel):
    type: Literal["auth_ok"] = "auth_ok"
    session_id: str


class UserRegisteredMessage(BaseModel):
    type: Literal["user_registered"] = "user_registered"
    user_id: str
    name: str


class EmotionMessage(BaseModel):
    type: Literal["emotion"] = "emotion"
    request_id: str
    emotion: str  # happy | excited | sad | empathy | …
    user_identified: str | None = None
    confidence: float | None = None


class TextChunkMessage(BaseModel):
    type: Literal["text_chunk"] = "text_chunk"
    request_id: str
    text: str


class ExpressionPayload(BaseModel):
    emojis: list[str]  # códigos Unicode OpenMoji, p.ej. ["1F44B"]
    duration_per_emoji: int = 2000
    transition: str = "bounce"


class MoveAction(BaseModel):
    type: Literal["move"]
    params: dict[str, Any]


class MoveSequenceAction(BaseModel):
    type: Literal["move_sequence"]
    total_duration_ms: int
    emotion_during: str
    steps: list[dict[str, Any]]


class LightAction(BaseModel):
    type: Literal["light"]
    params: dict[str, Any]


ResponseAction = MoveAction | MoveSequenceAction | LightAction


class ResponseMetaMessage(BaseModel):
    type: Literal["response_meta"] = "response_meta"
    request_id: str
    response_text: str
    expression: ExpressionPayload
    actions: list[dict[str, Any]] = Field(default_factory=list)


class StreamEndMessage(BaseModel):
    type: Literal["stream_end"] = "stream_end"
    request_id: str
    processing_time_ms: int = 0


class WsErrorMessage(BaseModel):
    type: Literal["error"] = "error"
    request_id: str | None = None
    error_code: str
    message: str
    recoverable: bool = False


# Union discriminada de mensajes del servidor
ServerMessage = (
    AuthOkMessage
    | UserRegisteredMessage
    | EmotionMessage
    | TextChunkMessage
    | ResponseMetaMessage
    | StreamEndMessage
    | WsErrorMessage
)
