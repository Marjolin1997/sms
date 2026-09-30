import re
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.scope import Owner, owned, ref
from app.models.messaging import ApprovalStatus, Template, TemplateVersion
from app.services import approvals
from app.services.sms_text import count_segments
from app.services.wallet import Conflict, NotFound, WalletError

VAR = re.compile(r"\{\{([a-z_][a-z0-9_]{0,31})\}\}")
MAX_BODY = 1600
MAX_VALUE = 160
MAX_SEGMENTS = 10


class InvalidTemplate(WalletError):
    code = "invalid_template"


class TemplateNotUsable(WalletError):
    code = "template_not_usable"


def variables(body: str) -> list[str]:
    """Validon sintaksën dhe kthen emrat e variablave sipas radhës së parë."""
    if not body.strip() or len(body) > MAX_BODY:
        raise InvalidTemplate(f"body must be 1..{MAX_BODY} characters")
    leftover = VAR.sub("", body)
    if "{{" in leftover or "}}" in leftover:
        raise InvalidTemplate("malformed placeholder; use {{name}} with [a-z0-9_]")
    return list(dict.fromkeys(VAR.findall(body)))


def create(db: Session, owner: Owner, name: str, body: str) -> TemplateVersion:
    variables(body)
    if db.scalar(select(Template).where(owned(Template, owner), Template.name == name)):
        raise Conflict("template name already exists")
    t = Template(owner_ref=ref(owner), name=name)
    db.add(t)
    db.flush()
    return _add_version(db, t.id, body)


def new_version(db: Session, template_id: int, body: str) -> TemplateVersion:
    variables(body)
    if db.get(Template, template_id, with_for_update=True) is None:
        raise NotFound("template not found")
    return _add_version(db, template_id, body)


def _add_version(db: Session, template_id: int, body: str) -> TemplateVersion:
    n = db.scalar(
        select(func.coalesce(func.max(TemplateVersion.version), 0)).where(
            TemplateVersion.template_id == template_id
        )
    )
    v = TemplateVersion(template_id=template_id, version=n + 1, body=body)
    db.add(v)
    db.flush()
    return v


def _version(db: Session, version_id: int) -> TemplateVersion:
    v = db.get(TemplateVersion, version_id, with_for_update=True)
    if v is None:
        raise NotFound("template version not found")
    return v


def review(db: Session, version_id: int, action: str, actor: str, reason: str | None = None):
    v = _version(db, version_id)
    approvals.transition(v, action, actor, reason)
    db.flush()
    return v


def usable_version(db: Session, owner: Owner, template_id: int) -> TemplateVersion:
    """Versioni më i ri i miratuar i një template-i që i përket këtij klienti."""
    v = db.scalar(
        select(TemplateVersion)
        .join(Template, Template.id == TemplateVersion.template_id)
        .where(
            Template.id == template_id,
            owned(Template, owner),
            TemplateVersion.status == ApprovalStatus.APPROVED,
        )
        .order_by(TemplateVersion.version.desc())
        .limit(1)
    )
    if v is None:
        raise TemplateNotUsable("template has no approved version for this account")
    return v


@dataclass(frozen=True)
class Rendered:
    version_id: int
    text: str
    encoding: str
    segments: int


def render(db: Session, owner: Owner, template_id: int, values: dict[str, str]) -> Rendered:
    """Validim para dërgimit: version i miratuar, variabla të plota (pa të tepërta)."""
    v = usable_version(db, owner, template_id)
    needed = set(variables(v.body))
    missing, extra = needed - values.keys(), values.keys() - needed
    if missing or extra:
        raise InvalidTemplate(f"missing={sorted(missing)} unexpected={sorted(extra)}")
    for k, val in values.items():
        if (
            not isinstance(val, str)
            or len(val) > MAX_VALUE
            or any(ord(c) < 32 and c not in "\n" for c in val)
        ):
            raise InvalidTemplate(f"invalid value for '{k}'")
    text = VAR.sub(lambda m: values[m.group(1)], v.body)  # një kalim: vlerat nuk rizgjerohen
    enc, segs = count_segments(text)
    if segs > MAX_SEGMENTS:
        raise InvalidTemplate(f"rendered message exceeds {MAX_SEGMENTS} segments")
    return Rendered(v.id, text, enc, segs)
