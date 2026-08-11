from functools import lru_cache
from pathlib import Path
import re

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_env: str = "development"
    database_url: str = "sqlite:///./content_factory.db"
    dbos_system_database_url: str = "sqlite:///./dbos_system.db"
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
    i5_tts_provider: str = "openai"
    i5_tts_model: str = "tts-1-hd"
    i5_tts_fallback_model: str = "tts-1"
    i5_tts_voice: str = "onyx"
    i5_generated_image_provider: str = "openai"
    i5_generated_image_model: str = "gpt-image-2"
    i5_generated_image_size: str = "1280x720"
    i5_generated_image_primary_quality: str = "medium"
    i5_generated_image_fallback_quality: str = "low"
    i5_video_provider: str = "disabled"
    i5_video_model: str = "sora-2"
    i5_allow_deprecated_sora: bool = False
    youtube_data_api_key: str | None = None
    youtube_oauth_client_id: str = ""
    youtube_oauth_client_secret: str = ""
    youtube_oauth_refresh_token: str = ""
    affiliate_offers_json: str = ""

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
        self.validate_i5_configuration()

    @property
    def i5_image_generation_api_key(self) -> str | None:
        """Prefer IMAGE_GENERATION_API_KEY, then shared OPENAI_API_KEY, for I5 images."""

        for candidate in (self.image_generation_api_key, self.openai_api_key):
            value = (candidate or "").strip()
            if value:
                return value
        return None

    def validate_i5_configuration(self, *, require_credentials: bool = False) -> None:
        """Validate the locked I5 provider catalog independently of legacy defaults."""

        locked_values = (
            ("I5_TTS_PROVIDER", self.i5_tts_provider.strip().lower(), "openai"),
            ("I5_TTS_MODEL", self.i5_tts_model.strip(), "tts-1-hd"),
            (
                "I5_TTS_FALLBACK_MODEL",
                self.i5_tts_fallback_model.strip(),
                "tts-1",
            ),
            ("I5_TTS_VOICE", self.i5_tts_voice.strip().lower(), "onyx"),
            (
                "I5_GENERATED_IMAGE_PROVIDER",
                self.i5_generated_image_provider.strip().lower(),
                "openai",
            ),
            (
                "I5_GENERATED_IMAGE_MODEL",
                self.i5_generated_image_model.strip(),
                "gpt-image-2",
            ),
            (
                "I5_GENERATED_IMAGE_SIZE",
                self.i5_generated_image_size.strip().lower(),
                "1280x720",
            ),
            (
                "I5_GENERATED_IMAGE_PRIMARY_QUALITY",
                self.i5_generated_image_primary_quality.strip().lower(),
                "medium",
            ),
            (
                "I5_GENERATED_IMAGE_FALLBACK_QUALITY",
                self.i5_generated_image_fallback_quality.strip().lower(),
                "low",
            ),
            ("I5_VIDEO_MODEL", self.i5_video_model.strip(), "sora-2"),
        )
        for environment_name, actual, expected in locked_values:
            if actual != expected:
                raise RuntimeError(
                    f"{environment_name} must be {expected!r} for canonical I5."
                )

        video_provider = self.i5_video_provider.strip().lower()
        if video_provider not in {"disabled", "openai", "sora"}:
            raise RuntimeError(
                "I5_VIDEO_PROVIDER must be 'disabled', 'openai', or 'sora' for canonical I5."
            )
        if video_provider != "disabled" and not self.i5_allow_deprecated_sora:
            raise RuntimeError(
                "I5_ALLOW_DEPRECATED_SORA must be true when Sora video is enabled."
            )
        if require_credentials and not (self.openai_api_key or "").strip():
            raise RuntimeError("OPENAI_API_KEY is required to start canonical I5 production.")

    def validate_i5_production(self) -> None:
        """Validate provider selection and credentials before production binding."""

        self.validate_i5_configuration(require_credentials=True)

    @property
    def output_path(self) -> Path:
        path = Path(self.output_dir).resolve()
        path.mkdir(parents=True, exist_ok=True)
        return path


@lru_cache
def get_settings() -> Settings:
    return Settings()
