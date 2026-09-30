"""Regjistri i Enterprise-ve (M1a). Vetëm identitet dhe mapping `owner_ref → enterprise.id`;
NUK ndikon në autorizim, query scoping, dërgim, ledger apo API. Asgjë këtu nuk thirret nga rruga e
kërkesave (M1b do ta lidhë me shkrimin e centralizuar).

Rregull: `owner_ref` krahasohet saktësisht siç është. Asnjë normalizim, bashkim apo hamendësim."""

import logging
import re
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import func, inspect, select, text
from sqlalchemy.orm import Session

from app.models.enterprise import Enterprise
from app.services.wallet import NotFound, WalletError

# Tabelat që mbajnë `owner_ref` direkt. Kopja e ngrirë e kësaj liste është te migrimi 0018;
# testi i driftit i detyron të përputhen me modelet.
LEGACY_OWNER_TABLES = (
    "sms_account_plans", "sms_api_keys", "sms_billing_profiles", "sms_campaigns",
    "sms_consent_events", "sms_consent_state", "sms_contact_lists", "sms_contacts",
    "sms_email_domains", "sms_emails", "sms_events", "sms_inbound_messages", "sms_invoices",
    "sms_keywords", "sms_messages", "sms_payments", "sms_sender_ids", "sms_subscriptions",
    "sms_templates", "sms_wallets", "sms_webhook_endpoints",
)  # fmt: skip
MAX_LEN = 64
log = logging.getLogger("sms.enterprises")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_SEPARATORS = re.compile(r"[\s_\-.]+")


class InvalidOwnerRef(WalletError):
    code = "invalid_owner_ref"


class EnterpriseNotFound(NotFound):
    code = "not_found"


@dataclass
class OwnerRefAudit:
    """Rezultati i auditimit të `owner_ref` në të dhënat legacy (vetëm-lexim)."""

    owners: list[str] = field(default_factory=list)  # të vlefshmit, të saktë, unikë
    errors: list[str] = field(default_factory=list)  # ndalojnë migrimin
    warnings: list[str] = field(default_factory=list)  # raportohen, s'ndalojnë
    null_counts: dict[str, int] = field(default_factory=dict)  # p.sh. çelësat e stafit

    @property
    def ok(self) -> bool:
        return not self.errors

    def report(self) -> str:
        lines = [f"{len(self.owners)} owner_ref të vlefshëm"]
        lines += [f"  ERROR: {e}" for e in self.errors]
        lines += [f"  WARNING: {w}" for w in self.warnings]
        if self.null_counts:
            lines.append(f"  NULL (i pritur për staf): {self.null_counts}")
        return "\n".join(lines)


def _existing_tables(db: Session) -> list[str]:
    have = set(inspect(db.get_bind()).get_table_names())
    return [t for t in LEGACY_OWNER_TABLES if t in have]


def audit_owner_refs(db: Session) -> OwnerRefAudit:
    """Nxjerr `owner_ref` distinct nga të gjitha tabelat legacy dhe raporton anomalitë.
    Nuk ndryshon asgjë dhe nuk bashkon asgjë."""
    out = OwnerRefAudit()
    where: dict[str, set[str]] = defaultdict(set)
    for table in _existing_tables(db):
        # emri i tabelës vjen nga konstanta e mësipërme, jo nga input i jashtëm
        for (v,) in db.execute(text(f"SELECT DISTINCT owner_ref FROM {table}")):  # noqa: S608
            if v is None:
                n = db.execute(
                    text(f"SELECT count(*) FROM {table} WHERE owner_ref IS NULL")
                ).scalar()  # noqa: S608
                out.null_counts[table] = int(n)
            else:
                where[v].add(table)
    for v, tables in sorted(where.items()):
        where_s = ", ".join(sorted(tables))
        if v == "":
            out.errors.append(f"owner_ref bosh ('') te: {where_s}")
        elif v != v.strip():
            out.errors.append(f"owner_ref me hapësira në fillim/fund {v!r} te: {where_s}")
        elif _CONTROL.search(v):
            out.errors.append(f"owner_ref me karaktere kontrolli {v!r} te: {where_s}")
        elif len(v) > MAX_LEN:
            out.errors.append(f"owner_ref më i gjatë se {MAX_LEN}: {v[:30]!r}… te: {where_s}")
    valid = [
        v for v in where if v == v.strip() and v and not _CONTROL.search(v) and len(v) <= MAX_LEN
    ]
    by_lower: dict[str, list[str]] = defaultdict(list)
    for v in valid:
        by_lower[v.lower()].append(v)
    for variants in by_lower.values():
        if len(variants) > 1:
            out.errors.append(
                f"variante që ndryshojnë vetëm nga shkronjat: {sorted(variants)} "
                "(nuk bashkohen automatikisht: vendos dhe korrigjo të dhënat)"
            )
    by_sep: dict[str, set[str]] = defaultdict(set)
    for v in valid:
        by_sep[_SEPARATORS.sub("", v.lower())].add(v)
    for variants in by_sep.values():
        if len(variants) > 1 and len({x.lower() for x in variants}) > 1:
            out.warnings.append(
                f"përplasje e mundshme semantike (vetëm ndarës ndryshojnë): {sorted(variants)}"
            )
    out.owners = sorted(valid)
    return out


