from collections.abc import Awaitable, Callable
from functools import wraps
from typing import Any

from fastapi import HTTPException, status

from app.models.user import User, UserRole


def admin_required(func: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
    @wraps(func)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        current_user = kwargs.get("current_user")

        if current_user is None:
            current_user = next((arg for arg in args if isinstance(arg, User)), None)

        if current_user is None:
            raise RuntimeError("admin_required expects a `current_user` argument.")

        if current_user.role != UserRole.ADMIN:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Admin privileges are required for this action.",
            )

        return await func(*args, **kwargs)

    return wrapper
