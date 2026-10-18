"""Bootstrap manual, idempotent: kopjon Enterprise-t ekzistues nga Enterprise DB në Central.

    ENTERPRISE_DATABASE_URL=... CENTRAL_DATABASE_URL=... \\
        python -m apps.central.tools.bootstrap_enterprises [--dry-run]

Rregulla: `sms_enterprises.id` ruhet (pa UUID të reja); lexon Enterprise DB vetëm (transaksion
vetëm-lexim, SQL minimal, pa ORM të Enterprise) dhe shkruan vetëm Central DB; asnjë shkrim prapa,
asnjë fshirje, asnjë mbishkrim. Jo shërbim runtime, jo sync, jo i importuar nga Central në kërkesa.
`owner_ref` NUK ruhet në Central: përdoret vetëm si burim fallback i emrit dhe për raport.

Planifikimi bëhet i plotë para çdo shkrimi; nëse ka konflikte/rreshta të pavlefshëm nuk shkruhet
asgjë (kodi 1). Shkrimi është një transaksion i vetëm në Central (all-or-nothing); rerun i sigurt.
Kodet e daljes: 0 = ok, 1 = konflikte/të pavlefshme (asgjë e shkruar), 2 = konfigurim/lidhje.
"""

import argparse
import os
import sys
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from apps.central.core.config import settings
from apps.central.core.db import make_engine
from apps.central.core.errors import Invalid
from apps.central.models.enterprise import Enterprise, EnterpriseStatus
from apps.central.services.enterprises import normalize_name

# Tabela eksplicite e statuseve (Enterprise → Central). Çdo vlerë tjetër = e pavlefshme.
STATUS_MAP = {
    "active": EnterpriseStatus.ACTIVE.value,
    "suspended": EnterpriseStatus.SUSPENDED.value,
}

_SOURCE = text(
    "select id, owner_ref, external_id, legal_name, short_name, status, created_at, updated_at "
    "from sms_enterprises order by created_at, id"
).columns(
    sa.column("id", sa.Uuid()), sa.column("owner_ref", sa.String()),
    sa.column("external_id", sa.String()), sa.column("legal_name", sa.String()),
    sa.column("short_name", sa.String()), sa.column("status", sa.String()),
    sa.column("created_at", sa.DateTime(timezone=True)),
    sa.column("updated_at", sa.DateTime(timezone=True)),
)  # fmt: skip


@dataclass
class Source:
    id: uuid.UUID | None
    owner_ref: str | None
    external_id: str | None
    legal_name: str | None
    short_name: str | None
    status: str | None
    created_at: datetime | None
    updated_at: datetime | None


@dataclass
class Item:
    id: uuid.UUID
    name: str
    status: str
    created_at: datetime | None
    updated_at: datetime | None
    name_source: str


@dataclass
class Report:
    scanned: int = 0
    create: list[Item] = field(default_factory=list)
    matching: list[Item] = field(default_factory=list)
    conflicts: list[dict] = field(default_factory=list)
    invalid: list[dict] = field(default_factory=list)
    central_only: int = 0
    name_sources: Counter = field(default_factory=Counter)
    dry_run: bool = False
    written: int = 0

    @property
    def ok(self) -> bool:
        return not self.conflicts and not self.invalid

    def render(self) -> str:
        out = [
            f"Scanned: {self.scanned}",
            f"Create: {len(self.create)}",
            f"Matching: {len(self.matching)}",
            f"Conflicts: {len(self.conflicts)}",
            f"Invalid: {len(self.invalid)}",
            f"Central-only (untouched): {self.central_only}",
            "Name sources: " + (", ".join(f"{k}={v}" for k, v in sorted(self.name_sources.items()))
                               or "-"),
            f"Mode: {'dry-run (0 writes)' if self.dry_run else 'apply'}; written: {self.written}",
        ]  # fmt: skip
        for c in self.conflicts:
            out.append(f"CONFLICT enterprise_id={c['id']} reason={c['reason']} "
                       f"source={c['source']} target={c['target']}")  # fmt: skip
        for i in self.invalid:
            out.append(
                f"INVALID enterprise_id={i['id']} reason={i['reason']} owner_ref={i['owner_ref']!r}"
            )
        if self.name_sources.get("owner_ref"):
            out.append(
                "NOTE: names derived from owner_ref are provisional; rename in Central later."
            )
        return "\n".join(out)