@dataclass
class BackfillResult:
    created: int
    already_present: int
    audit: OwnerRefAudit


def backfill_missing(db: Session) -> BackfillResult:
    """Krijon Enterprise për çdo `owner_ref` legacy që s'ka; idempotent, UUID të qëndrueshme
    (ekzistueset nuk preken). Refuzon (pa shkruar asgjë) nëse ka anomali."""
    audit = audit_owner_refs(db)
    if not audit.ok:
        raise InvalidOwnerRef("owner_ref anomalies block the backfill:\n" + audit.report())
    have = set(db.scalars(select(Enterprise.owner_ref)))
    missing = [o for o in audit.owners if o not in have]
    for o in missing:
        db.add(Enterprise(owner_ref=o))
    db.flush()
    return BackfillResult(len(missing), len(audit.owners) - len(missing), audit)


def _check(owner_ref: str | None) -> str:
    if not isinstance(owner_ref, str) or owner_ref == "" or owner_ref != owner_ref.strip():
        raise InvalidOwnerRef("owner_ref must be a non-empty string without surrounding whitespace")
    return owner_ref


def for_owner_ref(db: Session, owner_ref: str | None) -> Enterprise | None:
    """Enterprise-i për një `owner_ref` të saktë; `None` nëse nuk ekziston (nuk krijohet).
    `None`/bosh/me hapësira → InvalidOwnerRef (asnjë normalizim i heshtur)."""
    return db.scalar(select(Enterprise).where(Enterprise.owner_ref == _check(owner_ref)))


def require_for_owner_ref(db: Session, owner_ref: str | None) -> Enterprise:
    e = for_owner_ref(db, owner_ref)
    if e is None:
        raise EnterpriseNotFound(f"no enterprise for owner_ref {owner_ref!r}")
    return e


def count(db: Session) -> int:
    return db.scalar(select(func.count()).select_from(Enterprise))


# --- M1b: zgjidhja e centralizuar owner_ref → enterprise.id (dual-write) ----------------------


def valid_owner_ref(owner_ref) -> bool:
    return (
        isinstance(owner_ref, str)
        and owner_ref != ""
        and owner_ref == owner_ref.strip()
        and len(owner_ref) <= MAX_LEN
        and not _CONTROL.search(owner_ref)
    )


def _insert_if_absent(db: Session, owner_ref: str) -> None:
    """INSERT … ON CONFLICT DO NOTHING (i sigurt në konkurrencë; pa përjashtim nëse dy transaksione
    krijojnë të njëjtin tenant njëkohësisht, ose nëse një variant shkronjash e përplas indeksin)."""
    now = datetime.now(UTC)
    values = {
        "id": uuid.uuid4(), "owner_ref": owner_ref, "status": "active",
        "created_at": now, "updated_at": now,
    }  # fmt: skip
    dialect = db.get_bind().dialect.name
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as ins
    elif dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert as ins
    else:  # dialekte të tjerë: savepoint + kapje e IntegrityError
        from sqlalchemy.exc import IntegrityError

        try:
            with db.begin_nested():
                db.add(Enterprise(**values))
                db.flush()
        except IntegrityError:
            pass
        return
    db.execute(ins(Enterprise.__table__).values(**values).on_conflict_do_nothing())


