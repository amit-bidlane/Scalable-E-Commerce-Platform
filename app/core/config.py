from pydantic import computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    project_name: str = "Scalable E-Commerce Platform"
    api_v1_str: str = "/api/v1"
    postgres_server: str = "db"
    postgres_port: int = 5432
    postgres_user: str = "postgres"
    postgres_password: str = "postgres"
    postgres_db: str = "ecommerce"
    jwt_secret_key: str = "change-me"
    jwt_refresh_secret_key: str = "change-me-too"
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 15
    refresh_token_expire_days: int = 7
    rate_limit_per_minute: int = 100
    google_client_id: str | None = None
    google_client_secret: str | None = None
    google_redirect_uri: str = "http://localhost:8000/api/v1/auth/google/callback"
    redis_url: str = "redis://redis:6379/0"
    product_cache_ttl_seconds: int = 300
    payment_provider: str = "razorpay"
    payment_currency: str = "INR"
    payment_webhook_idempotency_ttl_seconds: int = 86400
    stripe_secret_key: str | None = None
    stripe_publishable_key: str | None = None
    stripe_webhook_secret: str | None = None
    razorpay_key_id: str | None = None
    razorpay_key_secret: str | None = None
    razorpay_webhook_secret: str | None = None
    rabbitmq_url: str = "amqp://guest:guest@rabbitmq:5672/"
    notifications_queue: str = "notifications_queue"
    log_level: str = "INFO"
    enable_metrics: bool = True

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    @computed_field
    @property
    def database_uri(self) -> str:
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_server}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def alembic_database_uri(self) -> str:
        return (
            f"postgresql+psycopg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_server}:{self.postgres_port}/{self.postgres_db}"
        )


settings = Settings()
