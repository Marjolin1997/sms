#!/usr/bin/env bash
# Rikthen një backup në një bazë të RE (kurrë mbi ekzistuesen). Pastaj kontrolloni me
# scripts/verify_ledger.py përpara se ta drejtoni aplikacionin te baza e re.
#   ADMIN_URL=postgresql://postgres:...@host/postgres scripts/restore.sh backups/sms-....dump sms_restored
set -euo pipefail
dump="${1:?usage: restore.sh <dump> <new_db_name>}"
target="${2:?usage: restore.sh <dump> <new_db_name>}"
: "${ADMIN_URL:?set ADMIN_URL (lidhje me bazën 'postgres' me të drejtë CREATEDB)}"
[[ "$target" =~ ^[a-z][a-z0-9_]{0,62}$ ]] || { echo "invalid database name" >&2; exit 2; }
if [[ -f "$dump.sha256" ]]; then
  ( cd "$(dirname "$dump")" && sha256sum -c "$(basename "$dump").sha256" )
else
  echo "WARNING: no checksum file next to the dump" >&2
fi
exists="$(psql "$ADMIN_URL" -Atc "select 1 from pg_database where datname='$target'")"
[[ -z "$exists" ]] || { echo "database '$target' already exists: refusing to overwrite" >&2; exit 3; }
psql "$ADMIN_URL" -qc "create database \"$target\""
base="${ADMIN_URL%/*}"
pg_restore --no-owner --no-privileges --exit-on-error --dbname="$base/$target" "$dump"
echo "OK restored into '$target'. Next: SMS_DATABASE_URL=${base/postgresql:/postgresql+psycopg:}/$target python -m scripts.verify_ledger"