def _from_loaded_rows(db: Session, owner_ref: str) -> uuid.UUID | None:
    """`enterprise_id` nga një rresht tenant-owned i të njëjtit `owner_ref` që sesioni e ka tashmë të
    ngarkuar (p.sh. Message që workeri sapo e lexoi, kur krijon Event). Mbështetet te invarianti
    `record.owner_ref == enterprise.owner_ref` (i verifikuar nga `enterprises_audit --check`)."""
    from sqlalchemy import inspect

    from app.models.tenant import TenantOwned

    for (
        obj
    ) in db.identity_map.values():  # vetëm rreshta të ruajtur (identity_map nuk përmban të rinj)
        if not isinstance(obj, TenantOwned) or obj.enterprise_id is None:
            continue
        if obj.owner_ref != owner_ref:
            continue
        st = inspect(obj)
        if st.attrs.owner_ref.history.has_changes() or st.attrs.enterprise_id.history.has_changes():
            continue  # owner_ref/enterprise_id në ndryshim e sipër: jo burim i besueshëm
        return obj.enterprise_id
    return None


def resolve_id(db: Session, owner_ref) -> uuid.UUID | None:
    """`enterprise.id` për një `owner_ref` të saktë; e krijon Enterprise-in nëse mungon (tenant i ri).
    Kthen `None` (pa përjashtim, pa krijuar asgjë) kur `owner_ref` është anomali: bosh, me hapësira,
    karaktere kontrolli, >64, ose ndryshon vetëm nga shkronjat e një ekzistuesi. Sjellja e
    sistemit nuk ndryshon: rreshti ruhet me `enterprise_id` NULL dhe kontrolli i konsistencës e raporton."""
    if owner_ref is None:
        return None
    if not valid_owner_ref(owner_ref):
        log.warning("owner_ref anomaly, enterprise_id left NULL: %r", owner_ref)
        return None
    cache: dict[str, uuid.UUID | None] = db.info.setdefault("_enterprise_ids", {})
    if owner_ref in cache and cache[owner_ref] is not None:
        return cache[owner_ref]
    known = _from_loaded_rows(db, owner_ref)
    if known is not None:  # rresht i njëjtit tenant, tashmë i ngarkuar në sesion: pa SELECT
        cache[owner_ref] = known
        return known
    with db.no_autoflush:
        found = db.scalar(select(Enterprise.id).where(Enterprise.owner_ref == owner_ref))
        if found is None:
            _insert_if_absent(db, owner_ref)
            found = db.scalar(select(Enterprise.id).where(Enterprise.owner_ref == owner_ref))
    if found is None:
        log.warning(
            "owner_ref %r conflicts with an existing enterprise variant; enterprise_id NULL",
            owner_ref,
        )
        return None
    cache[owner_ref] = found
    return found


# --- Kontrolli i konsistencës dhe backfill-i në batch ---------------------------------------


@dataclass
class TableConsistency:
    table: str
    rows: int
    null_enterprise_id: (
        int  # owner_ref i plotësuar por enterprise_id NULL (backfill i mbetur / anomali)
    )
    mismatched: int  # enterprise_id nuk përputhet me owner_ref të Enterprise-it (DUHET të jetë 0)


@dataclass
class ConsistencyReport:
    tables: list[TableConsistency]

    @property
    def mismatched(self) -> int:
        return sum(t.mismatched for t in self.tables)

    @property
    def unbackfilled(self) -> int:
        return sum(t.null_enterprise_id for t in self.tables)

    @property
    def ok(self) -> bool:
        return self.mismatched == 0

    @property
    def complete(self) -> bool:
        return self.ok and self.unbackfilled == 0

    def report(self) -> str:
        lines = [f"{'tabela':<26}{'rreshta':>10}{'pa enterprise_id':>18}{'jo-përputhje':>14}"]
        for t in self.tables:
            lines.append(f"{t.table:<26}{t.rows:>10}{t.null_enterprise_id:>18}{t.mismatched:>14}")
        lines.append(f"TOTAL: pa enterprise_id={self.unbackfilled}, jo-përputhje={self.mismatched}")
        return "\n".join(lines)


