"""Settings, read from environment variables (see .env.example)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def _bool(value: str | None, default: bool) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    immich_url: str
    immich_api_key: str
    # URL the browser uses to open photos in Immich (defaults to immich_url).
    immich_public_url: str = ""
    data_dir: Path = Path("data")

    # Age estimation backend: "mivolo" (local model) or "ollama" (vision LLM).
    estimator: str = "mivolo"
    device: str = "cpu"
    mivolo_model: str = "iitolstykh/mivolo_v2"
    ollama_url: str = "http://localhost:11434"
    ollama_model: str = "qwen2.5vl:7b"

    # Writing dates back to Immich.
    timezone: str = "UTC"
    # "middle": "1987" becomes 1 July 1987, "start": 1 January 1987.
    date_anchor: str = "middle"
    # Tag root for processed photos ("Age Guesser/Manual", ".../Estimated"). Empty disables tagging.
    tag_root: str = "Age Guesser"
    write_description: bool = True

    # Faces smaller than this (in the preview) are re-cropped from the original file.
    min_face_px: int = 64
    use_original_for_small_faces: bool = True

    @property
    def public_url(self) -> str:
        return (self.immich_public_url or self.immich_url).rstrip("/")

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Settings":
        e = os.environ if env is None else env
        url = e.get("IMMICH_URL", "").strip()
        key = e.get("IMMICH_API_KEY", "").strip()
        if not url or not key:
            raise SystemExit("IMMICH_URL and IMMICH_API_KEY must be set (see .env.example).")
        defaults = cls(immich_url=url, immich_api_key=key)
        settings = cls(
            immich_url=url.rstrip("/"),
            immich_api_key=key,
            immich_public_url=e.get("IMMICH_PUBLIC_URL", "").strip(),
            data_dir=Path(e.get("DATA_DIR", str(defaults.data_dir))),
            estimator=e.get("AGE_ESTIMATOR", defaults.estimator).strip().lower(),
            device=e.get("DEVICE", defaults.device).strip(),
            mivolo_model=e.get("MIVOLO_MODEL", defaults.mivolo_model).strip(),
            ollama_url=e.get("OLLAMA_URL", defaults.ollama_url).strip().rstrip("/"),
            ollama_model=e.get("OLLAMA_MODEL", defaults.ollama_model).strip(),
            timezone=e.get("TIMEZONE", e.get("TZ", defaults.timezone)).strip() or "UTC",
            date_anchor=e.get("DATE_ANCHOR", defaults.date_anchor).strip().lower(),
            tag_root=e.get("TAG_ROOT", defaults.tag_root).strip().strip("/"),
            write_description=_bool(e.get("WRITE_DESCRIPTION"), defaults.write_description),
            min_face_px=int(e.get("MIN_FACE_PX", defaults.min_face_px)),
            use_original_for_small_faces=_bool(
                e.get("USE_ORIGINAL_FOR_SMALL_FACES"), defaults.use_original_for_small_faces
            ),
        )
        try:
            ZoneInfo(settings.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            raise SystemExit(f"Unknown TIMEZONE '{settings.timezone}' (use an IANA name like Europe/Berlin).")
        return settings
