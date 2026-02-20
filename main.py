"""
main.py — Punto de entrada de la aplicación FastAPI.

Registra middlewares, routers y manejadores de error.
El endpoint WebSocket /ws/interact se añade en el paso 6.

Uso (desarrollo):
    uv run uvicorn main:app --reload

Uso (producción vía Docker):
    uv run uvicorn main:app --host 0.0.0.0 --port 8000
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
from routers.health import router as health_router


# ── Lifespan ──────────────────────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    init_db()
    await create_all_tables()
    yield
    # Shutdown (nada que limpiar por ahora)


# ── Aplicación ────────────────────────────────────────────────────────────────


app = FastAPI(
    title="Robi Backend",
    version="1.4",
    description="Backend del robot doméstico Robi — FastAPI + WebSocket + Gemini",
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
# Los routers REST de usuarios, memoria y face se añaden en el paso 4.
# El endpoint WebSocket /ws/interact se añade en el paso 6.


# ── Arranque directo ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host=settings.HOST,
        port=settings.PORT,
        reload=not settings.is_production,
        log_level=settings.LOG_LEVEL.lower(),
    )
