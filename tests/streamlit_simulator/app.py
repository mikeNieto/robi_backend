"""
tests/streamlit_simulator/app.py — Simulador Streamlit para Robi Backend

Permite probar el backend completo sin necesidad de Android ni ESP32.

Uso:
    uv run streamlit run tests/streamlit_simulator/app.py

Requiere el backend corriendo:
    uv run uvicorn main:app --reload --ws wsproto
"""

import json
import time
import uuid
from pathlib import Path

import requests
import streamlit as st

# websockets.sync.client está disponible en websockets >= 11 (tenemos <15)
from websockets.sync.client import connect as ws_connect

# ── Constantes ────────────────────────────────────────────────────────────────

# Todos los tags de emoción y sus códigos OpenMoji
EMOTION_OPENMOJI: dict[str, str] = {
    "happy": "1F600",
    "excited": "1F929",
    "sad": "1F622",
    "empathy": "1FAE6",
    "confused": "1F615",
    "surprised": "1F632",
    "love": "2764",
    "cool": "1F60E",
    "greeting": "1F44B",
    "neutral": "1F610",
    "curious": "1F914",
    "worried": "1F62C",
    "playful": "1F61C",
}

_OPENMOJI_CDN = (
    "https://cdn.jsdelivr.net/gh/hfg-gmuend/openmoji@latest/color/svg/{code}.svg"
)

CHUNK_SIZE = 4096  # bytes por frame de audio


# ── Helpers ───────────────────────────────────────────────────────────────────


def emoji_url(code: str) -> str:
    return _OPENMOJI_CDN.format(code=code.upper())


def emotion_img_html(emotion: str, size: int = 64) -> str:
    code = EMOTION_OPENMOJI.get(emotion, EMOTION_OPENMOJI["neutral"])
    url = emoji_url(code)
    return (
        f'<img src="{url}" width="{size}" height="{size}" '
        f'title="{emotion}" style="vertical-align:middle; margin-right:8px;">'
    )


def new_request_id() -> str:
    return str(uuid.uuid4())


# ── Gestión de sesión WebSocket ───────────────────────────────────────────────