def read_source(enterprise_url: str) -> list[Source]:
    """Lexim vetëm-lexim nga Enterprise DB (SQL minimal; pa ORM të Enterprise)."""
    engine = create_engine(enterprise_url)
    try:
        with engine.connect() as conn:
            if engine.dialect.name == "postgresql":
                conn.execute(text("SET TRANSACTION READ ONLY"))
            return [Source(*row) for row in conn.execute(_SOURCE)]
    finally:
        engine.dispose()


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def _derive_name(r: Source) -> tuple[str, str]:
    """legal_name → short_name → owner_ref (external_id është identifikues, jo emër)."""
    for src in ("legal_name", "short_name", "owner_ref"):
        v = getattr(r, src)
        if isinstance(v, str) and v.strip():
            return normalize_name(v), src
    raise Invalid("no usable name source (legal_name, short_name, owner_ref all blank)")


def build_plan(rows: list[Source], central: Session) -> Report:
    rep = Report(scanned=len(rows))
    ids = Counter(r.id for r in rows if r.id is not None)
    owners = defaultdict(list)
    for r in rows:
        if isinstance(r.owner_ref, str):
            owners[r.owner_ref.strip().lower()].append(r)
    existing = {e.id: e for e in central.scalars(select(Enterprise))}

    def bad(r, reason):
        rep.invalid.append({"id": r.id, "reason": reason, "owner_ref": r.owner_ref})

    for r in rows:
        if r.id is None:
            bad(r, "missing id")
            continue
        if ids[r.id] > 1:
            bad(r, "duplicate enterprise id in source")
            continue
        if not isinstance(r.owner_ref, str) or not r.owner_ref.strip():
            bad(r, "missing owner_ref")
            continue
        if len(owners[r.owner_ref.strip().lower()]) > 1:
            bad(r, "duplicate owner_ref in source (case/space-insensitive)")
            continue
        status = STATUS_MAP.get(r.status or "")
        if status is None:
            bad(r, f"unknown status {r.status!r}")
            continue
        try:
            name, name_src = _derive_name(r)
        except Invalid as e:
            bad(r, str(e))
            continue
        item = Item(r.id, name, status, _aware(r.created_at), _aware(r.updated_at), name_src)
        cur = existing.get(r.id)
        if cur is None:
            rep.create.append(item)
            rep.name_sources[name_src] += 1
        elif (cur.name, cur.status) == (name, status):
            rep.matching.append(item)
        else:
            rep.conflicts.append({
                "id": r.id, "reason": "enterprise id exists in Central with different data",
                "source": {"name": name, "status": status},
                "target": {"name": cur.name, "status": cur.status},
            })  # fmt: skip
    rep.central_only = len(set(existing) - {r.id for r in rows if r.id is not None})
    return rep


def run(enterprise_url: str, central_url: str, *, dry_run: bool = False,
        central_engine: Engine | None = None) -> Report:  # fmt: skip
    if enterprise_url == central_url:
        raise ValueError("ENTERPRISE_DATABASE_URL and CENTRAL_DATABASE_URL must differ")
    rows = read_source(enterprise_url)
    engine = central_engine or make_engine(central_url)
    try:
        with Session(engine, expire_on_commit=False) as db:
            rep = build_plan(rows, db)
            rep.dry_run = dry_run
            if dry_run or not rep.ok or not rep.create:
                return rep
            now = datetime.now(UTC)
            for it in rep.create:  # historia e ruajtur: created_at/updated_at nga burimi
                db.add(Enterprise(id=it.id, name=it.name, status=it.status,
                                  created_at=it.created_at or now,
                                  updated_at=it.updated_at or it.created_at or now))  # fmt: skip
            db.commit()  # një transaksion: all-or-nothing; rerun idempotent
            rep.written = len(rep.create)
            return rep
    finally:
        if central_engine is None:
            engine.dispose()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Bootstrap Enterprise records into Central (manual).")
    ap.add_argument("--dry-run", action="store_true", help="report only; zero writes")
    args = ap.parse_args(argv)
    ent = os.environ.get("ENTERPRISE_DATABASE_URL")
    if not ent:
        print("ENTERPRISE_DATABASE_URL is required", file=sys.stderr)  # noqa: T201
        return 2
    try:
        rep = run(ent, settings.database_url, dry_run=args.dry_run)
    except Exception as e:  # raport pa URL/sekrete
        print(f"bootstrap failed: {type(e).__name__}: {str(e)[:200]}", file=sys.stderr)  # noqa: T201
        return 2
    print(rep.render())  # noqa: T201
    return 0 if rep.ok else 1


if __name__ == "__main__":
    sys.exit(main())
