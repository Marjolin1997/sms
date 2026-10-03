"""Administrim manual i klientëve të shërbimit: grant/revoke enterprise, disable key/client.

    python -m apps.central.tools.service_credential_admin grant   --client-id C --enterprise <uuid>
    python -m apps.central.tools.service_credential_admin revoke  --client-id C --enterprise <uuid>
    python -m apps.central.tools.service_credential_admin disable-key    --client-id C --kid K
    python -m apps.central.tools.service_credential_admin disable-client --client-id C

`grant`/`revoke` ndryshojnë bashkësinë e autorizuar dhe rrisin `auth_generation`: konsumatori duhet
snapshot të plotë. Ndryshimet nuk shkruhen te `audit_log` (CLI pa aktor): borxh para go-live.
Kodet: 0 ok/no-op · 2 gabim.
"""

import argparse
import sys
import uuid

from sqlalchemy.orm import Session

from apps.central.core.config import settings
from apps.central.core.db import make_engine
from apps.central.core.errors import CentralError
from apps.central.services import service_auth


def run(action: str, client_id: str, enterprise_id=None, kid=None, engine=None) -> str:
    engine = engine or make_engine(settings.database_url)
    with Session(engine, expire_on_commit=False) as db:
        if action == "grant":
            changed = service_auth.grant_enterprise(db, client_id, enterprise_id)
        elif action == "revoke":
            changed = service_auth.revoke_enterprise(db, client_id, enterprise_id)
        elif action == "disable-key":
            changed = service_auth.disable_key(db, client_id, kid)
        else:
            changed = service_auth.disable_client(db, client_id)
        db.commit()
        gen = service_auth.get_client(db, client_id).auth_generation
    return f"{action}: {'changed' if changed else 'no change'} (auth_generation={gen})"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Manage service clients (manual).")
    ap.add_argument("action", choices=["grant", "revoke", "disable-key", "disable-client"])
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
