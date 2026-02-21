"""
tests/streamlit_simulator/app.py — Simulador Streamlit para Robi Backend

Uso:
    uv run streamlit run tests/streamlit_simulator/app.py

Requiere el backend corriendo:
    uv run uvicorn main:app --reload --ws wsproto
"""

import base64
import json
import time
import uuid
from pathlib import Path

import requests
import streamlit as st
from websockets.sync.client import connect as ws_connect

# ── Constantes ────────────────────────────────────────────────────────────────

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
_OPENMOJI_SVG = "https://openmoji.org/data/color/svg/{code}.svg"


# ── Helpers ───────────────────────────────────────────────────────────────────


def emoji_url(code: str) -> str:
    return _OPENMOJI_CDN.format(code=code.upper())


def emotion_img_html(emotion: str, size: int = 64) -> str:
    code = EMOTION_OPENMOJI.get(emotion, EMOTION_OPENMOJI["neutral"])
    return (
        f'<img src="{emoji_url(code)}" width="{size}" height="{size}" '
        f'title="{emotion}" style="vertical-align:middle; margin-right:8px;">'
    )


def emoji_row_html(codes: list[str], size: int = 40) -> str:
    imgs = "".join(
        f'<img src="{_OPENMOJI_SVG.format(code=c.upper())}" '
        f'width="{size}" height="{size}" title="{c}" style="margin-right:4px;">'
        for c in codes
    )
    return f'<div style="display:flex;flex-wrap:wrap;gap:2px;align-items:center;">{imgs}</div>'


# ── Session state ─────────────────────────────────────────────────────────────


