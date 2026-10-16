"""M1a: regjistri i Enterprise-ve (STRIKT ADITIV)

Krijon vetëm `sms_enterprises` dhe e mbush me një rresht për çdo `owner_ref` legacy, me UUID të
gjeneruar një herë dhe të ruajtur. NUK prek asnjë tabelë tjetër (pa kolona, pa FK, pa UPDATE),
NUK ndryshon sjelljen e sistemit. Auditon `owner_ref` PARA se të krijojë ndonjë gjë; nëse gjen
anomali (bosh, hapësira, karaktere kontrolli, variante vetëm nga shkronjat, gjatësi >64) ndalon
me raport të qartë dhe nuk bashkon asgjë. Përplasjet vetëm nga ndarësit raportohen si paralajmërim.

Leximet janë `SELECT DISTINCT` mbi tabelat legacy (kyçje ACCESS SHARE, pa shkrime).
Rikthimi: downgrade heq `sms_enterprises` (UUID-t humbin; s'i referon asgjë në M1a).

Revision ID: 0018
Revises: 0017
"""

import re
import uuid
from collections import defaultdict
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None

# Kopje e ngrirë e app.services.enterprises.LEGACY_OWNER_TABLES (testi i driftit i krahason)
TABLES = (
    "sms_account_plans", "sms_api_keys", "sms_billing_profiles", "sms_campaigns",
    "sms_consent_events", "sms_consent_state", "sms_contact_lists", "sms_contacts",
    "sms_email_domains", "sms_emails", "sms_events", "sms_inbound_messages", "sms_invoices",
    "sms_keywords", "sms_messages", "sms_payments", "sms_sender_ids", "sms_subscriptions",
    "sms_templates", "sms_wallets", "sms_webhook_endpoints",
)  # fmt: skip
MAX_LEN = 64
CONTROL = re.compile(r"[\x00-\x1f\x7f]")
SEPARATORS = re.compile(r"[\s_\-.]+")


def _audit(bind) -> tuple[list[str], list[str], list[str]]:
    """→ (owners të vlefshëm, gabime, paralajmërime). Vetëm-lexim."""
    have = set(sa.inspect(bind).get_table_names())
    where: dict[str, set[str]] = defaultdict(set)
    for t in TABLES:
        if t not in have:
            continue
        for (v,) in bind.execute(sa.text(f"SELECT DISTINCT owner_ref FROM {t}")):  # noqa: S608
            if v is not None:  # NULL = çelës stafi (sms_api_keys): legjitim
                where[v].add(t)
    errors, warnings, valid = [], [], []
    for v, tabs in sorted(where.items()):
        at = ", ".join(sorted(tabs))
        if v == "":
            errors.append(f"owner_ref bosh ('') te: {at}")
        elif v != v.strip():
            errors.append(f"owner_ref me hapësira në fillim/fund {v!r} te: {at}")
        elif CONTROL.search(v):
            errors.append(f"owner_ref me karaktere kontrolli {v!r} te: {at}")
        elif len(v) > MAX_LEN:
            errors.append(f"owner_ref më i gjatë se {MAX_LEN}: {v[:30]!r}... te: {at}")
        else:
            valid.append(v)
    by_lower = defaultdict(list)
    for v in valid:
        by_lower[v.lower()].append(v)
    for variants in by_lower.values():
        if len(variants) > 1:
            errors.append(f"variante që ndryshojnë vetëm nga shkronjat: {sorted(variants)}")
    by_sep = defaultdict(set)
    for v in valid:
        by_sep[SEPARATORS.sub("", v.lower())].add(v)
    for variants in by_sep.values():
        if len({x.lower() for x in variants}) > 1:
            warnings.append(f"përplasje e mundshme semantike (vetëm ndarës): {sorted(variants)}")
    return sorted(valid), errors, warnings


def upgrade() -> None:
    bind = op.get_bind()
    owners, errors, warnings = _audit(bind)
    for w in warnings:
        print(f"[0018] WARNING: {w}")  # noqa: T201
    if errors:
        raise RuntimeError(
            "Migrimi 0018 NDALOI: anomali në owner_ref (asgjë nuk u ndryshua, asgjë nuk u bashkua).\n"
            + "\n".join(f"  - {e}" for e in errors)
            + "\nKorrigjo të dhënat dhe ekzekuto sërish. Raport: python -m scripts.enterprises_audit"
        )
    enterprises = op.create_table(
        "sms_enterprises",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("owner_ref", sa.String(64), nullable=False),
        sa.Column("external_id", sa.String(64), nullable=True),
        sa.Column("legal_name", sa.String(200), nullable=True),
        sa.Column("short_name", sa.String(64), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("owner_ref", name="uq_sms_enterprises_owner_ref"),
    )
    op.create_index(
        "uq_sms_enterprises_owner_ref_lower",
        "sms_enterprises",
        [sa.text("lower(owner_ref)")],
        unique=True,
    )
    op.create_index(
        "uq_sms_enterprises_external_id",
        "sms_enterprises",
        ["external_id"],
        unique=True,
        postgresql_where=sa.text("external_id IS NOT NULL"),
        sqlite_where=sa.text("external_id IS NOT NULL"),
    )
    now = datetime.now(UTC)
    if owners:
        op.bulk_insert(
            enterprises,
            [
                {
                    "id": uuid.uuid4(),
                    "owner_ref": o,
                    "status": "active",
                    "created_at": now,
                    "updated_at": now,
                }
                for o in owners
            ],
        )
    print(f"[0018] {len(owners)} Enterprise të krijuar nga owner_ref legacy")  # noqa: T201


def downgrade() -> None:
    op.drop_index("uq_sms_enterprises_external_id", table_name="sms_enterprises")
    op.drop_index("uq_sms_enterprises_owner_ref_lower", table_name="sms_enterprises")
    op.drop_table("sms_enterprises")
