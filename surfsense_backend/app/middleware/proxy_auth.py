import logging
import secrets
from datetime import UTC, datetime

from fastapi_users.db import SQLAlchemyUserDatabase
from fastapi_users.password import PasswordHelper
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from app.config import config
from app.db import User, async_session_maker

logger = logging.getLogger(__name__)

_DEFAULT_BYPASS_PATHS = ["/health"]


def _normalise_email(email: str) -> str:
    return email.strip().lower()


def _coerce_bypass_paths(setting) -> list[str]:
    if not setting:
        return list(_DEFAULT_BYPASS_PATHS)
    if isinstance(setting, str):
        return [p.strip() for p in setting.split(",") if p.strip()]
    return list(setting)


def _is_bypass_path(path: str, bypass_paths: list[str]) -> bool:
    return any(path.startswith(p) for p in bypass_paths)


class ProxyAuthMiddleware(BaseHTTPMiddleware):
    """
    Starlette middleware for mPass proxy authentication.

    oauth2-proxy sets X-Auth-Request-Email on every request that has passed
    OIDC validation. This middleware reads that header, finds or creates the
    corresponding SurfSense user, and injects them into request.state.proxy_user
    so the current_active_user dependency sees a fully authenticated user
    without requiring a JWT token.

    Set MPASS_PROXY_AUTH_ENABLED=false in .env to disable entirely.

    Security note: header spoofing is not a concern on protected routes because
    Traefik ForwardAuth overwrites X-Auth-Request-* headers before they reach
    the app. Bypass paths never run this middleware, so spoofed headers there
    have no effect either.
    """

    def __init__(self, app):
        super().__init__(app)
        self.enabled = getattr(config, "MPASS_PROXY_AUTH_ENABLED", False)
        self.bypass_paths = _coerce_bypass_paths(
            getattr(config, "MPASS_BYPASS_PATHS", None)
        )

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        if not self.enabled:
            return await call_next(request)

        # Already injected on this request cycle (idempotent)
        if getattr(request.state, "proxy_user", None) is not None:
            return await call_next(request)

        if _is_bypass_path(request.url.path, self.bypass_paths):
            return await call_next(request)

        raw_email = request.headers.get("x-auth-request-email")
        if not raw_email:
            return await call_next(request)

        user = await self._resolve_user(_normalise_email(raw_email), request)

        # Respect deactivated accounts — mPass authentication does not
        # override an explicit SurfSense account suspension.
        if user is None or not user.is_active:
            return await call_next(request)

        request.state.proxy_user = user
        return await call_next(request)

    async def _resolve_user(self, email: str, request: Request) -> User | None:
        try:
            async with async_session_maker() as session:
                result = await session.execute(select(User).where(User.email == email))
                user = result.scalar_one_or_none()
                created = False

                if user is None:
                    hashed_password = PasswordHelper().hash(secrets.token_urlsafe(32))
                    user = User(
                        email=email,
                        hashed_password=hashed_password,
                        is_active=True,
                        is_verified=True,
                        is_superuser=False,
                    )
                    session.add(user)
                    try:
                        await session.commit()
                        await session.refresh(user)
                        created = True
                    except IntegrityError as exc:
                        # Concurrent request raced us to the insert — fall back
                        # to SELECT by email and re-raise if still not found.
                        await session.rollback()
                        result = await session.execute(
                            select(User).where(User.email == email)
                        )
                        user = result.scalar_one_or_none()
                        if user is None:
                            logger.error(
                                "ProxyAuth: IntegrityError but user still not found "
                                "for %s: %s",
                                email,
                                exc,
                            )
                            return None

                if created:
                    # Trigger on_after_register so the default SearchSpace,
                    # RBAC roles and system prompts are created — same as
                    # Google OAuth and email/password signup.
                    # Use a fresh session so UserManager always has a clean connection.
                    try:
                        from app.users import UserManager

                        async with async_session_maker() as reg_session:
                            if config.AUTH_TYPE == "GOOGLE":
                                from app.db import OAuthAccount

                                user_db = SQLAlchemyUserDatabase(
                                    reg_session, User, OAuthAccount
                                )
                            else:
                                user_db = SQLAlchemyUserDatabase(reg_session, User)

                            user_manager = UserManager(user_db)
                            await user_manager.on_after_register(user, request=request)
                    except Exception:
                        logger.exception(
                            "ProxyAuth: on_after_register failed for %s — "
                            "user created but default search space may be missing",
                            email,
                        )
                else:
                    # Update last_login for returning users (mirrors on_after_login)
                    try:
                        await session.execute(
                            update(User)
                            .where(User.id == user.id)
                            .values(last_login=datetime.now(UTC))
                        )
                        await session.commit()
                    except Exception:
                        logger.warning(
                            "ProxyAuth: failed to update last_login for %s", email
                        )

                return user

        except Exception:
            logger.exception(
                "ProxyAuth: unexpected error resolving user for %s", email
            )
            return None
