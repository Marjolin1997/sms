"""Regjistron (manualisht) çelësin publik Ed25519 të një klienti shërbimi; idempotent, pa sekrete.

    CENTRAL_DATABASE_URL=... python -m apps.central.tools.create_service_credential \\
        --client-id enterprise-main --kid 2030-01 --public-key-file enterprise.pub.pem \\
        [--scope sync:read] [--enterprise <uuid> ...]

Operatori e gjeneron çiftin ÇELËSASH jashtë Central dhe jep VETËM çelësin publik:
    openssl genpkey -algorithm ed25519 -out enterprise.key.pem
    openssl pkey -in enterprise.key.pem -pubout -out enterprise.pub.pem
Klient i ri: krijohet me çelësin dhe enterprise-et (`--enterprise`). Klient ekzistues: shtohet
çelësi (rotacion); enterprise-et ndryshojnë vetëm me `service_credential_admin` (rrit generation).
Kodet: 0 ok/no-op · 1 konflikt · 2 gabim input/konfigurim.
"""

import argparse
import sys
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from apps.central.core.config import settings
from apps.central.core.db import make_engine
from apps.central.core.errors import CentralError, Conflict
from apps.central.models.service_auth import ServiceClient
from apps.central.services import audit, service_auth

LABEL = "system:service_client_configuration"


def run(client_id, kid, public_key_pem, scopes, enterprise_ids, engine=None) -> tuple[int, str]:
    engine = engine or make_engine(settings.database_url)
    with Session(engine, expire_on_commit=False) as db:
        try:
            existing = db.scalar(select(ServiceClient).where(ServiceClient.client_id == client_id))
            exists = None if existing is None else existing.id
            if existing is not None and scopes and sorted(set(scopes)) != sorted(existing.scopes):
                return 2, "client exists: its scopes cannot be changed here (no escalation)"
            if exists is None:
                client = service_auth.create_client(db, client_id, scopes, enterprise_ids)
                audit.record_system(
                    db, label=LABEL, action="service_client.create", resource_type="service_client",
                    resource_id=client.id,
                    detail={"client_id": client_id, "scopes": list(client.scopes),
                            "enterprises": sorted(str(e) for e in enterprise_ids)},
                )  # fmt: skip
                created = True
            else:
                if enterprise_ids:
                    return (
                        2,
                        "client exists: use service_credential_admin grant to change enterprises",
                    )
                created = False
            key, new_key = service_auth.add_key(db, client_id, kid, public_key_pem)
            if (
                new_key
            ):  # vetëm çelësi PUBLIK ekziston këtu; në audit shkon vetëm kid (jo materiali)
                audit.record_system(
                    db, label=LABEL, action="service_key.add", resource_type="service_client",
                    resource_id=service_auth.get_client(db, client_id).id,
                    detail={"client_id": client_id, "kid": kid},
                )  # fmt: skip
        except Conflict as e:
            db.rollback()
            return 1, f"conflict: {e}"
        db.commit()
    if created:
        return 0, f"created client {client_id} with key {kid}"
    return (
        0,
        f"added key {kid} to {client_id}"
        if new_key
        else f"key {kid} already registered (unchanged)",
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Register a service client public key (manual).")
    ap.add_argument("--client-id", required=True)
    ap.add_argument("--kid", required=True)
    ap.add_argument("--public-key-file", required=True)
    ap.add_argument("--scope", action="append", dest="scopes")
    ap.add_argument("--enterprise", action="append", dest="enterprises", default=[])
    args = ap.parse_args(argv)
    try:
        with open(args.public_key_file, "rb") as f:
            pem = f.read()
        ids = [uuid.UUID(e) for e in args.enterprises]
        code, msg = run(args.client_id, args.kid, pem, args.scopes, ids)
    except (CentralError, OSError, ValueError) as e:
        print(f"error: {type(e).__name__}: {str(e)[:200]}", file=sys.stderr)  # noqa: T201
        return 2
    except Exception as e:
        print(f"create_service_credential failed: {type(e).__name__}", file=sys.stderr)  # noqa: T201
        return 2
    print(msg, file=sys.stderr if code else sys.stdout)  # noqa: T201
    return code


if __name__ == "__main__":
    sys.exit(main())