def _init_session() -> None:
    defaults = {
        "ws": None,
        "session_id": None,
        "connected": False,
        "history": [],
        "last_result": None,  # dict con la última respuesta; persiste tras st.rerun()
        "camera_on": False,
        "video_mode": "foto",
        # Contadores para resetear widgets (incrementar = nuevo widget vacío)
        "text_gen": 0,
        "audio_gen": 0,
        "photo_gen": 0,
        "video_gen": 0,
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v


# ── WebSocket ─────────────────────────────────────────────────────────────────


def ws_connect_auth(url: str, api_key: str) -> tuple[bool, str]:
    try:
        ws = ws_connect(url, open_timeout=10)
        ws.send(json.dumps({"type": "auth", "api_key": api_key}))
        msg = json.loads(ws.recv(timeout=10))
        if msg.get("type") == "auth_ok":
            st.session_state.ws = ws
            st.session_state.session_id = msg["session_id"]
            st.session_state.connected = True
            return True, f"Conectado · session_id: {msg['session_id']}"
        ws.close()
        return False, f"Auth rechazada: {msg}"
    except Exception as exc:
        return False, f"Error de conexión: {exc}"


def ws_disconnect() -> None:
    if st.session_state.ws:
        try:
            st.session_state.ws.close()
        except Exception:
            pass
    st.session_state.ws = None
    st.session_state.session_id = None
    st.session_state.connected = False


def ws_send_and_receive(
    user_text: str | None,
    audio_bytes: bytes | None,
    video_bytes: bytes | None,
    user_id: str,
) -> dict:
    """Envía todos los contenidos en un único mensaje multimodal y recibe la respuesta."""
    ws = st.session_state.ws
    request_id = str(uuid.uuid4())
    start_ts = time.monotonic()

    ws.send(
        json.dumps(
            {"type": "interaction_start", "user_id": user_id, "request_id": request_id}
        )
    )

    # Construir mensaje multimodal con todas las modalidades disponibles de una vez
    vid_mode = st.session_state.get("video_mode", "foto")
    payload: dict = {"type": "multimodal", "request_id": request_id}
    if user_text:
        payload["text"] = user_text
    if audio_bytes:
        payload["audio"] = base64.b64encode(audio_bytes).decode()
        payload["audio_mime"] = "audio/webm"
    if video_bytes:
        if vid_mode == "foto":
            payload["image"] = base64.b64encode(video_bytes).decode()
            payload["image_mime"] = "image/jpeg"
        else:
            payload["video"] = base64.b64encode(video_bytes).decode()
            payload["video_mime"] = "video/mp4"
    ws.send(json.dumps(payload))

    # Recibir — actualizar placeholders en tiempo real mientras llegan los chunks
    emotion = "neutral"
    full_text = ""
    meta = None
    latency_ms = None
    emotion_latency_ms = None
    first_chunk_latency_ms = None
    error = None
    chunks: list[dict] = []

    status_ph = st.empty()
    text_ph = st.empty()

    while True:
        try:
            raw = ws.recv(timeout=60)
        except TimeoutError:
            error = "⏱️ Timeout esperando respuesta del backend."
            break

        msg = json.loads(raw)
        mtype = msg.get("type", "")

        chunk_ts = int((time.monotonic() - start_ts) * 1000)
        chunks.append({"ts_ms": chunk_ts, **msg})

        if mtype == "emotion":
            emotion = msg.get("emotion", "neutral")
            emotion_latency_ms = chunk_ts
            status_ph.markdown(
                f"{emotion_img_html(emotion, 48)} **{emotion}** · ⏱️ {emotion_latency_ms} ms",
                unsafe_allow_html=True,
            )
        elif mtype == "text_chunk":
            if first_chunk_latency_ms is None:
                first_chunk_latency_ms = chunk_ts
            full_text += msg.get("text", "")
            text_ph.markdown(full_text)
        elif mtype == "response_meta":
            meta = msg
        elif mtype == "stream_end":
            latency_ms = int((time.monotonic() - start_ts) * 1000)
            break
        elif mtype == "error":
            error = f"❌ [{msg.get('error_code', '?')}] {msg.get('message', 'Error desconocido')}"
            break

    return {
        "emotion": emotion,
        "text": full_text,
        "meta": meta,
        "latency_ms": latency_ms,
        "emotion_latency_ms": emotion_latency_ms,
        "first_chunk_latency_ms": first_chunk_latency_ms,
        "error": error,
        "chunks": chunks,
    }


# ── REST ──────────────────────────────────────────────────────────────────────


def rest_get(base_url: str, path: str, api_key: str) -> tuple[int, dict]:
    try:
        r = requests.get(
            f"{base_url}{path}",
            headers={"X-API-Key": api_key},
            timeout=10,
            verify=False,
        )
        try:
            return r.status_code, r.json()
        except Exception:
            return r.status_code, {"raw": r.text}
    except Exception as exc:
        return 0, {"error": str(exc)}


# ── App ───────────────────────────────────────────────────────────────────────

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
        "URL WebSocket", value="ws://localhost:8000/ws/interact"
    )
    rest_base = (
        backend_url.replace("ws://", "http://")
        .replace("wss://", "https://")
        .rsplit("/ws/", 1)[0]
    )

    api_key = st.text_input("API Key", type="password", value="")
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
    user_id = st.selectbox(
        "user_id", ["unknown", "user_juan", "user_maria", "user_pedro"]
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
                    ok, msg = ws_connect_auth(backend_url, api_key)
                if ok:
                    st.success(msg)
                    st.rerun()
                else:
                    st.error(msg)
    else:
        st.success(f"✅ Conectado\n\n`{st.session_state.session_id}`")
        if st.button("Desconectar", use_container_width=True):
            ws_disconnect()
            st.rerun()

    st.divider()
    st.subheader("🔧 REST")
    if st.button("GET /api/health", use_container_width=True):
        code, body = rest_get(rest_base, "/api/health", api_key)
        (st.success if code == 200 else st.error)(f"**{code}** — {body}")

    rest_uid = st.text_input("user_id para memoria", value=user_id, key="rest_uid")
    if st.button("GET /api/users/{id}/memory", use_container_width=True):
        code, body = rest_get(rest_base, f"/api/users/{rest_uid}/memory", api_key)
        if code == 200:
            st.json(body)
        else:
            st.error(f"**{code}** — {body}")

# ── Layout ────────────────────────────────────────────────────────────────────

main_col, history_col = st.columns([3, 2], gap="large")

with main_col:
    st.header("💬 Interacción")

    # ── Inputs (claves dinámicas — incrementar el contador resetea el widget) ──
    tab_text, tab_audio, tab_video = st.tabs(["📝 Texto", "🎙️ Audio", "📹 Video"])

    with tab_text:
        st.text_area(
            "Mensaje de texto",
            placeholder="Escribe lo que diría el usuario…",
            height=120,
            key=f"input_text_{st.session_state.text_gen}",
            label_visibility="collapsed",
        )

    with tab_audio:
        st.audio_input(
            "Graba tu mensaje de voz", key=f"input_audio_{st.session_state.audio_gen}"
        )

    with tab_video:
        if st.button(
            "🔴 Desactivar cámara"
            if st.session_state.camera_on
            else "📷 Activar cámara",
            key="btn_toggle_camera",
        ):
            st.session_state.camera_on = not st.session_state.camera_on
            if not st.session_state.camera_on:
                st.session_state.photo_gen += 1
                st.session_state.video_gen += 1
            st.rerun()

        if st.session_state.camera_on:
            mode = st.radio(
                "Modo",
                ["foto", "video"],
                format_func=lambda m: (
                    "📷 Foto" if m == "foto" else "🎬 Video (archivo)"
                ),
                horizontal=True,
                key="video_mode",
                label_visibility="collapsed",
            )
            if mode == "foto":
                st.camera_input(
                    "Captura", key=f"input_photo_{st.session_state.photo_gen}"
                )
            else:
                st.file_uploader(
                    "Subir video",
                    type=["mp4", "webm", "mov", "avi"],
                    key=f"input_video_{st.session_state.video_gen}",
                )
        else:
            st.info("Activa la cámara para capturar una foto o subir un video.")

    st.divider()

    # ── Botón enviar ──────────────────────────────────────────────────────
    col_send, col_hint = st.columns([1, 3])
    with col_send:
        do_send = st.button(
            "Enviar ▶",
            type="primary",
            use_container_width=True,
            disabled=not st.session_state.connected,
        )
    with col_hint:
        if not st.session_state.connected:
            st.caption("⚠️ Conecta primero desde el panel lateral.")

    if do_send:
        # Leer valores actuales de los widgets
        text_val: str = st.session_state.get(
            f"input_text_{st.session_state.text_gen}", ""
        ).strip()

        audio_f = st.session_state.get(f"input_audio_{st.session_state.audio_gen}")
        audio_bytes: bytes | None = None
        if audio_f is not None:
            audio_f.seek(0)
            audio_bytes = audio_f.read()

        video_bytes: bytes | None = None
        video_label = ""
        vid_mode = st.session_state.get("video_mode", "foto")
        if vid_mode == "foto":
            photo_f = st.session_state.get(f"input_photo_{st.session_state.photo_gen}")
            if photo_f is not None:
                photo_f.seek(0)
                video_bytes = photo_f.read()
                video_label = "[foto]"
        else:
            vid_f = st.session_state.get(f"input_video_{st.session_state.video_gen}")
            if vid_f is not None:
                vid_f.seek(0)
                video_bytes = vid_f.read()
                video_label = f"[video: {vid_f.name}]"

        if not text_val and audio_bytes is None and video_bytes is None:
            st.warning("⚠️ Completa al menos un campo antes de enviar.")
        else:
            parts = (
                ([text_val] if text_val else [])
                + (["[audio]"] if audio_bytes else [])
                + ([video_label or "[video]"] if video_bytes else [])
            )
            user_label = " + ".join(parts)

            with st.spinner("Esperando respuesta…"):
                try:
                    result = ws_send_and_receive(
                        text_val or None, audio_bytes, video_bytes, user_id
                    )
                except Exception as exc:
                    st.session_state.connected = False
                    result = {
                        "error": str(exc),
                        "emotion": "neutral",
                        "text": "",
                        "meta": None,
                        "latency_ms": None,
                        "emotion_latency_ms": None,
                        "first_chunk_latency_ms": None,
                        "chunks": [],
                    }

            # Guardar resultado en session_state (persiste tras rerun)
            st.session_state.last_result = result
            st.session_state.history.append({"role": "user", "content": user_label})
            st.session_state.history.append(
                {
                    "role": "assistant",
                    "content": result["text"],
                    "emotion": result["emotion"],
                    "latency_ms": result["latency_ms"],
                    "meta": result["meta"],
                }
            )
            # Incrementar contadores → los widgets aparecen vacíos en el próximo render
            st.session_state.text_gen += 1
            st.session_state.audio_gen += 1
            st.session_state.photo_gen += 1
            st.session_state.video_gen += 1
            st.rerun()

    # ── Última respuesta (renderizada desde session_state, sobrevive al rerun) ─
    result = st.session_state.last_result
    if result:
        st.divider()
        st.subheader("🎭 Última respuesta")

        if result.get("error"):
            st.error(result["error"])
        else:
            emotion = result["emotion"]
            elat = result.get("emotion_latency_ms")
            st.markdown(
                f"{emotion_img_html(emotion, 48)} **{emotion}"
                + (f"** · ⏱️ {elat} ms" if elat else "**"),
                unsafe_allow_html=True,
            )

            fclat = result.get("first_chunk_latency_ms")
            st.markdown(
                result["text"]
                + (
                    f"\n\n<small style='color:gray;'>⚡ primer chunk: {fclat} ms</small>"
                    if fclat
                    else ""
                ),
                unsafe_allow_html=True,
            )

            meta = result.get("meta")
            if meta:
                codes = (meta.get("expression") or {}).get("emojis", [])
                if codes:
                    st.markdown(
                        f"**Emojis:** {emoji_row_html(codes, 36)}",
                        unsafe_allow_html=True,
                    )
                with st.expander("📦 response_meta"):
                    st.json(meta)

            chunks = result.get("chunks") or []
            with st.expander(f"🐛 Debug — chunks WS ({len(chunks)})"):
                for i, chunk in enumerate(chunks):
                    st.markdown(
                        f"**#{i + 1}** `{chunk.get('type', '?')}` · `{chunk.get('ts_ms', '?')} ms`"
                    )
                    st.json(chunk)

            if result.get("latency_ms") is not None:
                st.caption(f"⏱️ Latencia total: {result['latency_ms']} ms")

# ── Historial ─────────────────────────────────────────────────────────────────

with history_col:
    st.header("📜 Historial")

    hist = st.session_state.history
    if not hist:
        st.info("El historial aparecerá aquí después de la primera interacción.")
    else:
        for entry in reversed(hist):
            if entry["role"] == "user":
                with st.chat_message("user"):
                    st.write(entry["content"])
            else:
                with st.chat_message("assistant"):
                    emotion = entry.get("emotion")
                    latency = entry.get("latency_ms")
                    if emotion:
                        badge = f"{emotion_img_html(emotion, 24)} `{emotion}`"
                        if latency is not None:
                            badge += f" · ⏱️ `{latency} ms`"
                        st.markdown(badge, unsafe_allow_html=True)
                    meta = entry.get("meta")
                    if meta:
                        codes = (meta.get("expression") or {}).get("emojis", [])
                        if codes:
                            st.markdown(
                                emoji_row_html(codes, 32), unsafe_allow_html=True
                            )
                    st.write(entry["content"])

        if st.button("🗑️ Limpiar historial", use_container_width=True):
            st.session_state.history = []
            st.session_state.last_result = None
            st.rerun()
