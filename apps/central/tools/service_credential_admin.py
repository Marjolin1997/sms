"""Administrim manual i klientëve të shërbimit: grant/revoke enterprise, disable key/client.

    python -m apps.central.tools.service_credential_admin grant   --client-id C --enterprise <uuid>
    python -m apps.central.tools.service_credential_admin revoke  --client-id C --enterprise <uuid>
    python -m apps.central.tools.service_credential_admin disable-key    --client-id C --kid K
    python -m apps.central.tools.service_credential_admin disable-client --client-id C
    python -m apps.central.tools.service_credential_admin enable-auto-grant  --client-id C
    python -m apps.central.tools.service_credential_admin disable-auto-grant --client-id C

`enable-auto-grant`: enterprise-et e SAPOKRIJUARA nga provisioning-u i regjistrimit grantohen te ky
klient (aktiv); pa grant retroaktiv, pa bump `auth_generation` për vetë flamurin.

`grant`/`revoke` ndryshojnë bashkësinë e autorizuar dhe rrisin `auth_generation`: konsumatori duhet
snapshot të plotë. Çdo ndryshim real auditohet (M8-e), aktor sistemi
`system:service_client_configuration`.
Kodet: 0 ok/no-op · 2 gabim.
"""

import argparse
import sys
import uuid

from sqlalchemy.orm import Session

from apps.central.core.config import settings
from apps.central.core.db import make_engine
from apps.central.core.errors import CentralError
from apps.central.services import audit, service_auth

LABEL = "system:service_client_configuration"


def run(action: str, client_id: str, enterprise_id=None, kid=None, engine=None) -> str:
    """Çdo ndryshim real shkruhet te `audit_log` me aktor sistemi (CLI s'ka identitet njeriu) në të
    njëjtin transaksion; no-op ⇒ pa audit. Detail: vlera e vjetër/e re, kurrë sekrete."""
    engine = engine or make_engine(settings.database_url)
    with Session(engine, expire_on_commit=False) as db:
        detail = {"client_id": client_id}
        if action == "grant":
            changed = service_auth.grant_enterprise(db, client_id, enterprise_id)
            detail["enterprise_id"] = str(enterprise_id)
        elif action == "revoke":
            changed = service_auth.revoke_enterprise(db, client_id, enterprise_id)
            detail["enterprise_id"] = str(enterprise_id)
        elif action == "disable-key":
            changed = service_auth.disable_key(db, client_id, kid)
            detail["kid"] = kid
        elif action in ("enable-auto-grant", "disable-auto-grant"):
            enabled = action == "enable-auto-grant"
            changed = service_auth.set_auto_grant(db, client_id, enabled)
            detail["auto_grant_new_enterprises"] = {"before": not enabled, "after": enabled}
        else:
            changed = service_auth.disable_client(db, client_id)
        client = service_auth.get_client(db, client_id)
        if changed:
            audit.record_system(
                db, label=LABEL, action=f"service_client.{action.replace('-', '_')}",
                resource_type="service_client", resource_id=client.id, detail=detail,
            )  # fmt: skip
        db.commit()
        gen = client.auth_generation
    return f"{action}: {'changed' if changed else 'no change'} (auth_generation={gen})"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Manage service clients (manual).")
    ap.add_argument(
        "action",
        choices=[
            "grant",
            "revoke",
            "disable-key",
            "disable-client",
            "enable-auto-grant",
            "disable-auto-grant",
        ],
    )
    ap.add_argument("--client-id", required=True)
    ap.add_argument("--enterprise")
    ap.add_argument("--kid")
    args = ap.parse_args(argv)
    try:
        if args.action in ("grant", "revoke") and not args.enterprise:
            raise ValueError("--enterprise is required")
        if args.action == "disable-key" and not args.kid:
            raise ValueError("--kid is required")
        eid = uuid.UUID(args.enterprise) if args.enterprise else None
        print(run(args.action, args.client_id, eid, args.kid))  # noqa: T201
    except (CentralError, ValueError) as e:
        print(f"error: {type(e).__name__}: {str(e)[:200]}", file=sys.stderr)  # noqa: T201
        return 2
    except Exception as e:
        print(f"service_credential_admin failed: {type(e).__name__}", file=sys.stderr)  # noqa: T201
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
