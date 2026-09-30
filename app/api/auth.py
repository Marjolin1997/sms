"""Hyrje me email + fjalëkalim: login, logout, sesione, ndryshim fjalëkalimi, ftesa."""

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.security import Principal, current_principal
from app.models.users import User, UserSession
from app.services import auth as svc
from app.services import system_mail
from app.services.audit import audit
from app.services.wallet import WalletError

router = APIRouter(prefix="/v1/auth")
_STATUS = {"invalid_credentials": 401, "invalid_token": 404, "not_found": 404, "conflict": 409}


def _run(db: Session, fn):
    try:
        out = fn()
        db.commit()
        return out
    except WalletError as e:
        db.rollback()
        raise HTTPException(_STATUS.get(e.code, 422), {"code": e.code, "message": str(e)}) from e


def _session_out(user: User, s: UserSession, token: str) -> dict:
    return {
        "token": token,
        "expires_at": s.expires_at,
        "user": {"email": user.email, "role": user.role, "owner_ref": user.owner_ref},
    }


def _actor(u: User) -> Principal:
    return Principal(f"user:{u.id}", u.role, u.owner_ref)


class LoginIn(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(max_length=256)
    remember: bool = False


@router.post("/login")
def login(body: LoginIn, db: Session = Depends(get_db), user_agent: str = Header(default="")):
    res = svc.login(db, body.email, body.password, body.remember, user_agent)
    if res is None:
        db.commit()  # numëruesi i dështimeve ruhet
        raise HTTPException(
            401,
            {
                "code": "invalid_credentials",
                "message": "Wrong email or password, or the account is temporarily locked after "
                "too many attempts. Try again in a few minutes.",
            },
        )
    u, s, token = res
    audit(db, _actor(u), "auth.login", "user", u.id)
    db.commit()
    return _session_out(u, s, token)


def _session_principal(p: Principal = Depends(current_principal)) -> Principal:
    if p.session_id is None:
        raise HTTPException(
            400, {"code": "session_required", "message": "sign in with email and password first"}
        )
    return p


@router.post("/logout", status_code=204)
def logout(db: Session = Depends(get_db), p: Principal = Depends(_session_principal)):
    _run(db, lambda: svc.revoke_session(db, int(p.actor.split(":")[1]), p.session_id))


@router.get("/sessions")
def sessions(db: Session = Depends(get_db), p: Principal = Depends(_session_principal)):
    uid = int(p.actor.split(":")[1])
    return [
        {"id": s.id, "current": s.id == p.session_id, "user_agent": s.user_agent,
         "created_at": s.created_at, "last_seen_at": s.last_seen_at, "expires_at": s.expires_at}
        for s in svc.active_sessions(db, uid)
    ]  # fmt: skip


@router.delete("/sessions/{session_id}", status_code=204)
def revoke(
    session_id: int, db: Session = Depends(get_db), p: Principal = Depends(_session_principal)
):
    _run(db, lambda: svc.revoke_session(db, int(p.actor.split(":")[1]), session_id))


class PasswordIn(BaseModel):
    current_password: str = Field(max_length=256)
    new_password: str = Field(max_length=256)


@router.post("/change-password", status_code=204)
def change_password(
    body: PasswordIn,
    tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    p: Principal = Depends(_session_principal),
):
    uid = int(p.actor.split(":")[1])

    def go():
        svc.change_password(db, uid, body.current_password, body.new_password, p.session_id)
        audit(db, p, "auth.password_changed", "user", uid)
        return db.get(User, uid).email

    tasks.add_task(system_mail.send_password_changed, _run(db, go))


@router.get("/config")
def auth_config():
    """Çfarë ofron kjo instalim; paneli e përdor për të treguar 'Forgot password?'."""
    return {"self_service_reset": system_mail.enabled()}


class ForgotIn(BaseModel):
    email: str = Field(max_length=254)


@router.post("/forgot", status_code=202)
def forgot_password(body: ForgotIn, tasks: BackgroundTasks, db: Session = Depends(get_db)):
    """Gjithmonë e njëjta përgjigje, ekziston email-i apo jo. Lidhja del vetëm me email."""
    if not system_mail.enabled():
        raise HTTPException(
            503,
            {
                "code": "reset_unavailable",
                "message": "Email reset isn't set up. Ask your administrator.",
            },
        )
    res = svc.request_reset(db, body.email)
    if res:
        u, token = res
        audit(db, _actor(u), "auth.reset_requested", "user", u.id)
        tasks.add_task(
            system_mail.send_reset, u.email, token, int(svc.RESET_TTL.total_seconds() // 60)
        )
    db.commit()
    return {"ok": True}


@router.get("/invite/{token}")
def invite_info(token: str, db: Session = Depends(get_db)):
    """Email-i për faqen "vendos fjalëkalimin"; tokenin e ka vetëm i ftuari."""
    return _run(db, lambda: svc.token_info(db, token))


class AcceptIn(BaseModel):
    password: str = Field(max_length=256)


@router.post("/invite/{token}")
def accept_invite(
    token: str,
    body: AcceptIn,
    tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    user_agent: str = Header(default=""),
):
    def go():
        was_reset = svc.token_info(db, token)["kind"] == "reset"
        u, s, tok = svc.accept_token(db, token, body.password, user_agent)
        audit(db, _actor(u), "auth.password_set", "user", u.id)
        return _session_out(u, s, tok), u.email, was_reset

    out, email, was_reset = _run(db, go)
    if was_reset:
        tasks.add_task(system_mail.send_password_changed, email)
    return out
