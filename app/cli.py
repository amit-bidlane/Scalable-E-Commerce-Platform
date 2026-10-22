import argparse
import asyncio
from getpass import getpass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import get_password_hash
from app.db.session import AsyncSessionLocal
from app.models.user import User, UserRole

MIN_PASSWORD_LENGTH = 8


async def create_admin(email: str, password: str | None, promote: bool, session: AsyncSession) -> User:
    result = await session.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()

    if promote:
        if user is None:
            raise ValueError(f"{email} does not exist. Nothing was created.")
        user.role = UserRole.ADMIN
    else:
        if user is not None:
            raise ValueError(f"{email} already exists. Use --promote to make that user an admin.")
        if password is None or len(password) < MIN_PASSWORD_LENGTH:
            raise ValueError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")
        user = User(email=email, hashed_password=get_password_hash(password), role=UserRole.ADMIN)
        session.add(user)

    await session.commit()
    return user


def _ask_password() -> str:
    password = getpass("Password: ")
    if password != getpass("Repeat password: "):
        raise ValueError("Passwords do not match.")
    return password


async def _run_create_admin(args: argparse.Namespace) -> None:
    password = None if args.promote else _ask_password()
    async with AsyncSessionLocal() as session:
        user = await create_admin(args.email, password, args.promote, session)
    print(f"{user.email} is now an admin.")


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    commands = parser.add_subparsers(dest="command", required=True)
    admin = commands.add_parser("create_admin", help="Create an admin user.")
    admin.add_argument("email")
    admin.add_argument("--promote", action="store_true", help="Make an existing user an admin.")

    args = parser.parse_args()
    try:
        asyncio.run(_run_create_admin(args))
    except ValueError as exc:
        raise SystemExit(str(exc))


if __name__ == "__main__":
    main()
