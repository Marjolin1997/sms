"""Krijon një përdorues dhe printon lidhjen e ftesës.

python -m scripts.create_user ana@example.com superadmin
python -m scripts.create_user ana@example.com client --owner acme
"""

import argparse

from app.core.config import settings
from app.core.db import SessionLocal
from app.services import auth


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("email")
    ap.add_argument("role")
    ap.add_argument("--owner", default=None, help="owner_ref (vetëm për role=client)")
    a = ap.parse_args()
    with SessionLocal() as db:
        u, token = auth.create_user(db, a.email, a.role, a.owner, "cli")
        db.commit()
    base = settings.public_base_url.rstrip("/")
    print(f"Created {u.email} ({u.role}). Invite link, valid 72 hours, shown once:")
    print(f"{base}/#accept/{token}")
    print("(if the panel runs on another address, keep the part from #accept/ and use that host)")


if __name__ == "__main__":
    main()
