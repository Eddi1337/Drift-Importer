"""Application configuration loaded from environment / .env file."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import List

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="DRIFT_", env_file=".env", extra="ignore"
    )

    data_dir: Path = Path("./data")
    working_dir: Path = Path("./working")
    thumbnail_dir: Path = Path("./thumbnails")
    nas_root: Path = Path("/mnt/NAS")
    require_nas_mount: bool = False  # Enabled in the Pi deployment.
    system_sample_interval_s: float = 60.0

    # Comma-separated list of base paths to scan for an attached camera.
    mount_paths: str = "/media,/mnt,/run/media,/Volumes"

    host: str = "0.0.0.0"
    port: int = 8080

    auth_username: str = "admin"
    auth_password: str = ""

    max_concurrent_uploads: int = 1
    max_concurrent_ffmpeg: int = 1
    upload_chunk_bytes: int = 8 * 1024 * 1024

    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"

    # Optional path to an SSH private key used by the rsync-over-SSH backend.
    # Leave blank to rely on the default SSH agent / ~/.ssh keys.
    ssh_key_path: str = ""

    @property
    def mount_path_list(self) -> List[Path]:
        return [Path(p.strip()) for p in self.mount_paths.split(",") if p.strip()]

    @property
    def db_path(self) -> Path:
        return self.data_dir / "drift.db"

    @property
    def secret_key_path(self) -> Path:
        return self.data_dir / "secret.key"

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def auth_enabled(self) -> bool:
        return bool(self.auth_password)

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.working_dir, self.thumbnail_dir, self.log_dir):
            # Leave NAS directories to guarded jobs; the dashboard must still
            # start when the NAS is offline, without creating SD-card fallbacks.
            if self.require_nas_mount and d.is_relative_to(self.nas_root):
                continue
            d.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_dirs()
    return settings
