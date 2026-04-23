from contextlib import asynccontextmanager

from fastapi import FastAPI
from prometheus_fastapi_instrumentator import Instrumentator

from app.api.router import api_router
from app.core.cache import redis_cache
from app.core.config import settings
from app.core.logging import configure_logging
from app.core.messaging import rabbitmq_client
from app.core.rate_limiter import setup_rate_limiter

logger = configure_logging("app.api")

openapi_tags = [
    {"name": "health", "description": "Operational health and readiness probes."},
    {"name": "auth", "description": "Authentication, token lifecycle, and OAuth scaffolding endpoints."},
    {"name": "products", "description": "Catalog browsing, search, and admin product management endpoints."},
    {"name": "payments", "description": "Checkout, provider order creation, and payment webhook processing endpoints."},
]


@asynccontextmanager
async def lifespan(_: FastAPI):
    await redis_cache.connect()
    await rabbitmq_client.connect()
    logger.info("Application resources connected.")
    try:
        yield
    finally:
        await rabbitmq_client.close()
        await redis_cache.close()
        logger.info("Application resources closed.")


app = FastAPI(
    title=settings.project_name,
    description=(
        "Scalable E-Commerce Platform API with JWT authentication, RBAC, Redis caching, "
        "payment workflows, RabbitMQ notifications, and production observability."
    ),
    version="0.1.0",
    openapi_tags=openapi_tags,
    lifespan=lifespan,
)
setup_rate_limiter(app)
app.include_router(api_router, prefix=settings.api_v1_str)

if settings.enable_metrics:
    Instrumentator().instrument(app).expose(app, include_in_schema=False, should_gzip=True)


@app.get(
    "/health",
    tags=["health"],
    summary="Platform Health Check",
    description="Lightweight liveness probe for load balancers, orchestrators, and uptime checks.",
    response_description="Simple status payload confirming the API process is reachable.",
)
async def healthcheck() -> dict[str, str]:
    return {"status": "ok"}
