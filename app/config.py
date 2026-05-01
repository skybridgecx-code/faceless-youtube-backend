from functools import lru_cache
from pathlib import Path
import re

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_env: str = "development"
    database_url: str = "sqlite:///./content_factory.db"
    output_dir: str = "./out"
    channel_default_name: str = "Local AI Operator"
    require_human_review: bool = True
    enable_youtube_uploads: bool = False
    internal_api_key: str | None = None
    allowed_origins: str | None = None
    rate_limit_ai_per_minute: int = 5
    rate_limit_write_per_minute: int = 60
    openai_api_key: str | None = None
    openai_model: str = "gpt-4o-mini"
    image_generation_provider: str = "placeholder"
    image_generation_api_key: str | None = None
    image_generation_model: str = "gpt-image-1"
    youtube_data_api_key: str | None = None

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    @property
    def is_production(self) -> bool:
        return self.app_env.strip().lower() == "production"

    @property
    def allowed_origins_list(self) -> list[str]:
        configured = (self.allowed_origins or "").strip()
        if configured:
            return [origin.strip() for origin in configured.split(",") if origin.strip()]
        if self.is_production:
            return []
        return [
            "http://localhost",
            "http://localhost:3000",
            "http://localhost:5173",
            "http://127.0.0.1",
            "http://127.0.0.1:3000",
            "http://127.0.0.1:5173",
        ]

    def validate_startup(self) -> None:
        if self.is_production and not (self.internal_api_key or "").strip():
            raise RuntimeError("INTERNAL_API_KEY is required when APP_ENV=production.")
        if self.is_production and not self.allowed_origins_list:
            raise RuntimeError("ALLOWED_ORIGINS is required when APP_ENV=production.")
        model_name = self.openai_model.strip()
        if not model_name:
            raise RuntimeError("OPENAI_MODEL must not be empty.")
        if not re.fullmatch(r"[A-Za-z0-9._:-]+", model_name):
            raise RuntimeError("OPENAI_MODEL contains invalid characters.")
        image_provider = self.image_generation_provider.strip().lower()
        if not image_provider:
            raise RuntimeError("IMAGE_GENERATION_PROVIDER must not be empty.")
        image_model = self.image_generation_model.strip()
        if not image_model:
            raise RuntimeError("IMAGE_GENERATION_MODEL must not be empty.")
        if not re.fullmatch(r"[A-Za-z0-9._:-]+", image_model):
            raise RuntimeError("IMAGE_GENERATION_MODEL contains invalid characters.")

    @property
    def output_path(self) -> Path:
        path = Path(self.output_dir).resolve()
        path.mkdir(parents=True, exist_ok=True)
        return path


@lru_cache
def get_settings() -> Settings:
    return Settings()