def _init_session() -> None:
    """Inicializa las claves de session_state si aún no existen."""
    defaults = {
        "ws": None,  # WebSocket connection object (sync)
        "session_id": None,  # session_id recibido en auth_ok
        "connected": False,
        "history": [],  # lista de dict {role, content, emotion, latency_ms, meta}
        "last_emotion": "neutral",
        "last_latency_ms": None,
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v


def connect(url: str, api_key: str) -> tuple[bool, str]:
    """
    Establece conexión WebSocket y realiza el handshake de autenticación.
    Devuelve (ok: bool, mensaje: str).
    """
    try:
        ws = ws_connect(url, open_timeout=10)
        # Enviar auth
        ws.send(json.dumps({"type": "auth", "api_key": api_key}))
        raw = ws.recv(timeout=10)
        msg = json.loads(raw)
        if msg.get("type") == "auth_ok":
            st.session_state.ws = ws
            st.session_state.session_id = msg["session_id"]
            st.session_state.connected = True
            return True, f"Conectado · session_id: {msg['session_id']}"
        else:
            ws.close()
            return False, f"Auth rechazada: {msg}"
    except Exception as exc:
        return False, f"Error de conexión: {exc}"


def disconnect() -> None:
    """Cierra la conexión WebSocket y limpia el estado."""
    if st.session_state.ws is not None:
        try:
            st.session_state.ws.close()
        except Exception:
            pass
    st.session_state.ws = None
    st.session_state.session_id = None
    st.session_state.connected = False


def _receive_interaction(
    ws,
    user_id: str,
    request_id: str,
    text_placeholder,
    status_placeholder,
) -> dict:
    """
    Bucle de recepción de mensajes hasta stream_end.
    Actualiza los placeholders de Streamlit en tiempo real.

    Devuelve un dict con todos los datos de la interacción.
    """
    emotion = "neutral"
    full_text = ""
    meta: dict | None = None
    latency_ms: int | None = None
    start_ts = time.monotonic()

    while True:
        try:
            raw = ws.recv(timeout=60)
        except TimeoutError:
            status_placeholder.error("⏱️ Timeout esperando respuesta del backend.")
            break

        msg = json.loads(raw)
        mtype = msg.get("type", "")

        if mtype == "emotion":
            emotion = msg.get("emotion", "neutral")
            code = EMOTION_OPENMOJI.get(emotion, EMOTION_OPENMOJI["neutral"])
            status_placeholder.markdown(
                f"{emotion_img_html(emotion, 48)} **{emotion}**",
                unsafe_allow_html=True,
            )

        elif mtype == "text_chunk":
            full_text += msg.get("text", "")
            text_placeholder.markdown(full_text)

        elif mtype == "response_meta":
            meta = msg

        elif mtype == "stream_end":
            latency_ms = int((time.monotonic() - start_ts) * 1000)
            claimed_ms = msg.get("processing_time_ms", latency_ms)
            status_placeholder.markdown(
                f"{emotion_img_html(emotion, 48)} **{emotion}** · ⏱️ {claimed_ms} ms",
                unsafe_allow_html=True,
            )
            break

        elif mtype == "error":
            error_msg = msg.get("message", "Error desconocido")
            code = msg.get("error_code", "?")
            status_placeholder.error(f"❌ [{code}] {error_msg}")
            break

    return {
        "emotion": emotion,
        "text": full_text,
        "meta": meta,
        "latency_ms": latency_ms,
        "request_id": request_id,
    }


def send_text_interaction(
    user_input: str,
    user_id: str,
    text_placeholder,
    status_placeholder,
) -> dict | None:
    """Envía una interacción de texto y recibe la respuesta en streaming."""
    ws = st.session_state.ws
    request_id = new_request_id()

    try:
        # interaction_start
        ws.send(
            json.dumps(
                {
                    "type": "interaction_start",
                    "user_id": user_id,
                    "request_id": request_id,
                }
            )
        )
        # text message
        ws.send(
            json.dumps(
                {
                    "type": "text",
                    "content": user_input,
                    "request_id": request_id,
                }
            )
        )
        return _receive_interaction(
            ws, user_id, request_id, text_placeholder, status_placeholder
        )
    except Exception as exc:
        status_placeholder.error(f"Error enviando mensaje: {exc}")
        st.session_state.connected = False
        return None


def send_audio_interaction(
    audio_bytes: bytes,
    user_id: str,
    text_placeholder,
    status_placeholder,
) -> dict | None:
    """Envía audio como frames binarios seguido de audio_end, recibe respuesta."""
    ws = st.session_state.ws
    request_id = new_request_id()

    try:
        # interaction_start
        ws.send(
            json.dumps(
                {
                    "type": "interaction_start",
                    "user_id": user_id,
                    "request_id": request_id,
                }
            )
        )
        # enviar audio en chunks binarios
        total = len(audio_bytes)
        sent = 0
        while sent < total:
            chunk = audio_bytes[sent : sent + CHUNK_SIZE]
            ws.send(chunk)  # frame binario
            sent += len(chunk)

        # audio_end — señal de fin de audio
        ws.send(
            json.dumps(
                {
                    "type": "audio_end",
                    "request_id": request_id,
                }
            )
        )
        return _receive_interaction(
            ws, user_id, request_id, text_placeholder, status_placeholder
        )
    except Exception as exc:
        status_placeholder.error(f"Error enviando audio: {exc}")
        st.session_state.connected = False
        return None


# ── REST helpers ──────────────────────────────────────────────────────────────


def rest_get(base_url: str, path: str, api_key: str) -> tuple[int, dict]:
    """Realiza GET a la API REST y devuelve (status_code, json_body)."""
    try:
        r = requests.get(
            f"{base_url}{path}",
            headers={"X-API-Key": api_key},
            timeout=10,
            verify=False,  # certs autofirmados en dev
        )
        try:
            body = r.json()
        except Exception:
            body = {"raw": r.text}
        return r.status_code, body
    except Exception as exc:
        return 0, {"error": str(exc)}


# ── Configurar página ─────────────────────────────────────────────────────────

st.set_page_config(
    page_title="Robi Simulator",
    page_icon="🤖",
    layout="wide",
    initial_sidebar_state="expanded",
)

_init_session()

# ── Sidebar ───────────────────────────────────────────────────────────────────

with st.sidebar:
    st.title("🤖 Robi Simulator")
    st.caption("Herramienta de prueba para el backend de Robi sin Android.")
    st.divider()

    st.subheader("⚙️ Configuración")
    backend_url = st.text_input(
        "URL WebSocket",
        value="ws://localhost:8000/ws/interact",
        help="ws:// o wss:// — usa ws:// para desarrollo local",
    )
    # Derivar la URL REST base de la URL WS
    rest_base = backend_url.replace("ws://", "http://").replace("wss://", "https://")
    rest_base = rest_base.rsplit("/ws/", 1)[0]

    api_key = st.text_input(
        "API Key",
        type="password",
        value="",
        help="Valor de API_KEY en tu .env",
    )

    # Intentar leer API_KEY del .env local para comodidad en desarrollo
    if not api_key:
        env_file = Path(__file__).parent.parent.parent / ".env"
        if env_file.exists():
            for line in env_file.read_text().splitlines():
                if line.startswith("API_KEY=") and not line.startswith(
                    "API_KEY=change"
                ):
                    api_key = line.split("=", 1)[1].strip()
                    break

    st.divider()
    st.subheader("👤 Usuario")
    user_id_options = ["unknown", "user_juan", "user_maria", "user_pedro"]
    user_id = st.selectbox(
        "user_id",
        options=user_id_options,
        index=0,
        help="'unknown' = usuario no identificado",
    )
    custom_user = st.text_input("... o escribe un user_id personalizado", value="")
    if custom_user.strip():
        user_id = custom_user.strip()

    st.divider()
    st.subheader("🔌 Conexión")

    if not st.session_state.connected:
        if st.button("Conectar", type="primary", use_container_width=True):
            if not api_key:
                st.error("Ingresa la API Key primero.")
            else:
                with st.spinner("Conectando..."):
                    ok, msg = connect(backend_url, api_key)
                if ok:
                    st.success(msg)
                    st.rerun()
                else:
                    st.error(msg)
    else:
        st.success(f"✅ Conectado\n\n`{st.session_state.session_id}`")
        if st.button("Desconectar", use_container_width=True):
            disconnect()
            st.rerun()

    st.divider()
    st.subheader("🔧 REST")
    if st.button("GET /api/health", use_container_width=True):
        code, body = rest_get(rest_base, "/api/health", api_key)
        if code == 200:
            st.success(f"**{code}** — {body}")
        else:
            st.error(f"**{code}** — {body}")

    rest_user_id = st.text_input("user_id para memoria", value=user_id, key="rest_uid")
    if st.button("GET /api/users/{{id}}/memory", use_container_width=True):
        code, body = rest_get(rest_base, f"/api/users/{rest_user_id}/memory", api_key)
        if code == 200:
            st.json(body)
        else:
            st.error(f"**{code}** — {body}")

# ── Layout principal ──────────────────────────────────────────────────────────

main_col, history_col = st.columns([3, 2], gap="large")

# ── Columna principal: interacción ────────────────────────────────────────────

with main_col:
    st.header("💬 Interacción")

    # ── Texto ──────────────────────────────────────────────────────────────
    with st.form("text_form", clear_on_submit=True):
        user_text = st.text_area(
            "Mensaje de texto",
            placeholder="Escribe lo que diría el usuario (simula texto / voz procesada)…",
            height=80,
            label_visibility="collapsed",
        )
        col_send, col_hint = st.columns([1, 3])
        with col_send:
            submitted_text = st.form_submit_button(
                "Enviar texto ▶",
                type="primary",
                use_container_width=True,
                disabled=not st.session_state.connected,
            )
        with col_hint:
            if not st.session_state.connected:
                st.caption("⚠️ Conecta primero desde el panel lateral.")

    if submitted_text and user_text.strip():
        st.divider()
        st.subheader("🎭 Respuesta")
        status_ph = st.empty()
        text_ph = st.empty()

        with st.spinner("Esperando respuesta…"):
            result = send_text_interaction(
                user_text.strip(), user_id, text_ph, status_ph
            )

        if result:
            st.session_state.last_emotion = result["emotion"]
            st.session_state.last_latency_ms = result["latency_ms"]
            st.session_state.history.append(
                {
                    "role": "user",
                    "content": user_text.strip(),
                    "emotion": None,
                    "latency_ms": None,
                    "meta": None,
                }
            )
            st.session_state.history.append(
                {
                    "role": "assistant",
                    "content": result["text"],
                    "emotion": result["emotion"],
                    "latency_ms": result["latency_ms"],
                    "meta": result["meta"],
                }
            )
            if result["meta"]:
                with st.expander("📦 response_meta"):
                    st.json(result["meta"])

    st.divider()

    # ── Audio ──────────────────────────────────────────────────────────────
    st.subheader("🎙️ Enviar audio")
    tab_record, tab_upload = st.tabs(["🎤 Grabar", "📁 Subir archivo"])

    # Variables que comparten ambas pestañas
    _audio_bytes: bytes | None = None
    _audio_label: str = "[audio]"

    with tab_record:
        recorded = st.audio_input(
            "Graba tu mensaje de voz",
            help="Haz clic en el micrófono para iniciar/detener la grabación.",
        )
        if recorded is not None:
            _audio_bytes = recorded.read()
            _audio_label = "[audio: grabación]"
            st.caption(f"Grabación lista · {len(_audio_bytes):,} bytes")
            if st.button(
                "Enviar grabación ▶",
                type="primary",
                key="btn_send_record",
                disabled=not st.session_state.connected,
            ):
                st.divider()
                st.subheader("🎭 Respuesta (audio grabado)")
                status_ph_rec = st.empty()
                text_ph_rec = st.empty()

                with st.spinner("Enviando grabación y esperando respuesta…"):
                    result_rec = send_audio_interaction(
                        _audio_bytes, user_id, text_ph_rec, status_ph_rec
                    )

                if result_rec:
                    st.session_state.last_emotion = result_rec["emotion"]
                    st.session_state.last_latency_ms = result_rec["latency_ms"]
                    st.session_state.history.append(
                        {
                            "role": "user",
                            "content": _audio_label,
                            "emotion": None,
                            "latency_ms": None,
                            "meta": None,
                        }
                    )
                    st.session_state.history.append(
                        {
                            "role": "assistant",
                            "content": result_rec["text"],
                            "emotion": result_rec["emotion"],
                            "latency_ms": result_rec["latency_ms"],
                            "meta": result_rec["meta"],
                        }
                    )
                    if result_rec["meta"]:
                        with st.expander("📦 response_meta"):
                            st.json(result_rec["meta"])

    with tab_upload:
        audio_file = st.file_uploader(
            "Sube un archivo .wav o .aac",
            type=["wav", "aac", "mp3", "ogg"],
            help="El audio se enviará como frames binarios a través del WebSocket.",
        )
        if audio_file is not None:
            _audio_bytes = audio_file.read()
            _audio_label = f"[audio: {audio_file.name}]"
            st.audio(audio_file, format=audio_file.type)
            st.caption(f"Tamaño: {len(_audio_bytes):,} bytes · MIME: {audio_file.type}")

            if st.button(
                "Enviar archivo ▶",
                type="primary",
                key="btn_send_upload",
                disabled=not st.session_state.connected,
            ):
                st.divider()
                st.subheader("🎭 Respuesta (audio)")
                status_ph2 = st.empty()
                text_ph2 = st.empty()

                with st.spinner("Enviando audio y esperando respuesta…"):
                    result2 = send_audio_interaction(
                        _audio_bytes, user_id, text_ph2, status_ph2
                    )

                if result2:
                    st.session_state.last_emotion = result2["emotion"]
                    st.session_state.last_latency_ms = result2["latency_ms"]
                    st.session_state.history.append(
                        {
                            "role": "user",
                            "content": _audio_label,
                            "emotion": None,
                            "latency_ms": None,
                            "meta": None,
                        }
                    )
                    st.session_state.history.append(
                        {
                            "role": "assistant",
                            "content": result2["text"],
                            "emotion": result2["emotion"],
                            "latency_ms": result2["latency_ms"],
                            "meta": result2["meta"],
                        }
                    )
                    if result2["meta"]:
                        with st.expander("📦 response_meta"):
                            st.json(result2["meta"])

    # ── Estado actual ──────────────────────────────────────────────────────
    if st.session_state.last_latency_ms is not None:
        st.divider()
        emotion_now = st.session_state.last_emotion
        code_now = EMOTION_OPENMOJI.get(emotion_now, EMOTION_OPENMOJI["neutral"])
        st.markdown(
            f"**Última emoción:** {emotion_img_html(emotion_now, 40)} `{emotion_now}` "
            f"· **Latencia:** `{st.session_state.last_latency_ms} ms`",
            unsafe_allow_html=True,
        )
        st.image(emoji_url(code_now), width=100)

# ── Columna de historial ──────────────────────────────────────────────────────

with history_col:
    st.header("📜 Historial de sesión")

    hist = st.session_state.history
    if not hist:
        st.info("El historial aparecerá aquí después de la primera interacción.")
    else:
        # Mostrar del más reciente al más antiguo
        for entry in reversed(hist):
            role = entry["role"]
            content = entry["content"]
            emotion = entry.get("emotion")
            latency = entry.get("latency_ms")

            if role == "user":
                with st.chat_message("user"):
                    st.write(content)
            else:
                with st.chat_message("assistant"):
                    # Cabecera con emoción y latencia
                    if emotion:
                        badge_html = f"{emotion_img_html(emotion, 24)} `{emotion}`"
                        if latency is not None:
                            badge_html += f" · ⏱️ `{latency} ms`"
                        st.markdown(badge_html, unsafe_allow_html=True)
                    st.write(content)

        if st.button("🗑️ Limpiar historial", use_container_width=True):
            st.session_state.history = []
            st.rerun()
