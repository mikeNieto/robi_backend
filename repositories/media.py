"""
MediaRepository — gestión de archivos en `media/uploads/`.

Tipos soportados y su TTL de limpieza:
  - audio  (.wav, .aac, .mp3, .ogg)  → 24 h
  - image  (.jpg, .jpeg, .png, .webp) → 1 h
  - video  (.mp4, .webm, .mov)        → 1 h

Uso:
    media_repo = MediaRepository(base_dir=settings.MEDIA_DIR)
    path = await media_repo.save(data=audio_bytes, filename="clip.wav", media_type="audio")
    await media_repo.cleanup()
"""

import asyncio
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path


# TTL por tipo de medio
_TTL: dict[str, timedelta] = {
    "audio": timedelta(hours=24),
    "image": timedelta(hours=1),
    "video": timedelta(hours=1),
}

# Extensiones conocidas por tipo
_EXTENSIONS: dict[str, frozenset[str]] = {
    "audio": frozenset({".wav", ".aac", ".mp3", ".ogg"}),
    "image": frozenset({".jpg", ".jpeg", ".png", ".webp"}),
    "video": frozenset({".mp4", ".webm", ".mov"}),
}

# Inverso: extensión → tipo
_EXT_TO_TYPE: dict[str, str] = {
    ext: media_type for media_type, exts in _EXTENSIONS.items() for ext in exts
}


class MediaRepository:
    def __init__(self, base_dir: str | Path = "./media/uploads") -> None:
        self._base = Path(base_dir)
        self._base.mkdir(parents=True, exist_ok=True)

    # ── Subdir por tipo ───────────────────────────────────────────────────────

    def _subdir(self, media_type: str) -> Path:
        d = self._base / media_type
        d.mkdir(parents=True, exist_ok=True)
        return d

    # ── Guardar ───────────────────────────────────────────────────────────────

    async def save(self, data: bytes, filename: str, media_type: str) -> Path:
        """
        Escribe `data` en `media/uploads/<media_type>/<filename>`.
        Devuelve el path absoluto al archivo guardado.
        `media_type` debe ser "audio", "image" o "video".
        """
        if media_type not in _TTL:
            raise ValueError(
                f"media_type desconocido: {media_type!r}. Usa {list(_TTL)}"
            )

        dest = self._subdir(media_type) / filename
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, dest.write_bytes, data)
        return dest

    # ── Borrar ────────────────────────────────────────────────────────────────

    async def delete(self, file_path: str | Path) -> bool:
        """
        Elimina un archivo. Devuelve True si existía y fue borrado, False si no existía.
        """
        path = Path(file_path)
        if not path.exists():
            return False
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, path.unlink)
        return True

    # ── Limpieza ──────────────────────────────────────────────────────────────

    async def cleanup(self) -> dict[str, int]:
        """
        Elimina archivos que superan su TTL:
          - audio  > 24 h
          - image  > 1 h
          - video  > 1 h

        Devuelve un dict con el número de archivos borrados por tipo,
        p. ej. {"audio": 3, "image": 0, "video": 1}.
        """
        now = datetime.now(timezone.utc)
        counts: dict[str, int] = {t: 0 for t in _TTL}

        loop = asyncio.get_event_loop()

        def _scan_and_delete() -> dict[str, int]:
            deleted: dict[str, int] = {t: 0 for t in _TTL}
            for media_type, ttl in _TTL.items():
                subdir = self._base / media_type
                if not subdir.exists():
                    continue
                cutoff = now - ttl
                for entry in subdir.iterdir():
                    if not entry.is_file():
                        continue
                    mtime = datetime.fromtimestamp(
                        entry.stat().st_mtime, tz=timezone.utc
                    )
                    if mtime < cutoff:
                        try:
                            entry.unlink()
                            deleted[media_type] += 1
                        except OSError:
                            pass  # ya borrado por otro proceso, ignorar
            return deleted

        counts = await loop.run_in_executor(None, _scan_and_delete)
        return counts

    # ── Utilidad ──────────────────────────────────────────────────────────────

    def media_type_for(self, filename: str) -> str | None:
        """Infiere el tipo de medio a partir de la extensión del nombre de archivo."""
        ext = os.path.splitext(filename)[1].lower()
        return _EXT_TO_TYPE.get(ext)
