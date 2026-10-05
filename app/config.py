from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Core
    data_dir: Path = Path("./data")
    database_url: str | None = None  # default: sqlite:///{data_dir}/billsplit.db
    jwt_secret: str = "change-me"
    access_token_minutes: int = 15
    refresh_token_days: int = 30
    cors_origins: list[str] = ["*"]
    default_currency: str = "USD"  # currency for seeded groups

    # Bootstrap super admin (only used when the users table is empty)
    admin_username: str | None = None
    admin_password: str | None = None
    admin_display_name: str = "Admin"

    # Uploads
    upload_max_mb: int = 10

    # Storage: R2 is used when all r2_* settings are present
    r2_endpoint_url: str | None = None
    r2_access_key_id: str | None = None
    r2_secret_access_key: str | None = None
    r2_bucket: str | None = None
    r2_presign_seconds: int = 300

    # OCR
    ocr_enabled: bool = True
    ocr_max_concurrency: int = 1
    ocr_max_queue: int = 3
    ocr_timeout_seconds: float = 30
    ocr_header_retry: bool = True  # second, bounded OCR pass on the receipt header when no merchant is found
    ocr_threads: int = 0  # ONNX Runtime threads per OCR job; 0 = all cores. Set to the vCPU count on small servers

    # Scheduler
    scheduler_enabled: bool = True
    scheduler_interval_seconds: int = 300

    # Login rate limit
    login_attempts_per_minute: int = 5

    @property
    def db_url(self) -> str:
        return self.database_url or f"sqlite:///{self.data_dir / 'billsplit.db'}"

    @property
    def r2_configured(self) -> bool:
        return all([self.r2_endpoint_url, self.r2_access_key_id, self.r2_secret_access_key, self.r2_bucket])


@lru_cache
def get_settings() -> Settings:
    return Settings()
