from urllib.parse import unquote, urlsplit

from pydantic import computed_field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _is_weak(value: str | None) -> bool:
    """True for an empty value, a change-me style placeholder, or a stock default password."""
    value = (value or "").strip().lower()
    return value in {"", "postgres", "guest"} or value.startswith(("change-me", "changeme"))


class Settings(BaseSettings):
    app_env: str = "development"
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
        hide_input_in_errors=True,
    )

    @model_validator(mode="after")
    def check_app_env_and_refuse_default_secrets_in_production(self) -> "Settings":
        # A typo such as APP_ENV=prod must not switch the production check off.
        self.app_env = self.app_env.strip().lower()
        if self.app_env not in {"development", "test", "production"}:
            raise RuntimeError("app_env must be one of: development, test, production")
        if self.app_env != "production":
            return self
        rabbitmq = urlsplit(self.rabbitmq_url)
        redis = urlsplit(self.redis_url)
        weak = []
        if _is_weak(self.jwt_secret_key):
            weak.append("jwt_secret_key")
        if _is_weak(self.jwt_refresh_secret_key):
            weak.append("jwt_refresh_secret_key")
        if _is_weak(self.postgres_password):
            weak.append("postgres_password")
        if rabbitmq.username == "guest" or _is_weak(unquote(rabbitmq.password or "")):
            weak.append("rabbitmq_url")
        if _is_weak(unquote(redis.password or "")):
            weak.append("redis_url")
        if weak:
            # RuntimeError, not ValueError: pydantic wraps ValueError and its errors() would carry every input value.
            raise RuntimeError(
                "APP_ENV=production but these settings are empty or still a default: " + ", ".join(weak)
            )
        return self

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
