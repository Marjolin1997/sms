"""Krijon (manualisht) stafin e parë të Central; idempotent, pa shfaqur fjalëkalim/hash.

    CENTRAL_DATABASE_URL=... CENTRAL_ADMIN_PASSWORD=... \\
        python -m apps.central.tools.create_admin --email ana@example.com [--role admin|operator]

Fjalëkalimi vjen nga `CENTRAL_ADMIN_PASSWORD` ose kërkohet në terminal (getpass, dy herë); asnjëherë
nga argumentet e komandës dhe asnjë parazgjedhje e koduar. Email ekziston: nëse fjalëkalimi
dhe roli përputhen → no-op (kodi 0), përndryshe asgjë nuk ndryshohet (kodi 1).
Kodet: 0 ok · 1 konflikt · 2 gabim.
"""

import argparse
import getpass
import os
import sys

from sqlalchemy.orm import Session

from apps.central.core import passwords
from apps.central.core.config import settings
from apps.central.core.db import make_engine
from apps.central.core.errors import CentralError
from apps.central.models.user import Role
from apps.central.services import audit, users


def _password() -> str:
    env = os.environ.get("CENTRAL_ADMIN_PASSWORD")
    if env:
        return env
    if not sys.stdin.isatty():
        raise CentralError("set CENTRAL_ADMIN_PASSWORD or run in a terminal")
    first = getpass.getpass("Password: ")
    if first != getpass.getpass("Repeat password: "):
        raise CentralError("passwords do not match")
    return first


def run(email: str, password: str, role: str, engine=None) -> tuple[int, str]:
    """→ (kodi, mesazh pa sekrete)."""
    engine = engine or make_engine(settings.database_url)
    with Session(engine, expire_on_commit=False) as db:
        existing = users.get_by_email(db, email)
        if existing is not None:
            same = existing.role == role and passwords.verify_password(
                existing.password_hash, password
            )
            if same:
                return 0, f"user already exists (unchanged): {existing.email} id={existing.id}"
            return (
                1,
                f"user {existing.email} exists with different credentials or role; no changes made",
            )
        user = users.create_user(db, email, password, role)
        audit.record_system(
            db, label="system:create_admin", action="user.create", resource_type="user",
            resource_id=user.id, detail={"email": user.email, "role": user.role},
        )  # fmt: skip
        db.commit()
        return 0, f"created {user.role} {user.email} id={user.id}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Create the first Central staff user (manual).")
    ap.add_argument("--email", required=True)
    ap.add_argument("--role", choices=[r.value for r in Role], default=Role.ADMIN.value)
    args = ap.parse_args(argv)
    try:
        email = users.normalize_email(args.email)
        password = passwords.validate(_password())
        code, msg = run(email, password, args.role)
    except CentralError as e:
        print(f"error: {e}", file=sys.stderr)  # noqa: T201
        return 2
    except Exception as e:  # pa URL/sekrete në mesazh
        print(f"create_admin failed: {type(e).__name__}", file=sys.stderr)  # noqa: T201
        return 2
    print(msg, file=sys.stderr if code else sys.stdout)  # noqa: T201
    return code


if __name__ == "__main__":
    sys.exit(main())
