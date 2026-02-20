## Plan: Backend Python — Robi Robot

El backend se implementa en **Python 3.12 + uv**, con FastAPI, WebSocket streaming, LangChain DeepAgents + Gemini Flash Lite, SQLite asíncrono y Docker Compose (FastAPI + Nginx). La implementación es **incremental**: cada capa tiene sus propias pruebas unitarias antes de avanzar a la siguiente. La carpeta de destino es `/home/mike/Code/robi/robi_backend/`.

---

**Pasos**

### 0 — Scaffolding del proyecto
1. Ejecutar `uv init` en la raíz del workspace para crear `pyproject.toml` (Python 3.12, sin `requirements.txt`)
2. Crear la estructura de carpetas completa definida en §3.2: `websockets/`, `routers/`, `services/`, `repositories/`, `models/`, `middleware/`, `utils/`, `tests/unit/`, `tests/integration/`, `tests/streamlit_simulator/`, `data/`, `media/uploads/`, `media/logs/`, `nginx/certs/`
3. Añadir todas las dependencias al `pyproject.toml` mediante `uv add`:
   - `fastapi`, `uvicorn[standard]`, `websockets`
   - `deepagents`, `langchain-google-genai`, `langgraph`, `google-generativeai`
   - `sqlalchemy[asyncio]`, `aiosqlite`
   - `python-dotenv`, `pydantic-settings`
   - `structlog`
   - `pytest`, `pytest-asyncio`, `httpx` (tests)
   - `streamlit` (simulador)
4. Crear [.env.example](.env.example) con todas las variables de §3.8 documentadas
5. Crear [config.py](config.py) usando `pydantic-settings` con `Settings` cargado desde `.env`; tipos modernos (`str | None`, no `Optional[str]`)

---

### 1 — Modelos + Base de Datos
*(iteración 1: solo datos, sin lógica)*

6. Crear [models/entities.py](models/entities.py): dataclasses de dominio `User`, `Memory`, `Interaction`, `ConversationMessage` con tipos nativos (`list[str]`, `dict[str, Any]`, `str | None`)
7. Crear [models/requests.py](models/requests.py) y [models/responses.py](models/responses.py): modelos Pydantic para REST; sin `from typing import`
8. Crear [models/ws_messages.py](models/ws_messages.py): `Literal` union discriminada para todos los tipos de mensajes WS (§3.3)
9. Crear las tablas SQLAlchemy async (`users`, `memories`, `interactions`, `conversation_history`) en un módulo `db.py` o similar
10. **Tests unitarios**: verificar creación de tablas, constraints y serialización de modelos Pydantic → `tests/unit/test_models.py`

---

### 2 — Middleware e Infraestructura HTTP
*(iteración 2: app arranca y tiene health check)*

11. Crear [middleware/auth.py](middleware/auth.py): validación API Key en header `X-API-Key` con comparación constant-time (`secrets.compare_digest`)
12. Crear [middleware/error_handler.py](middleware/error_handler.py): handler global que convierte excepciones al formato de error de §3.9 (campo `error_code`, `recoverable`, `retry_after`)
13. Crear [middleware/logging.py](middleware/logging.py): middleware `structlog` que registra cada request con `request_id` en JSON
14. Crear [routers/health.py](routers/health.py): `GET /api/health` → `{"status": "ok", "version": "1.4"}`
15. Crear [main.py](main.py): instancia `FastAPI`, registra middlewares, routers; arranca con `uvicorn`
16. **Tests unitarios**: mock de API Key válida/inválida, formato de error, health endpoint → `tests/unit/test_middleware.py`

---

### 3 — Repositorios (CRUD)
*(iteración 3: acceso a datos testeable independientemente)*

17. Crear [repositories/users.py](repositories/users.py): `UserRepository` con métodos async `create`, `get_by_id`, `get_by_user_id`, `update_last_seen`, `list_all`; `face_embedding` como `bytes | None`
18. Crear [repositories/memory.py](repositories/memory.py): `MemoryRepository` con `save`, `get_for_user`, `delete`; incluye el **filtro de privacidad** (comparación de palabras clave; el filtro real con Gemini se añade en la iteración 5) y `get_recent_important(user_id, min_importance=5, limit=5)` siguiendo el diagrama §3.6
19. Crear [repositories/media.py](repositories/media.py): guardar/borrar archivos en `media/uploads/`, limpiar archivos con más de 24h (audio) y 1h (imágenes/video)
20. **Tests unitarios** con SQLite in-memory: CRUD de usuarios, memorias y filtro de privacidad básico → `tests/unit/test_repositories.py`

