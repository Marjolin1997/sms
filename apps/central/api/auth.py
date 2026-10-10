import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from apps.central.api.deps import current_user, get_db
from apps.central.core import tokens
from apps.central.core.errors import AuthenticationFailed
from apps.central.models.user import CentralUser
from apps.central.services import users

router = APIRouter(prefix="/auth")
log = logging.getLogger("central.auth")


class LoginIn(BaseModel):
    email: str = Field(max_length=320)
    password: str = Field(max_length=1024)


@router.post("/token")
def login(body: LoginIn, request: Request, db: Session = Depends(get_db)):
    ip = request.client.host if request.client else "unknown"
    if not tokens.configured():
        raise HTTPException(
            503, {"code": "auth_not_configured", "message": "authentication is unavailable"}
        )
    try:
        user = users.authenticate(db, body.email, body.password)
    except AuthenticationFailed as e:  # përgjigje e njëtrajtshme: pa dallim unknown/bad/disabled
        log.warning("login failed reason=%s ip=%s", e.reason, ip)
        raise HTTPException(
            401,
            {"code": "invalid_credentials", "message": "invalid email or password"},
            headers={"WWW-Authenticate": "Bearer"},
        ) from None
    db.commit()  # mund të ketë rehash
    token, ttl = tokens.issue(user.id)
    log.info("login ok user=%s ip=%s", user.id, ip)
    return {"access_token": token, "token_type": "bearer", "expires_in": ttl}


@router.get("/me")
def me(user: CentralUser = Depends(current_user)):
    return {"id": str(user.id), "email": user.email, "role": user.role, "status": user.status}
