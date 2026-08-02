"""Application configuration loaded from environment / .env file."""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import Field, computed_field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Strongly-typed settings sourced from `.env`."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    bot_token: str = Field(..., alias="BOT_TOKEN")
    database_url: str = Field(
        default="sqlite+aiosqlite:///mafia.db", alias="DATABASE_URL"
    )
    # Single-owner legacy setting, kept so old deployments keep working.
    admin_id: int = Field(default=0, alias="ADMIN_ID")
    # Comma-separated list of bot owners, e.g. ADMIN_IDS=111,222,333.
    # These ids get every admin power, including granting admin rights.
    admin_ids_raw: str = Field(default="", alias="ADMIN_IDS")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    # Render injects PORT; we fall back to 8080 for local dev.
    web_port: int = Field(default=8080, alias="PORT")

    @field_validator("database_url", mode="after")
    @classmethod
    def _normalize_database_url(cls, value: str) -> str:
        """Normalise the DB URL and make SQLite survive redeploys.

        Two problems are solved here:

        1. Render injects ``DATABASE_URL`` as ``postgres://user:pass@host/db``
           (sync driver convention). SQLAlchemy needs an explicit async
           driver: ``postgresql+asyncpg://...``.
        2. A *relative* SQLite path (``sqlite+aiosqlite:///mafia.db``) lives
           inside the deploy directory, which every PaaS wipes on each
           deploy - the database looked "recreated" after every push. When
           ``DATA_DIR`` (or a mounted Render disk) is available we rewrite
           the path to an absolute location on that persistent volume.
        """
        if value.startswith("postgres://"):
            return "postgresql+asyncpg://" + value[len("postgres://"):]
        if value.startswith("postgresql://"):
            return "postgresql+asyncpg://" + value[len("postgresql://"):]
        if value.startswith("sqlite"):
            return cls._anchor_sqlite_path(value)
        return value

    @staticmethod
    def _anchor_sqlite_path(value: str) -> str:
        """Move a relative SQLite file onto the persistent data directory.

        Absolute paths (four slashes) and in-memory URLs are left alone -
        the operator asked for a specific location and we honour it.
        """
        scheme, _, path = value.partition(":///")
        if not path or path.startswith("/") or ":memory:" in value:
            return value
        data_dir = os.getenv("DATA_DIR") or os.getenv("RENDER_DISK_MOUNT_PATH")
        if not data_dir:
            # Render mounts disks under /var/data by convention; use it only
            # when it actually exists so local runs keep using ./mafia.db.
            default_dir = Path("/var/data")
            if not default_dir.is_dir():
                return value
            data_dir = str(default_dir)
        target = Path(data_dir).expanduser()
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError:
            # Read-only filesystem: fall back to the original relative path
            # instead of crashing the bot on startup.
            return value
        return f"{scheme}:///{target / path}"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def admin_ids(self) -> frozenset[int]:
        """Every bot owner id, merged from ``ADMIN_IDS`` and ``ADMIN_ID``.

        Accepts commas, spaces and semicolons as separators so a copied
        list from a chat message does not silently produce zero admins.
        Non-numeric entries are ignored rather than crashing the bot on
        startup - a typo must never take the whole service down.
        """
        found: set[int] = set()
        raw = (self.admin_ids_raw or "").replace(";", ",").replace(" ", ",")
        for chunk in raw.split(","):
            chunk = chunk.strip()
            if not chunk:
                continue
            try:
                value = int(chunk)
            except ValueError:
                continue
            if value > 0:
                found.add(value)
        if self.admin_id > 0:
            found.add(self.admin_id)
        return frozenset(found)

    @property
    def is_admin_configured(self) -> bool:
        return bool(self.admin_ids)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached settings. Lazily resolved so importing this module
    does not require a configured environment (useful for tooling/tests)."""
    return Settings()


# Convenience module-level accessor. Callers should use ``get_settings()``
# directly when they want DI; ``settings`` is kept for backward-compat.
settings = get_settings()
