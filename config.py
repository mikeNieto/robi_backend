from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    # ── Servidor ──────────────────────────────────────────────
    HOST: str = "0.0.0.0"
    PORT: int = 9393
    ENVIRONMENT: str = "development"
    SERVER_IP: str = "192.168.2.200"

    # ── WebSocket ─────────────────────────────────────────────
    WS_PING_INTERVAL: int = 30
    WS_PING_TIMEOUT: int = 10
    WS_MAX_MESSAGE_SIZE_MB: int = 50

    # ── Seguridad ─────────────────────────────────────────────
    API_KEY: str
    ALLOWED_ORIGINS: str = "https://192.168.2.200"

    # ── LLM — Gemini ─────────────────────────────────────────
    GEMINI_API_KEY: str
    GEMINI_MODEL: str = "gemini-2.0-flash-lite"
    GEMINI_MAX_OUTPUT_TOKENS: int = 512
    GEMINI_TEMPERATURE: float = 0.7

    # ── Conversación ──────────────────────────────────────────
    CONVERSATION_KEEP_ALIVE_MS: int = 60000
    CONVERSATION_COMPACTION_THRESHOLD: int = 20

    # ── Búsqueda de persona ───────────────────────────────────
    PERSON_SEARCH_TIMEOUT_MS: int = 8000

    # ── Base de Datos ─────────────────────────────────────────
    DATABASE_URL: str = "sqlite+aiosqlite:///./data/robot.db"

    # ── Almacenamiento ────────────────────────────────────────
    MEDIA_DIR: str = "./media"
    MAX_UPLOAD_SIZE_MB: int = 50

    # ── Logging ───────────────────────────────────────────────
    LOG_LEVEL: str = "INFO"
    LOG_FILE: str = "./media/logs/robot.log"

    # ── Propiedades derivadas ─────────────────────────────────
    @property
    def allowed_origins_list(self) -> list[str]:
        """Devuelve ALLOWED_ORIGINS como lista, soportando valores separados por comas."""
        return [o.strip() for o in self.ALLOWED_ORIGINS.split(",")]

    @property
    def ws_max_message_size_bytes(self) -> int:
        return self.WS_MAX_MESSAGE_SIZE_MB * 1024 * 1024

    @property
    def max_upload_size_bytes(self) -> int:
        return self.MAX_UPLOAD_SIZE_MB * 1024 * 1024

    @property
    def is_production(self) -> bool:
        return self.ENVIRONMENT == "production"


# Instancia singleton — importar desde cualquier módulo con:
#   from config import settings
settings = Settings()  # type: ignore[call-arg]
