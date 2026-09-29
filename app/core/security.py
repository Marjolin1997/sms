import hmac

from fastapi import Header, HTTPException, status

from app.core.config import settings


def require_admin(x_admin_key: str = Header(default="")) -> None:
    """Placeholder deri në Fazën 7 (RBAC + API keys me scope)."""
    expected = settings.admin_api_key
    if not expected or not hmac.compare_digest(x_admin_key, expected):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid admin key")
