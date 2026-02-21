# ── Etapa única: python:3.12-slim + uv ───────────────────────────────────────
FROM python:3.12-slim

# Copiar el binario de uv desde la imagen oficial
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

# Crear directorio de trabajo
WORKDIR /app

# ── Dependencias (capa cacheada — solo se reconstruye si cambian los lock files)
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

# ── Código fuente (se copia después para aprovechar el caché de capas)
COPY . .

# ── Directorios de datos en runtime (se sobreescriben por volúmenes en Compose)
RUN mkdir -p data media/uploads media/logs

# Exponer el puerto interno de FastAPI
EXPOSE 8000

# ── Arranque ──────────────────────────────────────────────────────────────────
CMD ["uv", "run", "uvicorn", "main:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--ws", "wsproto"]
