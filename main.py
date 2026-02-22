"""
main.py — Punto de entrada de la aplicación FastAPI.

Registra middlewares, routers y manejadores de error.
El endpoint WebSocket /ws/interact gestiona la interacción de voz en tiempo real.

Uso (desarrollo):
    uv run uvicorn main:app --reload --ws wsproto

Uso (producción vía Docker):
    uv run uvicorn main:app --host 0.0.0.0 --port 8000 --ws wsproto
"""

from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from config import settings
from db import create_all_tables, init_db
from middleware.auth import APIKeyMiddleware
from middleware.error_handler import register_error_handlers
from middleware.logging import LoggingMiddleware
from fastapi import WebSocket

from routers.health import router as health_router
from routers.restore import router as restore_router
from ws_handlers.streaming import ws_interact


# ── Lifespan ──────────────────────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    init_db()
    await create_all_tables()
    yield
    # Shutdown
    from db import engine as _engine

    if _engine is not None:
        await _engine.dispose()


# ── Aplicación ────────────────────────────────────────────────────────────────


app = FastAPI(
    title="Robi Backend",
    version="2.0",
    description="Backend de Robi — Amigo Familiar | FastAPI + WebSocket + Gemini",
    lifespan=lifespan,
    # Deshabilitar docs en producción si se desea
    docs_url="/docs" if not settings.is_production else None,
    redoc_url="/redoc" if not settings.is_production else None,
)

# ── Middlewares (orden: el último en añadirse es el primero en ejecutarse) ────

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.add_middleware(APIKeyMiddleware)
app.add_middleware(LoggingMiddleware)

# ── Manejadores de error ──────────────────────────────────────────────────────

register_error_handlers(app)

# ── Routers ───────────────────────────────────────────────────────────────────

app.include_router(health_router)
app.include_router(restore_router)


# ── WebSocket ─────────────────────────────────────────────────────────────────


@app.websocket("/ws/interact")
async def websocket_interact(websocket: WebSocket) -> None:
    """Endpoint WebSocket principal para la interacción de voz con el robot."""
    await ws_interact(websocket)


# ── Arranque directo ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host=settings.HOST,
        port=settings.PORT,
        ws="wsproto",
        reload=not settings.is_production,
        log_level=settings.LOG_LEVEL.lower(),
    )