---

### 4 — REST endpoints de Usuarios y Memoria
*(iteración 4: API REST completa)*

21. Crear [routers/users.py](routers/users.py): `GET /api/users`, `GET /api/users/{user_id}`, `DELETE /api/users/{user_id}/memory`
22. Crear [routers/memory.py](routers/memory.py): `POST /api/users/{user_id}/memory`, `GET /api/users/{user_id}/memory`
23. Añadir `POST /api/face/register` para recibir `user_id`, `name`, `embedding` (bytes base64) desde Android y delegarlo a `UserRepository`
24. **Tests unitarios**: endpoints con `httpx.AsyncClient`, mock de repositorios → `tests/unit/test_routers.py`

---

### 5 — Servicios de IA
*(iteración 5: el núcleo inteligente, en capas)*

25. Crear [services/gemini.py](services/gemini.py): instancia singleton de `ChatGoogleGenerativeAI` (`gemini-2.0-flash-lite`, `streaming=True`); función `get_model() -> ChatGoogleGenerativeAI`
26. Crear [services/expression.py](services/expression.py): `parse_emotion_tag(text: str) -> tuple[str, str]` que extrae `[emotion:TAG]` del inicio del stream y retorna `(tag, remaining_text)`; mapeo de tags a códigos Unicode de OpenMoji (§3.7)
27. Crear [services/movement.py](services/movement.py): `build_move_sequence(description: str, steps: list[dict]) -> dict` que calcula `total_duration_ms` sumando los `duration_ms` de cada step
28. Crear [services/history.py](services/history.py): `ConversationHistory` con:
    - `add_message(session_id, role, content)` async
    - `get_history(session_id) -> list[dict]`
    - `compact_if_needed(session_id)` — si `len >= 20`, compacta msgs 1-15 con Gemini como background task (`asyncio.create_task`)
29. Crear [services/intent.py](services/intent.py): `classify_intent(response_text: str) -> str | None` que detecta si la respuesta implica `photo_request`, `video_request` o `None`; basado en palabras clave del texto generado por Gemini
30. Crear [services/agent.py](services/agent.py): `create_agent()` con `deepagents.create_deep_agent(model=..., tools=[], system_prompt=SYSTEM_PROMPT)` donde `SYSTEM_PROMPT` es el prompt TTS-safe de §3.7; `run_agent_stream(session_id, user_id, input, history) -> AsyncIterator[str]`
31. **Tests unitarios**: `parse_emotion_tag` con varios formatos, `build_move_sequence`, `history` con compactación mockeada, `classify_intent` → `tests/unit/test_services.py`

---

### 6 — WebSocket Handler (canal principal)
*(iteración 6: flujo completo de streaming)*

32. Crear [websockets/protocol.py](websockets/protocol.py): constantes y funciones helper para construir/serializar mensajes `auth_ok`, `emotion`, `text_chunk`, `capture_request`, `response_meta`, `stream_end`, `error`
33. Crear [websockets/auth.py](websockets/auth.py): `authenticate_websocket(ws, timeout=10s) -> str | None` — espera primer mensaje `{"type":"auth","api_key":...}`, valida con constant-time compare, devuelve `session_id` o cierra la conexión
34. Crear [websockets/streaming.py](websockets/streaming.py): handler principal `ws_interact(websocket)` que implementa el flujo completo de §3.4:
    - Acumula binarios de audio en buffer hasta `audio_end`
    - Carga memoria del usuario (top 5 por importancia)
    - Carga historial de la sesión (`ConversationHistory`)
    - Llama a `run_agent_stream` y:
      1. Parsea `[emotion:TAG]` del primer token → envía `emotion` inmediatamente
      2. Envía `text_chunk` por cada fragmento del stream
      3. Al finalizar, detecta `capture_request` si aplica
      4. Construye y envía `response_meta` (emojis + acciones)
      5. Envía `stream_end`
    - Guarda interacción + nuevas memorias en background
    - Verifica compactación del historial en background
35. Registrar el endpoint `ws /ws/interact` en [main.py](main.py) con el handler del paso anterior
36. **Tests unitarios**: mock del WebSocket con `unittest.mock`, flujo auth, acumulación de buffer, parseo de emotion tag en stream → `tests/unit/test_websocket.py`
37. **Test de integración**: levantar la app con `httpx` + cliente WebSocket real; verificar flujo completo auth → interaction_start → text → emotion + text_chunks + stream_end → `tests/integration/test_ws_flow.py`

