from fastapi import APIRouter

router = APIRouter()


@router.get(
    "/health",
    summary="API Health Check",
    description="API-scoped readiness probe exposed under the versioned route namespace.",
    response_description="Simple status payload confirming the versioned API router is healthy.",
)
async def api_healthcheck() -> dict[str, str]:
    return {"status": "ok"}