def check_consistency(db: Session) -> ConsistencyReport:
    """Invariant: për çdo rresht me `enterprise_id`, `owner_ref` = `owner_ref` i atij Enterprise.
    Vetëm-lexim. Tabelat pa kolonën `enterprise_id` (para 0019) anashkalohen."""
    insp = inspect(db.get_bind())
    out = []
    for t in _existing_tables(db):
        if "enterprise_id" not in {c["name"] for c in insp.get_columns(t)}:
            continue
        rows = db.execute(text(f"SELECT count(*) FROM {t}")).scalar()  # noqa: S608
        nulls = db.execute(
            text(f"SELECT count(*) FROM {t} WHERE owner_ref IS NOT NULL AND enterprise_id IS NULL")  # noqa: S608
        ).scalar()
        bad = db.execute(
            text(  # noqa: S608
                f"SELECT count(*) FROM {t} x WHERE x.enterprise_id IS NOT NULL AND NOT EXISTS "
                "(SELECT 1 FROM sms_enterprises e WHERE e.id = x.enterprise_id AND e.owner_ref = x.owner_ref)"
            )
        ).scalar()
        out.append(TableConsistency(t, int(rows), int(nulls), int(bad)))
    return ConsistencyReport(out)


def backfill_enterprise_ids(
    session_factory, batch: int = 5000, tables: tuple[str, ...] | None = None, log_fn=None
) -> dict[str, int]:
    """Plotëson `enterprise_id` për rreshtat ekzistues, në batch me commit për batch (pa UPDATE gjigant,
    pa kyçje të gjata). Idempotent dhe i rifillueshëm: prek vetëm rreshtat me `enterprise_id` NULL;
    ecën me kursor `id` që të mos ziejë kurrë te rreshtat pa Enterprise (anomali). Krijon Enterprise-t
    që mungojnë (vetëm nëse auditi është i pastër). → {tabela: rreshta të përditësuar}."""
    with session_factory() as db:
        backfill_missing(db)
        db.commit()
    done: dict[str, int] = {}
    with session_factory() as db:
        names = [t for t in (tables or _existing_tables(db))]
    for table in names:
        total, last = 0, 0
        while True:
            with session_factory() as db:
                ids = list(
                    db.execute(
                        text(  # noqa: S608
                            f"SELECT id FROM {table} WHERE id > :last AND enterprise_id IS NULL "
                            "AND owner_ref IS NOT NULL ORDER BY id LIMIT :n"
                        ),
                        {"last": last, "n": batch},
                    ).scalars()
                )
                if not ids:
                    break
                res = db.execute(
                    text(  # noqa: S608
                        f"UPDATE {table} SET enterprise_id = "
                        "(SELECT e.id FROM sms_enterprises e WHERE e.owner_ref = "
                        f"{table}.owner_ref) WHERE id >= :lo AND id <= :hi AND enterprise_id IS NULL"
                    ),
                    {"lo": ids[0], "hi": ids[-1]},
                )
                db.commit()
                total += res.rowcount or 0
                last = ids[-1]
                if log_fn:
                    log_fn(f"{table}: {total} rreshta…")
        done[table] = total
    return done


__all__ = [
    "LEGACY_OWNER_TABLES", "BackfillResult", "EnterpriseNotFound", "InvalidOwnerRef",
    "ConsistencyReport", "OwnerRefAudit", "TableConsistency", "audit_owner_refs",
    "backfill_enterprise_ids", "backfill_missing", "check_consistency", "count", "for_owner_ref",
    "resolve_id", "valid_owner_ref",
    "require_for_owner_ref",
]  # fmt: skip
