from collections.abc import Callable, Iterator

from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy.orm import Session

from apps.central.core import tokens
from apps.central.models.user import CentralUser, Role, UserStatus


def get_db(request: Request) -> Iterator[Session]:
    with request.app.state.sessionmaker() as session:
        yield session


def _unauthorized() -> HTTPException:
    return HTTPException(
        401,
        {"code": "unauthorized", "message": "invalid or missing credentials"},
        headers={"WWW-Authenticate": "Bearer"},
    )


def current_user(
    authorization: str = Header(default=""), db: Session = Depends(get_db)
) -> CentralUser:
    """Bearer JWT → përdorues aktiv nga DB (statusi dhe roli lexohen çdo herë; s'ka cache)."""
    if not tokens.configured():
        raise HTTPException(
            503, {"code": "auth_not_configured", "message": "authentication is unavailable"}
        )
    if not authorization.lower().startswith("bearer "):
        raise _unauthorized()
    try:
        user_id = tokens.decode(authorization[7:].strip())
    except tokens.TokenError:
        raise _unauthorized() from None
    user = db.get(CentralUser, user_id)
    if user is None or user.status != UserStatus.ACTIVE.value:
        raise _unauthorized()
    return user


def require_role(*roles: Role) -> Callable[..., CentralUser]:
    allowed = {r.value for r in roles}

    def dependency(user: CentralUser = Depends(current_user)) -> CentralUser:
        if user.role not in allowed:
            raise HTTPException(403, {"code": "forbidden", "message": "insufficient role"})
        return user

    return dependency