---

### 7 — Docker Compose + Nginx
*(iteración 7: despliegue en contenedores)*

38. Crear [Dockerfile](Dockerfile): imagen `python:3.12-slim`, instala `uv`, copia `pyproject.toml`, ejecuta `uv sync`, copia código, `CMD ["uv", "run", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]`
39. Crear [nginx/nginx.conf](nginx/nginx.conf): reverse proxy HTTPS en puerto 9393 → FastAPI en `fastapi:8000`; soporte WebSocket con `Upgrade` + `Connection` headers; TLS con certs en `/etc/nginx/certs/`
40. Crear [docker-compose.yml](docker-compose.yml): servicios `fastapi` (build local) y `nginx` (image `nginx:alpine`) con los volúmenes correspondientes para `data/`, `media/`, y `nginx/certs/`
41. Crear script `scripts/generate_certs.sh`: wrapper de `openssl req -x509 ...` para generar cert autofirmado con SAN `192.168.2.200`, e imprimir el fingerprint SHA-256 listo para certificate pinning en Android

---

### 8 — Simulador Streamlit
*(iteración 8: herramienta de prueba sin Android)*

42. Crear [tests/streamlit_simulator/app.py](tests/streamlit_simulator/app.py) — UI web que permite:
    - Configurar URL del backend y API Key en sidebar
    - Panel de conexión WebSocket: botón conectar/desconectar, estado de sesión
    - Campo de texto para enviar mensajes (simula voz procesada)
    - Selector de `user_id` predefinido o `unknown`
    - Visualización en tiempo real de: `emotion` (con emoji OpenMoji), `text_chunks` acumulados, `response_meta` (acciones), `stream_end`, latencia en ms
    - Panel de historial de la sesión
    - Botón para enviar un archivo de audio `.wav` o `.aac` como binario
    - Sección REST auxiliar: `GET /api/health`, `GET /api/users/{id}/memory`

---

### 9 — README
*(iteración 9: documentación para desarrolladores y operadores)*

43. Crear [README.md](README.md) con secciones:
    - **Prerrequisitos**: Python 3.12, uv, Docker + Docker Compose
    - **Ejecución local** (desarrollo): `cp .env.example .env`, editar `GEMINI_API_KEY`, `uv sync`, `uv run uvicorn main:app --reload`; acceso en `http://localhost:8000/docs`
    - **Ejecutar con Docker**: `bash scripts/generate_certs.sh`, `docker compose up -d --build`, verificar con `docker compose ps` y `docker compose logs -f`; acceso en `https://192.168.2.200:9393/docs`
    - **Pruebas unitarias**: `uv run pytest tests/unit/ -v --tb=short`
    - **Pruebas de integración**: `uv run pytest tests/integration/ -v`
    - **Simulador Streamlit**: `uv run streamlit run tests/streamlit_simulator/app.py`, instrucciones de uso con capturas de los pasos
    - **Variables de entorno**: tabla con descripción de cada variable del `.env`
    - **Fingerprint TLS para Android**: cómo extraerlo del cert generado

---

**Verificación**

```bash
# Unitarias
uv run pytest tests/unit/ -v

# Integración (requiere backend corriendo)
uv run pytest tests/integration/ -v

# Salud del backend
curl -k https://192.168.2.200:9393/api/health

# Simulador (dev local)
uv run streamlit run tests/streamlit_simulator/app.py
```

**Criterio de aceptación final**: el simulador Streamlit puede enviar texto, recibir `emotion` → `text_chunks` → `stream_end`, ver el emoji de emoción correcto y el historial de sesión, todo sin necesidad de Android ni ESP32.

---

**Decisiones**
- Se usa **uv** como gestor (no pip ni poetry); `pyproject.toml` como único archivo de dependencias
- **Python 3.12** (el doc dice 3.11+; el usuario dice 3.12)
- Tipos modernos: `str | None`, `list[str]`, `dict[str, Any]` — sin `from typing import Optional/List/Dict`
- **Sin TTS en backend** — el backend solo emite `text_chunk`, Android TTS sintetiza on-device
- **Sin reconocimiento facial en backend** — el backend solo guarda el embedding que Android ya procesó
- El filtro de privacidad en §3.6 queda en `repositories/memory.py` con clasificación por palabras clave en las primeras iteraciones; la versión con Gemini se añade en la iteración 5 (cuando Gemini ya está integrado)
- `deepagents` se usa sin tools (`tools=[]`) — arquitectura list vacía, extensible en futuras versiones
